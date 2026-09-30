"""
bt_trades.py — полноценный бэктест СДЕЛОК (не сигналов).

Отвечает на главный вопрос: есть ли у стратегии положительное матожидание
после комиссий и проскальзывания.

Чем отличается от bt_range.py: тот мерил «вышла ли цена из коридора».
Этот прогоняет сделку по свечам от входа до выхода и смотрит, что случилось
РАНЬШЕ — стоп или тейк. Именно порядок событий решает, плюс система или минус.

Запуск на Render: RUN_BACKTEST=trades
Настройки: BT_DAYS (60), BT_PAIRS (0 = все), BT_STEP (2), BT_FEE_PCT (0.05)

ЧЕСТНЫЕ ОГРАНИЧЕНИЯ (читать обязательно):
1. Внутрисвечная неоднозначность. Если в одной свече задеты и стоп, и тейк —
   мы не знаем, что было первым. Считаем СТОП (пессимистично). Это занижает
   результат, но не даёт себя обмануть.
2. Подтверждение пробоя. Живой бот ждёт закрытия 1-минутной свечи за уровнем
   с объёмом ≥2×. На истории часовых свечей 1м нет, поэтому пробой = часовая
   свеча закрылась за уровнем при объёме ≥2× нормы. Это приближение.
3. OI не восстанавливается за историю — ветка «объём ИЛИ рост OI» работает
   только по объёму. Значит зарядов в бэктесте МЕНЬШЕ, чем у живого бота.
4. Фильтры дня (1 монета в день, лимиты сигналов, дневной стоп) не моделируются —
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
MAX_HOLD_H = int(os.environ.get("BT_MAX_HOLD_H", "12"))  # держим не дольше горизонта оценки


def _baseline(upto):
    sl = upto[-B.BASE_FROM:-B.BASE_TO]
    if len(sl) < 10:
        return None, None
    vol = B.trimmed_mean([c["v"] for c in sl])
    atr = B.trimmed_mean(B.true_ranges(upto)[-B.BASE_FROM:-B.BASE_TO])
    return (vol, atr) if vol and atr and vol > 0 and atr > 0 else (None, None)


def _find_charge(upto, price, vol_base, atr_norm):
    """Заряд по текущим правилам бота. OI недоступен — передаём пустую статистику."""
    return B.detect_charge("BT", upto, price, vol_base, atr_norm,
                           {"btc_chg_win": 0.0, "do_charge": True},
                           {"funding": 0.0, "change_24h": 0.0},
                           lambda s: None, P=B.ALT_P)


def _simulate(future, side, entry, stop, tp1, tp2, fee_pct):
    """Прогон сделки по свечам. Возвращает результат в R (единицах риска) после комиссии.
    Половина позиции закрывается на TP1, остаток — на TP2 или по стопу.
    После TP1 стоп переносится в безубыток (как делает живой бот)."""
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    is_long = side == "long"
    half_done = False
    cur_stop = stop
    realized = 0.0          # в R, по закрытой половине

    for c in future:
        hit_stop = (c["l"] <= cur_stop) if is_long else (c["h"] >= cur_stop)
        tgt = tp1 if not half_done else tp2
        hit_tp = (c["h"] >= tgt) if is_long else (c["l"] <= tgt)

        # пессимистично: если в свече задеты оба — считаем стоп
        if hit_stop:
            part = 0.5 if half_done else 1.0
            r = (cur_stop - entry) / risk if is_long else (entry - cur_stop) / risk
            realized += r * part
            return realized - fee_pct / 100 * entry / risk

        if hit_tp:
            if not half_done:
                realized += 0.5 * (abs(tp1 - entry) / risk)
                half_done = True
                cur_stop = entry          # безубыток на остаток
            else:
                realized += 0.5 * (abs(tp2 - entry) / risk)
                return realized - fee_pct / 100 * entry / risk

    # вышли по времени — закрываем по последней цене
    last = future[-1]["c"] if future else entry
    part = 0.5 if half_done else 1.0
    r = (last - entry) / risk if is_long else (entry - last) / risk
    realized += r * part
    return realized - fee_pct / 100 * entry / risk


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

    cur_rs, old_rs = [], []
    charges = entries = 0
    skipped_chase = 0
    t0 = time.time()

    for i, sym in enumerate(pairs, 1):
        try:
            candles = B.get_candles(sym, B.CHARGE_TF, need)
        except Exception:
            continue
        if not candles or len(candles) < B.BASE_FROM + W + hold + 10:
            continue
        closed = candles[:-1]
        last_entry_idx = -10 ** 9

        for end in range(B.BASE_FROM + W, len(closed) - hold, BT_STEP):
            upto = closed[:end]
            vol_base, atr_norm = _baseline(upto)
            if not vol_base:
                continue
            price = upto[-1]["c"]
            c = _find_charge(upto, price, vol_base, atr_norm)
            if not c:
                continue
            charges += 1

            # ждём пробоя в течение жизни заряда
            for k in range(end, min(end + watch, len(closed) - hold)):
                bar = closed[k]
                rvol = bar["v"] / vol_base if vol_base else 0
                if rvol < B.BREAK_MIN_RVOL:
                    continue
                for side in ("long", "short"):
                    trig = B.order_trigger(c["hi"] if side == "long" else c["lo"],
                                           side == "long", c["atr"])
                    broke = bar["c"] > trig if side == "long" else bar["c"] < trig
                    if not broke:
                        continue
                    if k - last_entry_idx < watch // 2:     # грубый аналог кулдауна
                        continue
                    entry = bar["c"]
                    lvl = c["hi"] if side == "long" else c["lo"]
                    if c["atr"] > 0 and abs(entry - lvl) / c["atr"] > 0.3:
                        skipped_chase += 1
                        break                                # догоняем — пропускаем
                    w = dict(c)
                    # build_breakout ждёт поля из записи watchlist — добиваем нейтральными
                    for k_, v_ in (("created", 0), ("funding", 0.0), ("change_24h", 0.0),
                                   ("oi_win", None), ("oi_15m", None), ("P", B.ALT_P),
                                   ("swing_highs", []), ("swing_lows", [])):
                        w.setdefault(k_, v_)
                    bo = B.build_breakout(w, side, entry, 0.0, None)
                    fut = closed[k + 1:k + 1 + hold]
                    if not fut:
                        break
                    r = _simulate(fut, side, entry, bo["stop"], bo["tp1_price"], bo["tp2_price"], FEE_PCT)
                    if r is None:
                        break
                    cur_rs.append(r)
                    entries += 1
                    last_entry_idx = k
                    # тот же вход, но СТАРЫЕ цели v8.3 (TP1 1R, TP2 2R) — для сравнения
                    dist = abs(entry - bo["stop"]) / entry * 100
                    if side == "long":
                        o1, o2 = entry * (1 + dist / 100), entry * (1 + 2 * dist / 100)
                    else:
                        o1, o2 = entry * (1 - dist / 100), entry * (1 - 2 * dist / 100)
                    r_old = _simulate(fut, side, entry, bo["stop"], o1, o2, FEE_PCT)
                    if r_old is not None:
                        old_rs.append(r_old)
                    break
                else:
                    continue
                break
        if i % 20 == 0:
            print(f"[BT] {i}/{len(pairs)} пар | зарядов {charges} | сделок {entries} | {time.time()-t0:.0f}с")

    took = time.time() - t0
    lines = [
        f"💰 <b>Бэктест СДЕЛОК</b> ({B.CHARGE_TF}, {BT_DAYS} дн, {len(pairs)} пар)",
        f"Зарядов {charges} → сделок <b>{entries}</b>, комиссия {FEE_PCT}% за круг, "
        f"время {took/60:.1f} мин",
        f"Пропущено из-за погони за ценой: {skipped_chase}",
        "",
        "<b>Результат в R (1R = риск на сделку, у тебя $20):</b>",
    ]
    lines += _stats(cur_rs, "текущие цели (TP1 0.5R, TP2 1R)")
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
