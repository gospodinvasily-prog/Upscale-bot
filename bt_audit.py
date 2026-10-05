"""
bt_audit.py — аудит ДОПУЩЕНИЙ бэктеста, а не стратегии.

Зачем. После исправления подглядывания результат стал отрицательным, но живые
55 сделок дали +0.185R. Расхождение надо объяснить, а не замести. Проверяем,
какое из допущений модели переворачивает знак.

Проверяются четыре независимых допущения, каждое — отдельной сеткой:

1. ВНУТРИ СВЕЧИ. Если 15м свеча накрыла и стоп, и цель, что случилось раньше?
   Сейчас модель ВСЕГДА считает, что стоп. Это самый пессимистичный вариант,
   и на 15м свечах он может систематически занижать результат.
   Варианты: всегда стоп / всегда цель / 50 на 50 / по направлению свечи.

2. ПРОСКАЛЬЗЫВАНИЕ ВХОДА. В модели 0.25%, а живая сделка показала 0.10%.
   Это важно: стоп и цели считаются от цены СИГНАЛА, а входим мы хуже,
   поэтому проскальзывание напрямую портит соотношение риска к прибыли.
   При 0.25% до первой цели остаётся 0.75% при риске 1.25% — это 0.6R вместо 1R.

3. МОМЕНТ ВХОДА. Живой бот входит сразу на закрытии часа. bt_entry ждал 30 минут.
   Проверяем обе задержки: 0, 15, 30, 45 минут.

4. СТОП-ПРОСКАЛЬЗЫВАНИЕ И ФАНДИНГ. Добавлены недавно по чужому замечанию.
   Смотрим, сколько они стоят.

Запуск: RUN_BACKTEST=audit
Настройки: AU_DAYS (60), AU_PAIRS (0=все), AU_OFFSET (0)
"""
import os
import time
import statistics

import bot as B

AMBIG = [0]      # счётчик сделок, где решающая свеча накрыла и стоп, и цель

DAYS    = int(os.environ.get("AU_DAYS", "60"))
OFFSET  = int(os.environ.get("AU_OFFSET", "0"))
PAIRS_N = int(os.environ.get("AU_PAIRS", "0"))
HOLD_H  = int(os.environ.get("AU_HOLD_H", "12"))

# Таймфрейм ВЕДЕНИЯ сделки. Чем мельче свеча, тем реже она накрывает и стоп,
# и цель разом — а значит меньше гадания о том, что сработало первым.
# На 15м неоднозначность велика, на 5м и 1м почти исчезает.
MANAGE_TFS = [t.strip() for t in os.environ.get("AU_TFS", "15m,5m,1m").split(",") if t.strip()]
TF_SEC = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}
STEP = 900
STOP, TP1, TP3 = 1.0, 1.0, 2.0
PARTS = (1 / 3, 1 / 3, 1 / 3)
DIST = float(os.environ.get("AU_DIST", "1.5"))

INTRABAR = ["всегда стоп (как сейчас)", "всегда цель", "50 на 50", "по направлению свечи"]
SLIP_GRID = [0.0, 0.10, 0.25]
DELAY_GRID = [0, 15, 30, 45]


def _fetch(sym, tf, days):
    sec = TF_SEC[tf]
    now = int(time.time()) - OFFSET * 86400
    out, cur = [], now - days * 86400
    while cur < now:
        to = min(now, cur + 1900 * sec)
        raw = B.api_get("candlesticks", {"contract": f"{sym}_USDT", "interval": tf,
                                         "from": cur, "to": to})
        part = B.parse_candles(raw) if raw else []
        if not part:
            break
        out.extend(part)
        nxt = part[-1].get("t", 0) + sec
        if nxt <= cur:
            break
        cur = nxt
    seen, uniq = set(), []
    for c in sorted(out, key=lambda x: x.get("t", 0)):
        if c.get("t") not in seen:
            seen.add(c.get("t"))
            uniq.append(c)
    return uniq


def _sim(bars, side, entry, stop, t1, t2, t3, intrabar="всегда стоп (как сейчас)",
         stop_slip=0.0, cost_pct=0.0, rng=None):
    """Один прогон сделки. intrabar решает, что считать первым, когда свеча
    накрыла и стоп, и цель."""
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    is_long = side == "long"
    tg = [t1, t2, t3]
    for i in range(1, 3):
        if is_long and tg[i] <= tg[i - 1]:
            tg[i] = tg[i - 1] * 1.001
        if not is_long and tg[i] >= tg[i - 1]:
            tg[i] = tg[i - 1] * 0.999
    done, cur_stop, acc = 0, stop, 0.0
    cost = cost_pct / 100 * entry / risk

    def _hit_stop(c):
        return (c["l"] <= cur_stop) if is_long else (c["h"] >= cur_stop)

    def _hit_target(c):
        t = tg[done]
        return (c["h"] >= t) if is_long else (c["l"] <= t)

    for c in bars:
        s_hit, t_hit = _hit_stop(c), (done < 3 and _hit_target(c))
        if s_hit and t_hit:
            AMBIG[0] += 1          # решающая свеча накрыла оба уровня
        stop_first = True
        if s_hit and t_hit:
            if intrabar == "всегда цель":
                stop_first = False
            elif intrabar == "50 на 50":
                stop_first = (rng.random() < 0.5) if rng else True
            elif intrabar == "по направлению свечи":
                # зелёная свеча: сначала低 потом высоко → для лонга стоп раньше
                up = c["c"] >= c["o"]
                stop_first = up if is_long else (not up)
        if s_hit and stop_first:
            fill = cur_stop * (1 - stop_slip / 100) if is_long else cur_stop * (1 + stop_slip / 100)
            r = (fill - entry) / risk if is_long else (entry - fill) / risk
            return acc + r * sum(PARTS[done:]) - cost
        while done < 3 and _hit_target(c):
            acc += PARTS[done] * (abs(tg[done] - entry) / risk)
            done += 1
            if done == 1:
                cur_stop = entry
            elif done == 2:
                cur_stop = tg[0]
        if done >= 3:
            return acc - cost
        if s_hit:                      # цель взяли, но стоп в этой же свече тоже задет
            fill = cur_stop * (1 - stop_slip / 100) if is_long else cur_stop * (1 + stop_slip / 100)
            r = (fill - entry) / risk if is_long else (entry - fill) / risk
            return acc + r * sum(PARTS[done:]) - cost
    last = bars[-1]["c"] if bars else entry
    r = (last - entry) / risk if is_long else (entry - last) / risk
    return acc + r * sum(PARTS[done:]) - cost


def _line(rs, label):
    if not rs:
        return f"  {label}: сделок нет"
    n = len(rs)
    exp = sum(rs) / n
    wins = sum(1 for r in rs if r > 0)
    se = (statistics.pstdev(rs) / (n ** 0.5)) if n > 1 else 0.0
    mark = "✅" if exp - 1.96 * se > 0 else "❌" if exp + 1.96 * se < 0 else "  "
    return (f"  {mark} {label}: {n:4} сд, ВР {wins/n*100:3.0f}%, "
            f"<b>{exp:+.3f}R</b> (±{1.96*se:.3f})")


def run():
    import random as _rnd
    rng = _rnd.Random(4242)
    pairs = B.UPSCALE_PAIRS[:PAIRS_N] if PAIRS_N else B.UPSCALE_PAIRS
    hold = max(6, int(HOLD_H * 3600 / STEP))

    # собираем СДЕЛКИ один раз по часовым, свечи ведения — отдельно по каждому ТФ
    setups = []
    cache = {}          # (sym, tf) -> (свечи, индекс)
    n_ch = 0
    t0 = time.time()
    cov = 0.0

    for i, sym in enumerate(pairs, 1):
        try:
            fine = _fetch(sym, "15m", DAYS)
            base = _fetch(sym, "1h", DAYS + 5)
        except Exception:
            continue
        if len(fine) < 500 or len(base) < 150:
            continue
        fine, base = fine[:-1], base[:-1]
        if not cov:
            cov = (fine[-1].get("t", 0) - fine[0].get("t", 0)) / 86400
        idx = {c.get("t"): k for k, c in enumerate(fine)}
        cache[(sym, "15m")] = (fine, idx)
        for tf in MANAGE_TFS:
            if tf == "15m":
                continue
            try:
                ff = _fetch(sym, tf, DAYS)[:-1]
            except Exception:
                continue
            if len(ff) > 100:
                cache[(sym, tf)] = (ff, {c.get("t"): k for k, c in enumerate(ff)})
        last_end = 0
        for e in range(B.BASE_FROM + B.ACC_WINDOW, len(base)):
            upto = base[:e]
            sl = upto[-B.BASE_FROM:-B.BASE_TO]
            if len(sl) < 10:
                continue
            vb = B.trimmed_mean([c["v"] for c in sl])
            ab = B.trimmed_mean(B.true_ranges(upto)[-B.BASE_FROM:-B.BASE_TO])
            if not vb or not ab or vb <= 0 or ab <= 0:
                continue
            cts = upto[-1].get("t", 0)
            sig_ts = cts + 3600
            if sig_ts <= last_end:
                continue
            c = B.detect_charge(sym, upto, upto[-1]["c"], vb, ab,
                                {"btc_chg_win": 0.0, "do_charge": True},
                                {"funding": 0.0, "change_24h": 0.0},
                                lambda s: None, P=B.ALT_P)
            if not c or c["side"] not in ("long", "short"):
                continue
            n_ch += 1
            setups.append((sym, sig_ts, c["side"], c["hi"], c["lo"]))
            k = idx.get(sig_ts)
            if k is not None and k + hold + 4 < len(fine):
                last_end = fine[min(k + hold, len(fine) - 1)].get("t", 0)
        if i % 20 == 0:
            print(f"[AUDIT] {i}/{len(pairs)} | зарядов {n_ch} | {time.time()-t0:.0f}с")
    def collect(intrabar="всегда стоп (как сейчас)", slip=0.25, delay=0,
                stop_slip=0.15, cost=0.065, tf="15m"):
        out = []
        sec = TF_SEC[tf]
        hold_n = max(6, int(HOLD_H * 3600 / sec))
        for sym, sig_ts, side, hi, lo in setups:
            fi = cache.get((sym, tf))
            if not fi:
                continue
            fine, idx = fi
            k = idx.get(sig_ts + delay * 60)
            if k is None or k + hold_n + 2 >= len(fine):
                continue
            px = fine[k]["o"]
            is_l = side == "long"
            bnd = hi if is_l else lo
            room = (bnd - px) / px * 100 if is_l else (px - bnd) / px * 100
            if room < DIST:
                continue
            ent = px * (1 + slip / 100) if is_l else px * (1 - slip / 100)
            stp = px * (1 - STOP / 100) if is_l else px * (1 + STOP / 100)
            y1 = px * (1 + TP1 / 100) if is_l else px * (1 - TP1 / 100)
            y3 = px * (1 + TP3 / 100) if is_l else px * (1 - TP3 / 100)
            seg = fine[k:k + hold_n]
            r = _sim(seg, side, ent, stp, y1, bnd, y3,
                     intrabar=intrabar, stop_slip=stop_slip, cost_pct=cost, rng=rng)
            if r is not None:
                out.append(r)
        return out

    L = [f"🔬 <b>АУДИТ ДОПУЩЕНИЙ БЭКТЕСТА</b> (~{cov:.0f} дн, {len(pairs)} пар, "
         f"ход ≥{DIST}%)",
         "<i>одни и те же сделки через разные допущения — ищем, что переворачивает знак</i>",
         f"Зарядов с направлением: {n_ch}", ""]

    L.append("<b>0. ТАЙМФРЕЙМ ВЕДЕНИЯ — чем мельче свеча, тем меньше гадания</b>")
    L.append("  <i>на крупной свече стоп и цель часто попадают в одну — модель вынуждена "
             "угадывать, что было раньше</i>")
    for tf in MANAGE_TFS:
        AMBIG[0] = 0
        base_r = collect(tf=tf)
        n_amb = AMBIG[0]
        opt_r = collect(tf=tf, intrabar="всегда цель")
        L.append(_line(base_r, f"{tf}, стоп первым (как сейчас)"))
        L.append(_line(opt_r, f"{tf}, цель первой"))
        if base_r:
            spread = abs((sum(opt_r) / len(opt_r) if opt_r else 0) - sum(base_r) / len(base_r))
            L.append(f"     <i>сделок с неоднозначностью: {n_amb} из {len(base_r)} "
                     f"({n_amb/len(base_r)*100:.0f}%) — цена допущения {spread:.3f}R</i>")
    L.append("")
    L.append("<b>1. ВНУТРИ СВЕЧИ: что раньше, стоп или цель?</b>")
    L.append("  <i>если 15м свеча накрыла оба уровня. Сейчас модель всегда выбирает стоп</i>")
    for ib in INTRABAR:
        L.append(_line(collect(intrabar=ib), ib))

    L += ["", "<b>2. ПРОСКАЛЬЗЫВАНИЕ ВХОДА</b>",
          "  <i>стоп и цели считаются от цены СИГНАЛА, вход хуже — это съедает RR. "
          "Живая сделка показала 0.10%</i>"]
    for sp in SLIP_GRID:
        L.append(_line(collect(slip=sp), f"проскальзывание {sp}%"))

    L += ["", "<b>3. ЗАДЕРЖКА ВХОДА после закрытия часа</b>",
          "  <i>живой бот входит сразу (0 мин)</i>"]
    for dl in DELAY_GRID:
        L.append(_line(collect(delay=dl), f"задержка {dl} мин"))

    L += ["", "<b>4. ЦЕНА ДОПУЩЕНИЙ, добавленных последними</b>"]
    L.append(_line(collect(stop_slip=0.0, cost=0.0), "без стоп-проскальзывания и комиссий"))
    L.append(_line(collect(stop_slip=0.0), "без стоп-проскальзывания"))
    L.append(_line(collect(), "как сейчас (стоп-слип 0.15%, издержки 0.065%)"))

    L += ["", "<b>5. САМЫЙ БЛАГОПРИЯТНЫЙ НАБОР</b> — верхняя граница возможного",
          "  <i>цель раньше стопа, проскальзывание 0.10%, без задержки, без стоп-слипа</i>"]
    L.append(_line(collect(intrabar="всегда цель", slip=0.10, delay=0,
                           stop_slip=0.0, cost=0.065), "лучший случай"))
    L.append(_line(collect(intrabar="50 на 50", slip=0.10, delay=0,
                           stop_slip=0.10, cost=0.065), "реалистичный случай"))

    msg = "\n".join(L)
    print(msg.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    try:
        B.send_blocks(msg.split("\n"))
    except Exception as e:
        print(f"[AUDIT] отправка не удалась: {e}")


def main():
    try:
        run()
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            B.send_telegram(f"⚠️ Аудит упал: {e}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
