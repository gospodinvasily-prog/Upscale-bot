"""
bt_vwap.py — VWAP как магнит: стоп не должен стоять на пути отката.

Наблюдение с живых сделок: входим в лонг, когда цена уже на 2-2.5 ATR ВЫШЕ
дневного VWAP. Цена идёт обратно к VWAP, по дороге задевает наш стоп, отбивается
от VWAP и уходит к цели. Направление было верным — убил нас стоп, оказавшийся
на пути возврата.

Отсюда три проверяемые идеи:

A. ОГРАНИЧИТЬ УДАЛЁННОСТЬ. Сейчас пускаем до 2.5 ATR от VWAP — слишком далеко.
   Чем дальше цена ушла, тем вероятнее откат раньше движения.

B. VWAP НЕ МЕЖДУ ВХОДОМ И СТОПОМ. Если он там, откат к нему почти наверняка
   снимет стоп. Требуем, чтобы VWAP был ЗА стопом либо в стороне цели.

C. СТОП ЗА VWAP. Не фиксированный процент, а ставим стоп по другую сторону
   от VWAP с запасом. Тогда откат к магниту нас не трогает.

Рядом с каждым вариантом — монетка. Без неё цифры бессмысленны.

Запуск: RUN_BACKTEST=vwap
Настройки: VW_DAYS (60), VW_PAIRS (0=все), VW_DIST (1.5)
"""
import os
import time
import statistics

import bot as B

DAYS    = int(os.environ.get("VW_DAYS", "60"))
PAIRS_N = int(os.environ.get("VW_PAIRS", "0"))
DIST    = float(os.environ.get("VW_DIST", "1.5"))
SLIP    = float(os.environ.get("VW_SLIP", "0.15"))
COST    = float(os.environ.get("VW_COST", "0.065"))
STOP_SLIP = float(os.environ.get("VW_STOP_SLIP", "0.10"))
HOLD_H  = int(os.environ.get("VW_HOLD_H", "12"))

FINE_SEC = 900
STOP, TP1, TP3 = 1.0, 1.0, 2.0
PARTS = (1 / 3, 1 / 3, 1 / 3)

FAR_GRID = [0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 99]   # максимум ATR от VWAP (99 = без лимита)
VWSTOP_MARGIN = [0.1, 0.25, 0.5]                  # запас стопа за VWAP, %


def _fetch(sym, tf, days):
    sec = {"15m": 900, "1h": 3600}[tf]
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


def _stat(rs):
    if not rs:
        return None
    n = len(rs)
    exp = sum(rs) / n
    se = (statistics.pstdev(rs) / (n ** 0.5)) if n > 1 else 0.0
    return n, exp, 1.96 * se, sum(1 for r in rs if r > 0) / n * 100


def _line(rs, label, ctl=None):
    st = _stat(rs)
    if not st:
        return f"  {label}: сделок нет"
    n, exp, ci, wr = st
    mark = "✅" if exp - ci > 0 else "❌" if exp + ci < 0 else "  "
    s = f"  {mark} {label}: {n:4} сд, ВР {wr:3.0f}%, <b>{exp:+.3f}R</b> (±{ci:.3f})"
    sc = _stat(ctl) if ctl else None
    if sc:
        s += f"  <i>монетка {sc[1]:+.3f}R, эдж {exp - sc[1]:+.3f}R</i>"
    return s


def run():
    import random as _rnd
    rng = _rnd.Random(555)
    pairs = B.UPSCALE_PAIRS[:PAIRS_N] if PAIRS_N else B.UPSCALE_PAIRS
    hold = max(6, int(HOLD_H * 3600 / FINE_SEC))
    bars_day = int(86400 / FINE_SEC)

    setups = []     # (seg, side, px, bnd, atr_pct, vwap)
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
            k = idx.get(sig_ts)
            if k is None or k + hold + 2 >= len(fine) or k < bars_day // 2:
                continue
            px = fine[k]["o"]
            is_l = c["side"] == "long"
            bnd = c["hi"] if is_l else c["lo"]
            room = (bnd - px) / px * 100 if is_l else (px - bnd) / px * 100
            if room < DIST:
                continue
            lo_i = max(0, k - bars_day)
            seg_v = fine[lo_i:k]
            vv = sum(x["v"] for x in seg_v)
            if not seg_v or vv <= 0:
                continue
            vwap = sum((x["h"] + x["l"] + x["c"]) / 3 * x["v"] for x in seg_v) / vv
            atr_pct = c["atr"] / px * 100 if px > 0 else 0
            if atr_pct <= 0:
                continue
            setups.append((fine[k:k + hold], c["side"], px, bnd, atr_pct, vwap))
            last_end = fine[min(k + hold, len(fine) - 1)].get("t", 0)
        if i % 20 == 0:
            print(f"[VWAP] {i}/{len(pairs)} | сделок {len(setups)} | {time.time()-t0:.0f}с")

    def collect(far=99, need_beyond=False, stop_at_vwap=None, coin=False):
        """far — максимум ATR от VWAP; need_beyond — VWAP должен быть ЗА стопом;
        stop_at_vwap — ставим стоп за VWAP с таким запасом в %."""
        out = []
        for seg, side, px, bnd, atr_pct, vwap in setups:
            sd = rng.choice(("long", "short")) if coin else side
            is_l = sd == "long"
            b = bnd if sd == side else (px * (1 + DIST / 100) if is_l else px * (1 - DIST / 100))
            # насколько цена ушла от VWAP, в ATR (со знаком: + в сторону сделки)
            gap_pct = (px - vwap) / px * 100 if is_l else (vwap - px) / px * 100
            gap_atr = gap_pct / atr_pct if atr_pct else 0
            if gap_atr > far:
                continue
            if stop_at_vwap is not None:
                # стоп по ДРУГУЮ сторону VWAP: откат к магниту нас не трогает
                if gap_atr <= 0:
                    continue                      # VWAP уже не на пути — правило не применимо
                stp = vwap * (1 - stop_at_vwap / 100) if is_l else vwap * (1 + stop_at_vwap / 100)
                if abs(px - stp) / px * 100 > 6:  # слишком далеко — пропускаем
                    continue
            else:
                stp = px * (1 - STOP / 100) if is_l else px * (1 + STOP / 100)
                if need_beyond:
                    # VWAP между входом и стопом — откат его снимет, пропускаем
                    between = (stp < vwap < px) if is_l else (px < vwap < stp)
                    if between:
                        continue
            ent = px * (1 + SLIP / 100) if is_l else px * (1 - SLIP / 100)
            y1 = px * (1 + TP1 / 100) if is_l else px * (1 - TP1 / 100)
            y3 = px * (1 + TP3 / 100) if is_l else px * (1 - TP3 / 100)
            r = _sim(seg, sd, ent, stp, y1, b, y3)
            if r is not None:
                out.append(r)
        return out

    L = [f"🧲 <b>VWAP КАК МАГНИТ</b> (~{cov:.0f} дн, {len(pairs)} пар)",
         "<i>наблюдение: входим далеко от VWAP, цена возвращается к нему, по пути "
         "снимает стоп, отбивается и уходит к цели — направление верное, стоп не там</i>",
         f"Зарядов: {n_ch} | сделок в выборке: {len(setups)}",
         f"Стоп {STOP}%, цели {TP1}% → граница → {TP3}%, проскальзывание {SLIP}%", ""]

    L.append("<b>A. КАК ДАЛЕКО ОТ VWAP пускать вход</b>")
    L.append("  <i>чем дальше цена ушла, тем вероятнее откат раньше движения</i>")
    for f in FAR_GRID:
        lbl = "без ограничения" if f >= 99 else f"не дальше {f} ATR от VWAP"
        L.append(_line(collect(far=f), lbl, collect(far=f, coin=True)))

    L += ["", "<b>B. VWAP НЕ МЕЖДУ ВХОДОМ И СТОПОМ</b>",
          "  <i>если он там — откат к нему снимет стоп почти наверняка</i>"]
    L.append(_line(collect(), "как сейчас (не проверяем)", collect(coin=True)))
    L.append(_line(collect(need_beyond=True), "VWAP за стопом или в стороне цели",
                   collect(need_beyond=True, coin=True)))

    L += ["", "<b>C. СТОП ЗА VWAP</b> — не процент, а по другую сторону магнита",
          "  <i>только для сделок, где VWAP на пути отката</i>"]
    for m in VWSTOP_MARGIN:
        L.append(_line(collect(stop_at_vwap=m), f"стоп за VWAP с запасом {m}%",
                       collect(stop_at_vwap=m, coin=True)))

    L += ["", "<b>D. ЛУЧШЕЕ СОЧЕТАНИЕ A+B</b>"]
    best = None
    for f in FAR_GRID:
        rs = collect(far=f, need_beyond=True)
        st = _stat(rs)
        if st and st[0] >= 60:
            cl = _stat(collect(far=f, need_beyond=True, coin=True))
            edge = st[1] - (cl[1] if cl else 0)
            lbl = "без ограничения" if f >= 99 else f"≤{f} ATR"
            L.append(_line(rs, f"{lbl} + VWAP за стопом",
                           collect(far=f, need_beyond=True, coin=True)))
            if best is None or st[1] > best[1]:
                best = (lbl, st[1], st[0], edge)
    if best:
        L.append(f"→ <b>лучшее: {best[0]} + VWAP за стопом</b> — {best[2]} сд, "
                 f"{best[1]:+.3f}R, эдж {best[3]:+.3f}R")

    msg = "\n".join(L)
    print(msg.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    try:
        B.send_blocks(msg.split("\n"))
    except Exception as e:
        print(f"[VWAP] отправка не удалась: {e}")


def main():
    try:
        run()
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            B.send_telegram(f"⚠️ Прогон VWAP упал: {e}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
