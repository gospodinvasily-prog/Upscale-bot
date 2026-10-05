"""
bt_tf.py — на каком таймфрейме искать заряд.

Прошлый прогон таймфреймов делался со сломанным таймингом (вход по цене, которая
появлялась раньше сигнала), поэтому его вывод «15м хуже часа» недействителен.
Здесь тайминг честный: заряд считается по ЗАКРЫТЫМ свечам своего ТФ, вход — по
открытию первой мелкой свечи после закрытия.

Окно коридора везде одно и то же в ЧАСАХ (12ч), меняется только размер свечи:
  15м → 48 свечей, 30м → 24, 1ч → 12, 2ч → 6, 4ч → 3.
Так сравниваются именно таймфреймы, а не разная длина коридора.

Для каждого ТФ рядом считается КОНТРОЛЬ — то же самое, но направление монеткой.
Без него любая цифра бессмысленна.

Запуск: RUN_BACKTEST=tf
Настройки: TF_DAYS (45), TF_LIST ("15m,30m,1h,2h,4h"), TF_PAIRS (0=все), TF_DIST (1.5)
"""
import os
import time
import statistics

import bot as B

DAYS    = int(os.environ.get("TF_DAYS", "45"))
PAIRS_N = int(os.environ.get("TF_PAIRS", "0"))
DIST    = float(os.environ.get("TF_DIST", "1.5"))
SLIP    = float(os.environ.get("TF_SLIP", "0.15"))
COST    = float(os.environ.get("TF_COST", "0.065"))
STOP_SLIP = float(os.environ.get("TF_STOP_SLIP", "0.10"))
HOLD_H  = int(os.environ.get("TF_HOLD_H", "12"))
TF_LIST = [t.strip() for t in os.environ.get("TF_LIST", "15m,30m,1h,2h,4h").split(",") if t.strip()]

TF_SEC = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400}
FINE = "15m"                     # на чём ведём сделку
STOP, TP1, TP3 = 1.0, 1.0, 2.0
PARTS = (1 / 3, 1 / 3, 1 / 3)
WINDOW_HOURS = 12                # коридор везде 12 часов


def _fetch(sym, tf, days):
    sec = TF_SEC[tf]
    now = int(time.time())
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


def _sim(bars, side, entry, stop, t1, t2, t3):
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
    cost = COST / 100 * entry / risk
    for c in bars:
        if (c["l"] <= cur_stop) if is_long else (c["h"] >= cur_stop):
            fill = cur_stop * (1 - STOP_SLIP / 100) if is_long else cur_stop * (1 + STOP_SLIP / 100)
            r = (fill - entry) / risk if is_long else (entry - fill) / risk
            return acc + r * sum(PARTS[done:]) - cost
        while done < 3:
            t = tg[done]
            if (c["h"] >= t) if is_long else (c["l"] <= t):
                acc += PARTS[done] * (abs(t - entry) / risk)
                done += 1
                if done == 1:
                    cur_stop = entry
                elif done == 2:
                    cur_stop = tg[0]
            else:
                break
        if done >= 3:
            return acc - cost
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
    rng = _rnd.Random(31337)
    pairs = B.UPSCALE_PAIRS[:PAIRS_N] if PAIRS_N else B.UPSCALE_PAIRS
    fine_sec = TF_SEC[FINE]
    hold = max(6, int(HOLD_H * 3600 / fine_sec))

    res = {tf: [] for tf in TF_LIST}
    ctl = {tf: [] for tf in TF_LIST}
    n_ch = {tf: 0 for tf in TF_LIST}
    t0 = time.time()
    cov = 0.0

    for i, sym in enumerate(pairs, 1):
        try:
            fine = _fetch(sym, FINE, DAYS)
        except Exception:
            continue
        if len(fine) < 400:
            continue
        fine = fine[:-1]
        if not cov:
            cov = (fine[-1].get("t", 0) - fine[0].get("t", 0)) / 86400
        idx = {c.get("t"): k for k, c in enumerate(fine)}

        for tf in TF_LIST:
            sec = TF_SEC[tf]
            win = max(4, round(WINDOW_HOURS * 3600 / sec))   # коридор 12ч на любом ТФ
            try:
                base = _fetch(sym, tf, DAYS + 5)[:-1]
            except Exception:
                continue
            if len(base) < B.BASE_FROM + win + 10:
                continue
            # профиль под этот ТФ: окно и база масштабируются
            P = dict(B.ALT_P)
            P["tf"] = tf
            last_end = 0
            saved_w = B.ACC_WINDOW
            B.ACC_WINDOW = win
            try:
                for e in range(B.BASE_FROM + win, len(base)):
                    upto = base[:e]
                    sl = upto[-B.BASE_FROM:-B.BASE_TO]
                    if len(sl) < 10:
                        continue
                    vb = B.trimmed_mean([c["v"] for c in sl])
                    ab = B.trimmed_mean(B.true_ranges(upto)[-B.BASE_FROM:-B.BASE_TO])
                    if not vb or not ab or vb <= 0 or ab <= 0:
                        continue
                    cts = upto[-1].get("t", 0)
                    sig_ts = cts + sec                 # свеча ЗАКРЫЛАСЬ — сигнал появился
                    if sig_ts <= last_end:
                        continue
                    c = B.detect_charge(sym, upto, upto[-1]["c"], vb, ab,
                                        {"btc_chg_win": 0.0, "do_charge": True},
                                        {"funding": 0.0, "change_24h": 0.0},
                                        lambda s: None, P=P)
                    if not c or c["side"] not in ("long", "short"):
                        continue
                    n_ch[tf] += 1
                    # вход по первой мелкой свече после закрытия
                    k = idx.get(sig_ts - sig_ts % fine_sec)
                    if k is None:
                        k = idx.get(sig_ts)
                    if k is None or k + hold + 2 >= len(fine):
                        continue
                    px = fine[k]["o"]
                    is_l = c["side"] == "long"
                    bnd = c["hi"] if is_l else c["lo"]
                    room = (bnd - px) / px * 100 if is_l else (px - bnd) / px * 100
                    if room < DIST:
                        continue
                    seg = fine[k:k + hold]
                    ent = px * (1 + SLIP / 100) if is_l else px * (1 - SLIP / 100)
                    stp = px * (1 - STOP / 100) if is_l else px * (1 + STOP / 100)
                    y1 = px * (1 + TP1 / 100) if is_l else px * (1 - TP1 / 100)
                    y3 = px * (1 + TP3 / 100) if is_l else px * (1 - TP3 / 100)
                    r = _sim(seg, c["side"], ent, stp, y1, bnd, y3)
                    if r is None:
                        continue
                    res[tf].append(r)
                    last_end = seg[-1].get("t", 0)
                    # контроль: то же, но направление монеткой
                    sd = rng.choice(("long", "short"))
                    isr = sd == "long"
                    bnr = c["hi"] if isr else c["lo"]
                    rr = _sim(seg, sd,
                              px * (1 + SLIP / 100) if isr else px * (1 - SLIP / 100),
                              px * (1 - STOP / 100) if isr else px * (1 + STOP / 100),
                              px * (1 + TP1 / 100) if isr else px * (1 - TP1 / 100),
                              bnr,
                              px * (1 + TP3 / 100) if isr else px * (1 - TP3 / 100))
                    if rr is not None:
                        ctl[tf].append(rr)
            finally:
                B.ACC_WINDOW = saved_w
        if i % 10 == 0:
            print(f"[TF] {i}/{len(pairs)} | {time.time()-t0:.0f}с | "
                  + " ".join(f"{tf}:{len(res[tf])}" for tf in TF_LIST))

    L = [f"⏱ <b>НА КАКОМ ТАЙМФРЕЙМЕ ИСКАТЬ ЗАРЯД</b> (~{cov:.0f} дн, {len(pairs)} пар)",
         f"<i>коридор везде {WINDOW_HOURS}ч, меняется только размер свечи. "
         f"Тайминг честный: свеча закрылась → вход по следующей {FINE}</i>",
         f"Ход до границы ≥{DIST}%, стоп {STOP}%, три цели по трети, "
         f"проскальзывание {SLIP}%, издержки {COST}%",
         f"Время {(time.time()-t0)/60:.1f} мин", ""]
    for tf in TF_LIST:
        win = max(4, round(WINDOW_HOURS * 3600 / TF_SEC[tf]))
        L.append(f"<b>{tf}</b> (коридор {win} свечей, зарядов найдено {n_ch[tf]}):")
        L.append(_line(res[tf], "наш сигнал"))
        L.append(_line(ctl[tf], "монетка"))
        if res[tf] and ctl[tf]:
            d = sum(res[tf]) / len(res[tf]) - sum(ctl[tf]) / len(ctl[tf])
            L.append(f"     <i>сигнал лучше монетки на {d:+.3f}R</i>")
        L.append("")
    best = max((tf for tf in TF_LIST if len(res[tf]) >= 80),
               key=lambda tf: sum(res[tf]) / len(res[tf]), default=None)
    if best:
        L.append(f"→ <b>лучший ТФ: {best}</b> — {len(res[best])} сд, "
                 f"{sum(res[best])/len(res[best]):+.3f}R")
    else:
        L.append("→ ни на одном ТФ не набралось 80 сделок для вывода")

    msg = "\n".join(L)
    print(msg.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    try:
        B.send_blocks(msg.split("\n"))
    except Exception as e:
        print(f"[TF] отправка не удалась: {e}")


def main():
    try:
        run()
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            B.send_telegram(f"⚠️ Прогон таймфреймов упал: {e}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
