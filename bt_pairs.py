"""
bt_pairs.py — разбор ПО ПАРАМ и по группам пар.

Зачем. Общая средняя около −0.2R может скрывать разные пары: где-то система в плюсе,
где-то глубоко в минусе, а среднее это смешивает. Проверяем три вещи:

  1. Таблица по каждой паре: сделок, винрейт, средняя.
  2. Группы по волатильности (ATR в % от цены). Если «уставки» работают только на
     спокойных или только на дёрганых парах — это видно здесь.
  3. Персональный стоп: для каждой группы перебираем стоп в % и в ATR.

ГЛАВНАЯ ОПАСНОСТЬ — отбор задним числом. Если взять 103 пары и оставить лучшие,
они будут лучшими и при полностью случайных данных. Поэтому:
  • рядом с каждой парой считается МОНЕТКА на тех же сделках;
  • период делится пополам: первая половина — отбор, вторая — ПРОВЕРКА.
    Пара попадает в «подтверждённые» только если она в плюсе в ОБЕИХ половинах.
  • показывается, сколько пар прошло бы такой отбор на случайных данных.

Запуск: RUN_BACKTEST=pairs
Настройки: BP_DAYS (60), BP_PAIRS (0=все), BP_DIST (1.5), BP_MIN_TRADES (12)
"""
import os
import time
import statistics

import bot as B

DAYS       = int(os.environ.get("BP_DAYS", "60"))
OFFSET     = int(os.environ.get("BP_OFFSET", "0"))
PAIRS_N    = int(os.environ.get("BP_PAIRS", "0"))
DIST       = float(os.environ.get("BP_DIST", "1.5"))
MIN_TRADES = int(os.environ.get("BP_MIN_TRADES", "12"))
SLIP       = float(os.environ.get("BP_SLIP", "0.10"))
COST       = float(os.environ.get("BP_COST", "0.065"))
STOP_SLIP  = float(os.environ.get("BP_STOP_SLIP", "0.05"))
HOLD_H     = int(os.environ.get("BP_HOLD_H", "12"))
BTC_MARGIN = float(os.environ.get("BP_BTC_MARGIN", "0.2"))
ENTRY_MINS = [0, 30]

FINE_SEC = 900
STOP, TP1, TP3 = 1.0, 1.0, 2.0
PARTS = (1 / 3, 1 / 3, 1 / 3)
VW_BARS = 97

STOP_GRID = [0.75, 1.0, 1.5, 2.0, 3.0]        # персональный стоп, % от цены
ATR_GRID  = [1.0, 1.5, 2.0, 3.0]              # персональный стоп, во сколько ATR


def _fetch(sym, tf, days):
    sec = {"15m": 900, "1h": 3600}[tf]
    now = int(time.time()) - OFFSET * 86400
    out, cur, probes = [], now - days * 86400, 0
    while cur < now:
        to = min(now, cur + 1900 * sec)
        raw = B.api_get("candlesticks", {"contract": f"{sym}_USDT", "interval": tf,
                                         "from": cur, "to": to})
        part = B.parse_candles(raw) if raw else []
        if not part:
            probes += 1
            if probes > 40:
                break
            cur += 5 * 86400
            continue
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


def _vwap(win):
    vol = sum(x["v"] for x in win)
    if not win or vol <= 0:
        return None
    return sum((x["h"] + x["l"] + x["c"]) / 3 * x["v"] for x in win) / vol


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
    e = sum(rs) / n
    se = (statistics.pstdev(rs) / (n ** 0.5)) if n > 1 else 0.0
    return n, e, 1.96 * se, sum(1 for r in rs if r > 0) / n * 100


def run():
    import random as _rnd
    rng = _rnd.Random(20251005)
    pairs = B.UPSCALE_PAIRS[:PAIRS_N] if PAIRS_N else B.UPSCALE_PAIRS
    hold = max(6, int(HOLD_H * 3600 / FINE_SEC))

    # ── уклон BTC по его VWAP ──
    btc15 = _fetch("BTC", "15m", DAYS + 2)
    vw_dev = {}
    for k in range(VW_BARS, len(btc15)):
        vw = _vwap(btc15[max(0, k - VW_BARS + 1):k + 1])
        if vw:
            vw_dev[btc15[k].get("t", 0) + FINE_SEC] = (btc15[k]["c"] - vw) / vw * 100

    def btc_side(ts):
        t = ts - ts % FINE_SEC
        for back in range(0, 8):
            v = vw_dev.get(t - back * FINE_SEC)
            if v is not None:
                return "long" if v > BTC_MARGIN else "short" if v < -BTC_MARGIN else "neutral"
        return None

    # ── сделки по каждой паре ──
    per = {}            # sym -> список (ts, side, px, bnd, atr_pct, seg)
    t0 = time.time()
    cov = 0.0
    for i, sym in enumerate(pairs, 1):
        if sym == "BTC":
            continue
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
            if cts + 3600 <= last_end:
                continue
            c = B.detect_charge(sym, upto, upto[-1]["c"], vb, ab,
                                {"btc_chg_win": 0.0, "do_charge": True},
                                {"funding": 0.0, "change_24h": 0.0},
                                lambda s: None, P=B.ALT_P)
            if not c:
                continue
            for em in ENTRY_MINS:
                ts = cts + 3600 + em * 60
                k = idx.get(ts)
                if k is None or k + hold + 2 >= len(fine) or k < VW_BARS + 2:
                    continue
                px = fine[k]["o"]
                bs = btc_side(ts)
                if bs is None or bs == "neutral":
                    continue
                side = c["side"] if c["side"] in ("long", "short") else bs
                if side != bs:
                    continue
                vw = _vwap(fine[k - VW_BARS:k])          # VWAP пары по закрытым свечам
                if not vw:
                    continue
                pside = "long" if fine[k - 1]["c"] > vw else "short"
                if pside != side:                        # фильтр v10.3: сторона VWAP пары
                    continue
                is_l = side == "long"
                bnd = c["hi"] if is_l else c["lo"]
                room = (bnd - px) / px * 100 if is_l else (px - bnd) / px * 100
                if room < DIST:
                    continue
                atr_pct = c["atr"] / px * 100 if px > 0 else 0
                per.setdefault(sym, []).append((ts, side, px, bnd, atr_pct,
                                                fine[k:k + hold]))
                last_end = fine[min(k + hold, len(fine) - 1)].get("t", 0)
                break
        if i % 20 == 0:
            tot = sum(len(v) for v in per.values())
            print(f"[PAIRS] {i}/{len(pairs)} | пар с сделками {len(per)} | сделок {tot} | "
                  f"{time.time()-t0:.0f}с")

    def run_trade(rec, stop_pct=None, atr_k=None, coin=False):
        ts, side, px, bnd, atr_pct, seg = rec
        sd = rng.choice(("long", "short")) if coin else side
        is_l = sd == "long"
        b = bnd if sd == side else (px * (1 + DIST / 100) if is_l else px * (1 - DIST / 100))
        sp = (atr_k * atr_pct) if atr_k else (stop_pct or STOP)
        if sp <= 0.05 or sp > 15:
            return None
        ent = px * (1 + SLIP / 100) if is_l else px * (1 - SLIP / 100)
        stp = px * (1 - sp / 100) if is_l else px * (1 + sp / 100)
        y1 = px * (1 + TP1 / 100) if is_l else px * (1 - TP1 / 100)
        y3 = px * (1 + TP3 / 100) if is_l else px * (1 - TP3 / 100)
        return _sim(seg, sd, ent, stp, y1, b, y3)

    all_recs = [(s, r) for s, v in per.items() for r in v]
    all_recs.sort(key=lambda x: x[1][0])
    if not all_recs:
        B.send_telegram("⚠️ bt_pairs: сделок не набралось")
        return
    mid_ts = all_recs[len(all_recs) // 2][1][0]

    L = [f"🧩 <b>РАЗБОР ПО ПАРАМ</b> (~{cov:.0f} дн, {len(pairs)} пар)"
         + (f"\n⏪ период сдвинут назад на {OFFSET} дней" if OFFSET else ""),
         "<i>настройки как в боте: заряд 1ч, фильтр BTC, сторона VWAP пары, "
         f"ход ≥{DIST}%, стоп {STOP}%, цели по трети</i>",
         f"Сделок всего: {len(all_recs)} | пар с сделками: {len(per)}",
         f"Проскальзывание {SLIP}%, издержки {COST}%", ""]

    # ── 1. группы по волатильности ──
    vol_of = {s: statistics.median([r[4] for r in v]) for s, v in per.items() if v}
    groups = [("спокойные (ATR < 0.6%)", lambda a: a < 0.6),
              ("средние (0.6–1.0%)", lambda a: 0.6 <= a < 1.0),
              ("живые (1.0–1.5%)", lambda a: 1.0 <= a < 1.5),
              ("дёрганые (≥1.5%)", lambda a: a >= 1.5)]
    L.append("<b>1. ГРУППЫ ПО ВОЛАТИЛЬНОСТИ</b> (ATR пары в % от цены, медиана)")
    for gname, cond in groups:
        syms = [s for s, a in vol_of.items() if cond(a)]
        rs = [run_trade(r) for s in syms for r in per[s]]
        rs = [x for x in rs if x is not None]
        cl = [run_trade(r, coin=True) for s in syms for r in per[s]]
        cl = [x for x in cl if x is not None]
        st, sc = _stat(rs), _stat(cl)
        if not st:
            L.append(f"  {gname}: сделок нет")
            continue
        mark = "✅" if st[1] - st[2] > 0 else "❌" if st[1] + st[2] < 0 else "  "
        L.append(f"  {mark} {gname}: {len(syms)} пар, {st[0]:4} сд, ВР {st[3]:3.0f}%, "
                 f"<b>{st[1]:+.3f}R</b> (±{st[2]:.3f}) | монетка {sc[1]:+.3f}R" if sc else "")
    L.append("")

    # ── 2. персональный стоп по группам ──
    L.append("<b>2. ПЕРСОНАЛЬНЫЙ СТОП ПО ГРУППАМ</b>")
    L.append("  <i>цели не меняются (1% → граница → 2%), меняется только стоп</i>")
    for gname, cond in groups:
        syms = [s for s, a in vol_of.items() if cond(a)]
        recs = [r for s in syms for r in per[s]]
        if len(recs) < 30:
            continue
        best = None
        line = []
        for sp in STOP_GRID:
            rs = [x for x in (run_trade(r, stop_pct=sp) for r in recs) if x is not None]
            st = _stat(rs)
            if st:
                line.append(f"{sp}%: {st[1]:+.2f}")
                if best is None or st[1] > best[1]:
                    best = (f"{sp}%", st[1], st[0])
        for ak in ATR_GRID:
            rs = [x for x in (run_trade(r, atr_k=ak) for r in recs) if x is not None]
            st = _stat(rs)
            if st:
                line.append(f"{ak}×ATR: {st[1]:+.2f}")
                if best is None or st[1] > best[1]:
                    best = (f"{ak}×ATR", st[1], st[0])
        L.append(f"  <b>{gname}</b> ({len(recs)} сд): " + " | ".join(line))
        if best:
            L.append(f"     лучший стоп: {best[0]} → {best[1]:+.3f}R")
    L.append("")

    # ── 3. по парам, с проверкой на второй половине ──
    L.append("<b>3. ПО ПАРАМ: отбор на первой половине, ПРОВЕРКА на второй</b>")
    L.append("  <i>пара «подтверждена», только если в плюсе в ОБЕИХ половинах периода</i>")
    rows, confirmed, cand_a = [], [], 0
    for s, v in per.items():
        a = [r for r in v if r[0] <= mid_ts]
        b = [r for r in v if r[0] > mid_ts]
        ra = [x for x in (run_trade(r) for r in a) if x is not None]
        rb = [x for x in (run_trade(r) for r in b) if x is not None]
        rall = [x for x in (run_trade(r) for r in v) if x is not None]
        if len(rall) < MIN_TRADES:
            continue
        sa, sb, sall = _stat(ra), _stat(rb), _stat(rall)
        rows.append((s, sall, sa, sb, vol_of.get(s, 0)))
        if sa and sa[1] > 0 and len(ra) >= 4:
            cand_a += 1
            if sb and sb[1] > 0:
                confirmed.append(s)
    rows.sort(key=lambda x: -x[1][1])
    L.append(f"  <i>пар с ≥{MIN_TRADES} сделками: {len(rows)}</i>")
    L.append("  <b>Лучшие 12:</b>")
    for s, sall, sa, sb, vol in rows[:12]:
        ta = f"{sa[1]:+.2f}" if sa else "  —  "
        tb = f"{sb[1]:+.2f}" if sb else "  —  "
        L.append(f"   {s:9} ATR {vol:.2f}% | всего {sall[0]:3} сд {sall[1]:+.3f}R "
                 f"(ВР {sall[3]:.0f}%) | 1-я пол {ta} | 2-я пол {tb}")
    L.append("  <b>Худшие 8:</b>")
    for s, sall, sa, sb, vol in rows[-8:]:
        ta = f"{sa[1]:+.2f}" if sa else "  —  "
        tb = f"{sb[1]:+.2f}" if sb else "  —  "
        L.append(f"   {s:9} ATR {vol:.2f}% | всего {sall[0]:3} сд {sall[1]:+.3f}R "
                 f"(ВР {sall[3]:.0f}%) | 1-я пол {ta} | 2-я пол {tb}")
    L.append("")

    # ── 4. проверка отбора: держится ли он на второй половине ──
    L.append("<b>4. ВЫДЕРЖИВАЕТ ЛИ ОТБОР ПРОВЕРКУ</b>")
    sel = [r for s in confirmed for r in per[s] if r[0] > mid_ts]
    rest = [r for s, v in per.items() if s not in confirmed for r in v if r[0] > mid_ts]
    rs_sel = [x for x in (run_trade(r) for r in sel) if x is not None]
    rs_rest = [x for x in (run_trade(r) for r in rest) if x is not None]
    ss, sr = _stat(rs_sel), _stat(rs_rest)
    L.append(f"  в плюсе на 1-й половине: {cand_a} пар | из них и на 2-й: "
             f"<b>{len(confirmed)}</b>")
    if ss:
        L.append(f"  2-я половина, отобранные пары: {ss[0]} сд, <b>{ss[1]:+.3f}R</b> (±{ss[2]:.3f})")
    if sr:
        L.append(f"  2-я половина, остальные пары:  {sr[0]} сд, {sr[1]:+.3f}R (±{sr[2]:.3f})")
    # сколько пар прошло бы такой отбор СЛУЧАЙНО
    rnd_conf = 0
    for s, v in per.items():
        a = [r for r in v if r[0] <= mid_ts]
        b = [r for r in v if r[0] > mid_ts]
        ra = [x for x in (run_trade(r, coin=True) for r in a) if x is not None]
        rb = [x for x in (run_trade(r, coin=True) for r in b) if x is not None]
        if len(ra) >= 4 and len(rb) >= 1 and sum(ra) > 0 and sum(rb) > 0:
            rnd_conf += 1
    L.append(f"  <i>тот же отбор на СЛУЧАЙНОМ направлении подтвердил бы {rnd_conf} пар — "
             f"если это число близко к {len(confirmed)}, отбор ничего не значит</i>")
    if confirmed:
        L.append("  Подтверждённые: " + ", ".join(sorted(confirmed)))

    msg = "\n".join(L)
    print(msg.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    try:
        B.send_blocks(msg.split("\n"))
    except Exception as e:
        print(f"[PAIRS] отправка не удалась: {e}")


def main():
    try:
        run()
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            B.send_telegram(f"⚠️ Разбор по парам упал: {e}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
