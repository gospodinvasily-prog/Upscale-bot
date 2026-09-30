"""
bt_trades.py — полноценный бэктест СДЕЛОК (не сигналов).

Отвечает на главный вопрос: есть ли у стратегии положительное матожидание
после комиссий и проскальзывания.

Чем отличается от bt_range.py: тот мерил «вышла ли цена из коридора».
Этот прогоняет сделку по свечам от входа до выхода и смотрит, что случилось
РАНЬШЕ — стоп или тейк. Именно порядок событий решает, плюс система или минус.

Запуск на Render: RUN_BACKTEST=trades
Настройки: BT_DAYS (60), BT_PAIRS (0 = все), BT_STEP (2), BT_FEE_PCT (0.05)

КАЛИБРОВКА (проверено перед выпуском):
  • на чистом случайном блуждании даёт -0.21R — ложного преимущества не показывает;
  • на данных с заложенным преимуществом (сжатие → импульс) даёт -0.02R,
    то есть разницу в +0.2R улавливает.
  Инструмент КОНСЕРВАТИВЕН: реальное преимущество скорее занизит, чем завысит.
  Поэтому плюс на живых данных — сигнал надёжный, минус около нуля — неоднозначный.

ЧЕСТНЫЕ ОГРАНИЧЕНИЯ (читать обязательно):
1. Вход — стоп-маркет на свече ПЕРЕСЕЧЕНИЯ триггера, по цене триггера или по
   открытию, если свеча открылась уже за ним. Свеча входа включена в прогон,
   разворот внутри неё считается. Требовать закрытия свечи за уровнем нельзя —
   это заглядывание в будущее.
2. Внутрисвечная неоднозначность. Если в одной свече задеты и стоп, и тейк —
   мы не знаем, что было первым. Считаем СТОП (пессимистично). Это занижает
   результат, но не даёт себя обмануть.
3. Подтверждение пробоя. Живой бот ждёт закрытия 1-минутной свечи за уровнем
   с объёмом ≥2×. На истории часовых свечей 1м нет, поэтому пробой = часовая
   свеча закрылась за уровнем при объёме ≥2× нормы. Это приближение.
4. OI не восстанавливается за историю — ветка «объём ИЛИ рост OI» работает
   только по объёму. Значит зарядов в бэктесте МЕНЬШЕ, чем у живого бота.
5. Сделки не пересекаются: после входа пропускаем его горизонт. Иначе
   трендовый участок даёт десяток входов подряд и завышает среднее.
6. Оценка силы заряда (score ≥ 6) НЕ применяется: она на треть состоит из OI
   и taker-потока, которых в истории нет. Тестируем механику на структурно
   валидных зарядах — их больше, чем у живого бота.
7. Фильтры дня (1 монета в день, лимиты сигналов, дневной стоп) не моделируются —
   они режут количество сделок, но не меняют матожидание одной сделки.
"""
import os
import time
import statistics
from datetime import datetime

import bot as B


BT_DAYS  = int(os.environ.get("BT_DAYS", "60"))
BT_PAIRS = int(os.environ.get("BT_PAIRS", "0"))
BT_STEP  = int(os.environ.get("BT_STEP", "2"))
FEE_PCT  = float(os.environ.get("BT_FEE_PCT", "0.05"))   # комиссия+спред за круг, % (замер на Upscale)
SLIP_PCT = float(os.environ.get("BT_SLIP_PCT", "0.03"))  # проскальзывание входа стоп-маркетом, %
# Две поправки на то, чего нет в исторических часовых свечах.
# 1) OI даёт живому боту до +3 к силе заряда, в истории его не восстановить: заряды,
#    которые живьём набрали бы 6-8, здесь набирают 3-5 и отсеиваются порогом 6.
#    Поэтому в бэктесте порог силы ниже на 2 — иначе выборка нерепрезентативна.
# 2) Живой бот подтверждает пробой объёмом на МИНУТНОЙ свече (≥2× её нормы). У минутки
#    всплеск в момент пробоя обычен, у часовой почти никогда. Для часового прокси порог ниже.
BT_MIN_SCORE  = int(os.environ.get("BT_MIN_SCORE", str(max(1, B.ACC_MIN_SCORE - 2))))
BT_BREAK_RVOL = float(os.environ.get("BT_BREAK_RVOL", "1.3"))
MAX_HOLD_H = int(os.environ.get("BT_MAX_HOLD_H", "12"))  # держим не дольше горизонта оценки


def _baseline(upto):
    sl = upto[-B.BASE_FROM:-B.BASE_TO]
    if len(sl) < 10:
        return None, None
    vol = B.trimmed_mean([c["v"] for c in sl])
    atr = B.trimmed_mean(B.true_ranges(upto)[-B.BASE_FROM:-B.BASE_TO])
    return (vol, atr) if vol and atr and vol > 0 and atr > 0 else (None, None)


def _find_charge(upto, price, vol_base, atr_norm):
    """Заряд по СТРУКТУРЕ: цена стоит, коридор в адаптивном потолке, сжатие, объём.
    Оценку силы (score >= 6) не проверяем: она на треть состоит из OI и taker-потока,
    которых в исторических свечах нет, поэтому реальный score восстановить нельзя.
    Итог: тестируем механику сделки на всех структурно валидных зарядах."""
    W = B.ACC_WINDOW
    win = upto[-W:]
    hi = max(c["h"] for c in win)
    lo = min(c["l"] for c in win)
    if lo <= 0:
        return None
    rng_pct = (hi - lo) / lo * 100
    move = win[-1]["c"] - win[0]["o"]
    if abs(move) > B.ALT_P["flat_atr_eff"] * atr_norm:
        return None
    atr_pct = atr_norm / price * 100
    cap = min(B.ACC_MAX_RANGE_ABS,
              max(B.ACC_RANGE_FLOOR_PCT, B.ACC_RANGE_ATR_K * atr_pct * (W ** 0.5)))
    if rng_pct > cap:
        return None
    sq = B.bb_width_percentile([c["c"] for c in upto])
    trs = B.true_ranges(upto)
    tr_ratio = (sum(trs[-W:]) / W) / atr_norm if atr_norm > 0 else 99
    if not ((sq is not None and sq <= B.ACC_SQUEEZE_PCTL) or tr_ratio <= B.ACC_TR_RATIO_MAX):
        return None
    half = W // 2
    if (sum(c["v"] for c in win[-half:]) / half) / vol_base < B.ACC_RVOL_MIN:
        return None
    highs = [c["h"] for c in upto[-B.SWING_LOOKBACK:]]
    lows = [c["l"] for c in upto[-B.SWING_LOOKBACK:]]
    sh, sl = B.find_swings(highs, lows)
    return {"symbol": "BT", "hi": hi, "lo": lo, "height": hi - lo, "atr": atr_norm,
            "price": price, "rng_pct": rng_pct, "score": 0, "side": "both",
            "swing_highs": sorted(x for x in sh if x > price),
            "swing_lows": sorted((x for x in sl if 0 < x < price), reverse=True),
            "created": 0, "funding": 0.0, "change_24h": 0.0,
            "oi_win": None, "oi_15m": None, "P": B.ALT_P}


def _simulate(future, side, entry, stop, tp1, tp2, fee_pct, optimistic=False, amb=None):
    """Прогон сделки по свечам. Возвращает результат в R после комиссии.
    Половина на TP1, затем стоп в безубыток, остаток на TP2.

    optimistic: что считать, если в ОДНОЙ свече задеты и стоп, и цель.
      False — стоп (пессимизм), True — цель (оптимизм). Порядок внутри часовой
      свечи неизвестен, поэтому честный ответ — вилка между двумя прогонами.
      Это принципиально: TP1 (0.5R) ближе к входу, чем стоп (1R), и такие свечи
      частые — значит выбор допущения двигает итог сильно.
    amb: список для подсчёта, в скольких сделках исход решила спорная свеча."""
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    is_long = side == "long"
    half_done = False
    cur_stop = stop
    realized = 0.0

    for c in future:
        hit_stop = (c["l"] <= cur_stop) if is_long else (c["h"] >= cur_stop)
        tgt = tp1 if not half_done else tp2
        hit_tp = (c["h"] >= tgt) if is_long else (c["l"] <= tgt)

        if hit_stop and hit_tp:
            if amb is not None:
                amb.append(1)
            if not optimistic:
                part = 0.5 if half_done else 1.0
                r = (cur_stop - entry) / risk if is_long else (entry - cur_stop) / risk
                return realized + r * part - fee_pct / 100 * entry / risk
            # оптимистично: сначала цель
            if not half_done:
                realized += 0.5 * (abs(tp1 - entry) / risk)
                half_done = True
                cur_stop = entry
                continue
            realized += 0.5 * (abs(tp2 - entry) / risk)
            return realized - fee_pct / 100 * entry / risk

        if hit_stop:
            part = 0.5 if half_done else 1.0
            r = (cur_stop - entry) / risk if is_long else (entry - cur_stop) / risk
            return realized + r * part - fee_pct / 100 * entry / risk

        if hit_tp:
            if not half_done:
                realized += 0.5 * (abs(tp1 - entry) / risk)
                half_done = True
                cur_stop = entry
            else:
                realized += 0.5 * (abs(tp2 - entry) / risk)
                return realized - fee_pct / 100 * entry / risk

    last = future[-1]["c"] if future else entry
    part = 0.5 if half_done else 1.0
    r = (last - entry) / risk if is_long else (entry - last) / risk
    return realized + r * part - fee_pct / 100 * entry / risk


def _stats(rs, label):
    if not rs:
        return [f"  {label}: сделок нет"]
    n = len(rs)
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    exp = sum(rs) / n
    gp = sum(wins) or 0.0
    gl = abs(sum(losses)) or 0.0
    pf = (gp / gl) if gl > 0 else float("inf")
    # накопленная кривая и просадка
    eq, peak, dd = 0.0, 0.0, 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    # 95% интервал матожидания
    sd = statistics.pstdev(rs) if n > 1 else 0.0
    se = sd / (n ** 0.5) if n else 0.0
    lo, hi = exp - 1.96 * se, exp + 1.96 * se
    return [
        f"  <b>{label}</b>: сделок {n}, винрейт {len(wins)/n*100:.0f}%",
        f"    матожидание <b>{exp:+.3f}R</b> (95%: {lo:+.3f}…{hi:+.3f})",
        f"    профит-фактор {pf:.2f}, итого {sum(rs):+.1f}R, просадка {dd:.1f}R",
        f"    вывод: " + ("<b>ПЛЮС</b> — нижняя граница выше нуля" if lo > 0
                          else "минус — верхняя граница ниже нуля" if hi < 0
                          else "статистически НЕ отличается от нуля, данных мало"),
    ]


def run():
    pairs = B.UPSCALE_PAIRS[:BT_PAIRS] if BT_PAIRS else B.UPSCALE_PAIRS
    W = B.ACC_WINDOW
    per_day = 24 * 60 // B.TF_MIN
    need = min(1990, BT_DAYS * per_day + B.BASE_FROM + W + 20)
    hold = max(4, int(MAX_HOLD_H * 60 / B.TF_MIN))
    watch = max(4, int(B.WATCH_TTL_HOURS * 60 / B.TF_MIN))

    cur_rs, old_rs, opt_rs = [], [], []
    amb = []
    charges = entries = 0
    n_cross = n_novol = 0
    saved_min = B.ACC_MIN_SCORE
    B.ACC_MIN_SCORE = BT_MIN_SCORE          # см. поправку 1 выше
    t0 = time.time()

    for i, sym in enumerate(pairs, 1):
        try:
            candles = B.get_candles(sym, B.CHARGE_TF, need)
        except Exception:
            continue
        if not candles or len(candles) < B.BASE_FROM + W + hold + 10:
            continue
        closed = candles[:-1]

        # v2: сделки НЕ ПЕРЕСЕКАЮТСЯ. Иначе сильный тренд даёт заряд за зарядом и десяток
        # выигрышных входов подряд, а пила — ни одного: число сделок само коррелирует
        # с результатом, и среднее по сделкам завышается (на случайных данных давало +0.77R).
        end = B.BASE_FROM + W
        limit_end = len(closed) - hold
        while end < limit_end:
            upto = closed[:end]
            vol_base, atr_norm = _baseline(upto)
            if not vol_base:
                end += BT_STEP
                continue
            price = upto[-1]["c"]
            c = _find_charge(upto, price, vol_base, atr_norm)
            if not c:
                end += BT_STEP
                continue
            charges += 1

            entered_at = None
            for k in range(max(end, 1), min(end + watch, limit_end)):
                bar = closed[k]
                prev = closed[k - 1]
                done = False
                for side in ("long", "short"):
                    lvl = c["hi"] if side == "long" else c["lo"]
                    trig = B.order_trigger(lvl, side == "long", c["atr"])
                    # Берём ТОЛЬКО свечу, в которой цена ПЕРЕСЕКАЕТ триггер (до неё была
                    # по эту сторону уровня). Раньше годилась любая свеча с максимумом выше
                    # триггера — и если уровень пробили часом раньше на тихом объёме, мы всё
                    # равно исполнялись по старой цене триггера, которой на рынке уже нет.
                    # На случайных данных это давало +1.07% хода из воздуха.
                    if side == "long":
                        crossing = prev["c"] <= trig and bar["h"] >= trig
                    else:
                        crossing = prev["c"] >= trig and bar["l"] <= trig
                    if not crossing:
                        continue
                    n_cross += 1
                    # объём проверяем на свече пересечения: не подтвердила — входа нет
                    if (bar["v"] / vol_base) < BT_BREAK_RVOL:
                        n_novol += 1
                        continue
                    # стоп-маркет: если свеча открылась уже за триггером — исполняемся по открытию
                    fill = max(trig, bar["o"]) if side == "long" else min(trig, bar["o"])
                    slip = fill * SLIP_PCT / 100
                    entry = fill + slip if side == "long" else fill - slip
                    bo = B.build_breakout(dict(c), side, entry, 0.0, None)
                    # свеча входа ВКЛЮЧЕНА: цена могла коснуться триггера и тут же развернуться
                    # в стоп внутри того же часа — не учитывать это было бы поддавками
                    fut = closed[k:k + 1 + hold]
                    if not fut:
                        continue
                    r = _simulate(fut, side, entry, bo["stop"], bo["tp1_price"],
                                  bo["tp2_price"], FEE_PCT, False, amb)
                    if r is None:
                        continue
                    cur_rs.append(r)
                    ro = _simulate(fut, side, entry, bo["stop"], bo["tp1_price"],
                                   bo["tp2_price"], FEE_PCT, True)
                    if ro is not None:
                        opt_rs.append(ro)
                    entries += 1
                    entered_at = k
                    dist = abs(entry - bo["stop"]) / entry * 100
                    if side == "long":
                        o1, o2 = entry * (1 + dist / 100), entry * (1 + 2 * dist / 100)
                    else:
                        o1, o2 = entry * (1 - dist / 100), entry * (1 - 2 * dist / 100)
                    r_old = _simulate(fut, side, entry, bo["stop"], o1, o2, FEE_PCT)
                    if r_old is not None:
                        old_rs.append(r_old)
                    done = True
                    break
                if done:
                    break

            # после сделки перескакиваем за её горизонт, иначе следующий заряд
            # будет наблюдать тот же кусок рынка
            end = (entered_at + hold + 1) if entered_at is not None else (end + watch)
        if i % 20 == 0:
            print(f"[BT] {i}/{len(pairs)} пар | зарядов {charges} | сделок {entries} | {time.time()-t0:.0f}с")

    B.ACC_MIN_SCORE = saved_min
    took = time.time() - t0
    lines = [
        f"💰 <b>Бэктест СДЕЛОК</b> ({B.CHARGE_TF}, {BT_DAYS} дн, {len(pairs)} пар)",
        f"Комиссия {FEE_PCT}% за круг, проскальзывание {SLIP_PCT}%, время {took/60:.1f} мин",
        f"<b>Воронка:</b> зарядов {charges} → пересечений уровня {n_cross} → "
        f"отсеяно объёмом {n_novol} → сделок <b>{entries}</b>",
        f"<i>порог силы в бэктесте {BT_MIN_SCORE} (живой {saved_min}, разница — нет истории OI); "
        f"порог объёма {BT_BREAK_RVOL}× на часовой (живой {B.BREAK_MIN_RVOL}× на минутной)</i>",
        "",
        "<b>Результат в R (1R = риск на сделку, у тебя $20):</b>",
    ]
    if cur_rs:
        lines.append(f"Исход решила спорная свеча (задеты и стоп, и цель): "
                     f"<b>{len(amb)/len(cur_rs)*100:.0f}%</b> сделок — "
                     f"в них порядок событий внутри часа неизвестен")
        lines.append("")
    lines += _stats(cur_rs, "ПЕССИМИСТИЧНО: в спорной свече сначала стоп")
    lines.append("")
    lines += _stats(opt_rs, "ОПТИМИСТИЧНО: в спорной свече сначала цель")
    if cur_rs and opt_rs:
        p, o = sum(cur_rs)/len(cur_rs), sum(opt_rs)/len(opt_rs)
        lines.append("")
        if p > 0:
            v = "<b>ПЛЮС при любом допущении</b> — стратегия рабочая"
        elif o < 0:
            v = "<b>МИНУС при любом допущении</b> — стратегия в текущем виде не работает"
        else:
            v = ("<b>вилка накрывает ноль</b> — часовых свечей НЕ ХВАТАЕТ для ответа, "
                 "нужен прогон по 15м/5м свечам")
        lines.append(f"Истина между {p:+.3f}R и {o:+.3f}R → {v}")
    lines.append("")
    lines += _stats(cur_rs, "(для сравнения тейков) текущие цели, пессимистично")
    lines.append("")
    lines += _stats(old_rs, "старые цели v8.3 (TP1 1R, TP2 2R)")
    if cur_rs and old_rs:
        d = sum(cur_rs) / len(cur_rs) - sum(old_rs) / len(old_rs)
        lines.append("")
        lines.append(f"<b>Уменьшение тейков дало {d:+.3f}R на сделку</b> — "
                     + ("правка оправдалась" if d > 0 else "правка НЕ оправдалась, вернуть старые"))
    if cur_rs:
        exp = sum(cur_rs) / len(cur_rs)
        lines.append("")
        lines.append(f"В деньгах при риске $20: {exp*20:+.2f}$ на сделку, "
                     f"на 100 сделок {exp*20*100:+.0f}$")
        need_trades = (500 / (exp * 20)) if exp > 0 else None
        if need_trades and need_trades > 0:
            lines.append(f"До цели челленджа +$500 нужно ~{need_trades:.0f} сделок")
    lines += ["", "<i>Оговорки: в спорной свече считаем стоп (пессимизм); пробой по часовой "
              "свече вместо 1м; OI не восстановлен, поэтому зарядов меньше, чем у живого бота.</i>"]

    msg = "\n".join(lines)
    print(msg.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    try:
        B.send_blocks(msg.split("\n"))
    except Exception as e:
        print(f"[BT] отправка не удалась: {e}")


def main():
    try:
        run()
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            B.send_telegram(f"⚠️ Бэктест сделок упал: {e}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
