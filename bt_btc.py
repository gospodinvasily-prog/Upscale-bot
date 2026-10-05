"""
bt_btc.py — фильтр по уклону BITCOIN.

Идея: альты ходят за биткоином. Значит входить в лонг имеет смысл только когда
у BTC уклон вверх, в шорт — когда вниз, а в нейтрали не входить вовсе.

Стратегия берётся НАША РАБОЧАЯ (заряд 1ч → уклон → ход до границы), не последняя.
Меняется только одно: добавляется требование совпадения с уклоном BTC.

Уклон BTC определяется тремя способами — чтобы вывод не зависел от того, как
именно мы его посчитали:
  A. изменение цены BTC за 12ч с нейтральной зоной (порог перебирается);
  B. положение BTC относительно его дневного VWAP;
  C. положение BTC относительно EMA20 на 4ч (так бот уже меряет силу BTC).

Рядом с каждым вариантом — монетка и доля отсеянных сделок.

Запуск: RUN_BACKTEST=btc
Настройки: BC_DAYS (60), BC_PAIRS (0=все), BC_DIST (1.5)
"""
import os
import time
import statistics

import bot as B

DAYS    = int(os.environ.get("BC_DAYS", "60"))
OFFSET  = int(os.environ.get("BC_OFFSET", "0"))     # на сколько дней сдвинуть период назад
# Заряды, у которых уклон ПАРЫ не определён («both»): направление берём от BTC.
# 1 = включить, 0 = только заряды с чётким уклоном (как было раньше)
INCLUDE_BOTH = os.environ.get("BC_BOTH", "1") == "1"
# Минута входа после закрытия часа. Живой бот сканирует на :30 (v10.0), поэтому и тест
# входит на :30, а уклон BTC берёт по 15м свече, закрытой к этому моменту.
# Раньше тест входил на :00 — результаты прошлых прогонов получены при :00.
ENTRY_MIN = int(os.environ.get("BC_ENTRY_MIN", "30"))
PAIRS_N = int(os.environ.get("BC_PAIRS", "0"))
DIST    = float(os.environ.get("BC_DIST", "1.5"))
SLIP    = float(os.environ.get("BC_SLIP", "0.15"))
COST    = float(os.environ.get("BC_COST", "0.065"))
STOP_SLIP = float(os.environ.get("BC_STOP_SLIP", "0.10"))
HOLD_H  = int(os.environ.get("BC_HOLD_H", "12"))

FINE_SEC = 900
STOP, TP1, TP3 = 1.0, 1.0, 2.0
PARTS = (1 / 3, 1 / 3, 1 / 3)

CHG_GRID = [0.3, 0.5]                # нейтральная зона по изменению BTC за 12ч, %
BTC_WIN_H = 12                        # окно изменения BTC
DIST_GRID = [1.0, 1.5]               # ход до границы коридора пары, %
VWAP_GRID = [2.0, 2.5]               # не дальше N ATR от дневного VWAP пары
BTC_TFS = ["1h", "4h"]               # на каком ТФ считать КОРИДОР биткоина
# Насколько далеко BTC должен отойти от своего VWAP, чтобы считать уклон уверенным.
# 0 = любое положение (как сейчас). Чем больше — тем строже и тем меньше сделок.
VW_MARGIN = [0.0, 0.1, 0.2, 0.3, 0.5]


def _fetch(sym, tf, days):
    sec = {"15m": 900, "1h": 3600, "4h": 14400}[tf]
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


def _line(rs, label, ctl=None, base_n=None):
    st = _stat(rs)
    if not st:
        return f"  {label}: сделок нет"
    n, exp, ci, wr = st
    mark = "✅" if exp - ci > 0 else "❌" if exp + ci < 0 else "  "
    s = f"  {mark} {label}: {n:4} сд, ВР {wr:3.0f}%, <b>{exp:+.3f}R</b> (±{ci:.3f})"
    sc = _stat(ctl) if ctl else None
    if sc:
        s += f"  <i>монетка {sc[1]:+.3f}R, эдж {exp - sc[1]:+.3f}R</i>"
    if base_n:
        s += f" <i>[осталось {n/base_n*100:.0f}%]</i>"
    return s


def run():
    import random as _rnd
    rng = _rnd.Random(1234)

    # ── уклон BTC по трём определениям, на каждый час ──
    btc1h = _fetch("BTC", "1h", DAYS + 5)
    btc15 = _fetch("BTC", "15m", DAYS + 2)
    if len(btc1h) < 100:
        B.send_telegram("⚠️ Не получил свечи BTC")
        return
    btc1h = btc1h[:-1]
    chg = {}          # ts закрытия часа -> изменение за 12ч, %
    for k in range(BTC_WIN_H, len(btc1h)):
        a, b = btc1h[k - BTC_WIN_H]["o"], btc1h[k]["c"]
        if a > 0:
            chg[btc1h[k].get("t", 0) + 3600] = (b - a) / a * 100
    # BTC относительно своего дневного VWAP (по 15м)
    vw_side = {}
    bars_day = int(86400 / FINE_SEC)
    for k in range(bars_day // 2, len(btc15)):
        seg = btc15[max(0, k - bars_day):k + 1]
        vv = sum(x["v"] for x in seg)
        if vv <= 0:
            continue
        vw = sum((x["h"] + x["l"] + x["c"]) / 3 * x["v"] for x in seg) / vv
        vw_side[btc15[k].get("t", 0) + FINE_SEC] = (btc15[k]["c"] - vw) / vw * 100 if vw else 0.0
    # уклон BTC по ЕГО СОБСТВЕННОМУ коридору — на 1ч и на 4ч
    btc_corr = {tf: {} for tf in BTC_TFS}
    for tf in BTC_TFS:
        sec_tf = {"1h": 3600, "4h": 14400}[tf]
        bb = btc1h if tf == "1h" else _fetch("BTC", "4h", DAYS + 10)
        if tf == "4h":
            bb = bb[:-1] if bb else []
        win = max(4, round(12 * 3600 / sec_tf)) if tf == "1h" else 6
        if len(bb) < B.BASE_FROM + win + 10:
            continue
        saved = B.ACC_WINDOW
        B.ACC_WINDOW = win
        P = dict(B.ALT_P)
        P["tf"] = tf
        try:
            for e in range(B.BASE_FROM + win, len(bb)):
                upto = bb[:e]
                sl = upto[-B.BASE_FROM:-B.BASE_TO]
                if len(sl) < 10:
                    continue
                vb = B.trimmed_mean([x["v"] for x in sl])
                ab = B.trimmed_mean(B.true_ranges(upto)[-B.BASE_FROM:-B.BASE_TO])
                if not vb or not ab or vb <= 0 or ab <= 0:
                    continue
                cc = B.detect_charge("BTC", upto, upto[-1]["c"], vb, ab,
                                     {"btc_chg_win": 0.0, "do_charge": True},
                                     {"funding": 0.0, "change_24h": 0.0},
                                     lambda s: None, P=P)
                ts_k = upto[-1].get("t", 0) + sec_tf
                if cc:
                    btc_corr[tf][ts_k] = cc["side"] if cc["side"] in ("long", "short") else "neutral"
                else:
                    # заряда нет — берём положение цены в окне как грубый уклон
                    w = upto[-win:]
                    hi = max(x["h"] for x in w)
                    lo = min(x["l"] for x in w)
                    px_ = upto[-1]["c"]
                    pos = (px_ - lo) / (hi - lo) if hi > lo else 0.5
                    btc_corr[tf][ts_k] = ("long" if pos > 0.6 else
                                          "short" if pos < 0.4 else "neutral")
        finally:
            B.ACC_WINDOW = saved

    # BTC относительно EMA20 на 4ч
    ema_side = {}
    btc4h = _fetch("BTC", "4h", DAYS + 10)
    if len(btc4h) > 25:
        btc4h = btc4h[:-1]
        ema, kf = btc4h[0]["c"], 2 / 21
        for c in btc4h:
            ema = c["c"] * kf + ema * (1 - kf)
            ema_side[c.get("t", 0) + 14400] = "long" if c["c"] > ema else "short"

    def btc_bias_chg(ts, thr):
        """Ближайшее известное изменение BTC не позже ts."""
        t = ts - ts % 3600
        for back in range(0, 6):
            v = chg.get(t - back * 3600)
            if v is not None:
                return "long" if v > thr else "short" if v < -thr else "neutral"
        return None

    def btc_bias_map(ts, m, step):
        t = ts - ts % step
        for back in range(0, 8):
            v = m.get(t - back * step)
            if v:
                return v
        return None

    pairs = B.UPSCALE_PAIRS[:PAIRS_N] if PAIRS_N else B.UPSCALE_PAIRS
    hold = max(6, int(HOLD_H * 3600 / FINE_SEC))
    setups = []
    n_ch = 0
    n_both = 0
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
        last_end_b = 0
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
            sig_ts = cts + 3600 + ENTRY_MIN * 60     # момент входа: :30 после закрытия часа
            if sig_ts <= last_end:
                continue
            c = B.detect_charge(sym, upto, upto[-1]["c"], vb, ab,
                                {"btc_chg_win": 0.0, "do_charge": True},
                                {"funding": 0.0, "change_24h": 0.0},
                                lambda s: None, P=B.ALT_P)
            if not c:
                continue
            is_both = c["side"] not in ("long", "short")
            if is_both and not INCLUDE_BOTH:
                continue
            n_ch += 1
            if is_both:
                n_both += 1
            k = idx.get(sig_ts)
            if k is None or k + hold + 2 >= len(fine):
                continue
            px = fine[k]["o"]
            if not is_both:
                is_l = c["side"] == "long"
                bnd = c["hi"] if is_l else c["lo"]
                room = (bnd - px) / px * 100 if is_l else (px - bnd) / px * 100
                if room < min(DIST_GRID):
                    continue
            elif sig_ts <= last_end_b:
                continue
            lo_i = max(0, k - int(86400 / FINE_SEC))
            sg = fine[lo_i:k]
            vv = sum(x["v"] for x in sg)
            vw_pair = (sum((x["h"] + x["l"] + x["c"]) / 3 * x["v"] for x in sg) / vv) if vv > 0 else None
            atr_pct = c["atr"] / px * 100 if px > 0 else 0
            vw_atr = (abs(px - vw_pair) / px * 100 / atr_pct) if (vw_pair and atr_pct > 0) else 99
            side_s = "both" if is_both else c["side"]
            setups.append((fine[k:k + hold], side_s, px, c["hi"], c["lo"], sig_ts, vw_atr))
            end_ts = fine[min(k + hold, len(fine) - 1)].get("t", 0)
            if is_both:
                last_end_b = end_ts       # отдельно, чтобы не менять базу «только с уклоном»
            else:
                last_end = end_ts
        if i % 20 == 0:
            print(f"[BTC] {i}/{len(pairs)} | сделок {len(setups)} | {time.time()-t0:.0f}с")

    def collect(bias_fn=None, coin=False, dist=DIST, vwmax=99, both="include"):
        """both: include — и заряды с уклоном пары, и без него (направление от BTC);
        exclude — только с уклоном пары; only — только заряды БЕЗ уклона пары."""
        out = []
        for seg, side, px, hi, lo, ts, vw_atr in setups:
            if vw_atr > vwmax:
                continue
            is_both = side == "both"
            if (both == "exclude" and is_both) or (both == "only" and not is_both):
                continue
            if is_both and bias_fn is None:
                continue                  # без фильтра BTC направления у такого заряда нет
            if bias_fn is not None:
                bb = bias_fn(ts)
                if bb is None or bb == "neutral":
                    continue
                if not is_both and bb != side:
                    continue
                eff = bb if is_both else side
            else:
                eff = side
            is_l0 = eff == "long"
            bnd = hi if is_l0 else lo
            room = (bnd - px) / px * 100 if is_l0 else (px - bnd) / px * 100
            if room < dist:
                continue
            sd = rng.choice(("long", "short")) if coin else eff
            is_l = sd == "long"
            b = bnd if sd == eff else (px * (1 + dist / 100) if is_l else px * (1 - dist / 100))
            ent = px * (1 + SLIP / 100) if is_l else px * (1 - SLIP / 100)
            stp = px * (1 - STOP / 100) if is_l else px * (1 + STOP / 100)
            y1 = px * (1 + TP1 / 100) if is_l else px * (1 - TP1 / 100)
            y3 = px * (1 + TP3 / 100) if is_l else px * (1 - TP3 / 100)
            r = _sim(seg, sd, ent, stp, y1, b, y3)
            if r is not None:
                out.append(r)
        return out

    L = [f"₿ <b>ФИЛЬТР ПО УКЛОНУ BITCOIN</b> (~{cov:.0f} дн, {len(pairs)} пар)",
         "<i>входим только когда уклон монеты совпадает с уклоном BTC. "
         "Нейтраль пропускаем</i>"
         + (f"\n⏪ <b>ПЕРИОД СДВИНУТ НАЗАД НА {OFFSET} ДНЕЙ</b> — проверка на чужих данных"
            if OFFSET else ""),
         (f"Заряды БЕЗ уклона пары включены: направление берём от BTC "
          f"({n_both} из {n_ch} зарядов)" if INCLUDE_BOTH else
          "Заряды без уклона пары не берём"),
         f"Зарядов: {n_ch} | сделок в выборке: {len(setups)}", ""]

    # ── Как часто BTC нейтрален и как долго ждать уклона ──
    # Считаем по тем же :30, на которых сканирует бот, только в окне работы (04:00-23:00 МСК).
    scan_ts = []
    if vw_side:
        t_lo, t_hi = min(vw_side), max(vw_side)
        t = (t_lo // 3600 + 1) * 3600 + ENTRY_MIN * 60
        while t <= t_hi:
            msk_h = ((t // 3600) + 3) % 24
            if 4 <= msk_h <= 22:
                scan_ts.append(t)
            t += 3600
    L.append("<b>КАК ЧАСТО BTC НЕЙТРАЛЕН</b> (по сканам :30 в окне 04:00–23:00 МСК)")
    L.append("  <i>нейтраль = BTC в пределах запаса от своего VWAP, в этот скан альты не торгуем</i>")
    if scan_ts:
        n_days = max(1.0, (scan_ts[-1] - scan_ts[0]) / 86400)
        for m in VW_MARGIN:
            cnt = {"long": 0, "short": 0, "neutral": 0}
            run_n = longest = 0
            prev_t = None
            last_dir, flips = None, 0
            for t in scan_ts:
                v = btc_bias_map(t, vw_side, FINE_SEC)
                if v is None:
                    continue
                bb = "long" if v > m else "short" if v < -m else "neutral"
                cnt[bb] += 1
                if prev_t is not None and t - prev_t > 3700:
                    run_n = 0                      # ночной разрыв — серия обрывается
                if bb == "neutral":
                    run_n += 1
                    longest = max(longest, run_n)
                else:
                    run_n = 0
                    if last_dir and bb != last_dir:
                        flips += 1
                    last_dir = bb
                prev_t = t
            tot = sum(cnt.values()) or 1
            lbl = "любое положение" if m == 0 else f"запас {m}%"
            L.append(f"  {lbl:16}: лонг {cnt['long']/tot*100:3.0f}%, шорт {cnt['short']/tot*100:3.0f}%, "
                     f"<b>нейтраль {cnt['neutral']/tot*100:3.0f}%</b> | "
                     f"самая долгая нейтраль подряд: <b>{longest} ч</b> | "
                     f"смен лонг↔шорт: {flips/n_days:.1f} в сутки")
    else:
        L.append("  данных BTC не хватило")
    L.append("")

    methods = []
    for tf in BTC_TFS:
        if btc_corr.get(tf):
            methods.append((f"коридор BTC {tf}",
                            lambda ts, m=btc_corr[tf], st={"1h": 3600, "4h": 14400}[tf]:
                            btc_bias_map(ts, m, st)))
    for thr in CHG_GRID:
        methods.append((f"изменение BTC 12ч, нейтраль ±{thr}%",
                        lambda ts, t=thr: btc_bias_chg(ts, t)))
    def vw_bias(ts, margin):
        """BTC выше своего VWAP на margin% → лонги, ниже на столько же → шорты."""
        v = btc_bias_map(ts, vw_side, FINE_SEC)
        if v is None:
            return None
        return "long" if v > margin else "short" if v < -margin else "neutral"

    for m in VW_MARGIN:
        lbl = "BTC vs VWAP (любое положение)" if m == 0 else f"BTC дальше {m}% от VWAP"
        methods.append((lbl, lambda ts, mm=m: vw_bias(ts, mm)))
    if ema_side:
        methods.append(("BTC vs EMA20 4ч", lambda ts: btc_bias_map(ts, ema_side, 14400)))

    # согласие двух признаков: VWAP и коридор BTC смотрят в одну сторону
    if btc_corr.get("1h"):
        def both_agree(ts):
            a = vw_bias(ts, 0.0)
            b = btc_bias_map(ts, btc_corr["1h"], 3600)
            if not a or not b or a == "neutral" or b == "neutral":
                return "neutral"
            return a if a == b else "neutral"
        methods.append(("VWAP + коридор BTC 1ч согласны", both_agree))

    best = None
    for dist in DIST_GRID:
        for vw in VWAP_GRID:
            L.append(f"<b>Пара: ход ≥{dist}% | VWAP ≤{vw} ATR</b>")
            base_rs = collect(dist=dist, vwmax=vw)
            b0 = _stat(base_rs)
            L.append(_line(base_rs, "без фильтра BTC",
                           collect(coin=True, dist=dist, vwmax=vw)))
            bn = len(base_rs) or 1
            for name, fn in methods:
                rs = collect(fn, dist=dist, vwmax=vw)
                st = _stat(rs)
                if not st or st[0] < 30:
                    continue
                cl = collect(fn, coin=True, dist=dist, vwmax=vw)
                L.append(_line(rs, name, cl, bn))
                sc = _stat(cl)
                edge = st[1] - (sc[1] if sc else 0)
                gain = st[1] - (b0[1] if b0 else 0)
                if st[0] >= 60 and (best is None or st[1] > best[2]):
                    best = (f"ход ≥{dist}%, VWAP ≤{vw}, {name}", st[0], st[1], edge, gain)
            L.append("")

    # эффект добавления зарядов без уклона пары — на основных настройках
    if INCLUDE_BOTH:
        L += ["<b>ЧТО ДАЮТ ЗАРЯДЫ БЕЗ УКЛОНА ПАРЫ</b> (ход ≥1.5%, VWAP пары ≤2.0)",
              "  <i>для каждого фильтра BTC: только с уклоном пары / вместе / "
              "только без уклона</i>"]
        cmp_methods = [m for m in methods if any(k in m[0] for k in
                       ("любое положение", "0.2%", "0.3%", "согласны", "коридор BTC 1h"))]
        for name, fn in cmp_methods:
            a = collect(fn, dist=1.5, vwmax=2.0, both="exclude")
            b_ = collect(fn, dist=1.5, vwmax=2.0, both="include")
            c_ = collect(fn, dist=1.5, vwmax=2.0, both="only")
            L.append(f"  <b>{name}</b>")
            L.append(_line(a, "   только с уклоном пары"))
            L.append(_line(b_, "   ВМЕСТЕ с зарядами без уклона"))
            L.append(_line(c_, "   только заряды без уклона (направление от BTC)"))
        L.append("")

    if best:
        L.append(f"→ <b>лучшее: {best[0]}</b>")
        L.append(f"   {best[1]} сд, {best[2]:+.3f}R, эдж над монеткой {best[3]:+.3f}R, "
                 f"прирост к «без фильтра» {best[4]:+.3f}R")
        L.append("<i>фильтр имеет смысл, только если прирост заметный И выборка осталась "
                 "достаточной</i>")
    else:
        L.append("→ ни один вариант не набрал 60 сделок")

    msg = "\n".join(L)
    print(msg.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    try:
        B.send_blocks(msg.split("\n"))
    except Exception as e:
        print(f"[BTC] отправка не удалась: {e}")


def main():
    try:
        run()
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            B.send_telegram(f"⚠️ Прогон фильтра BTC упал: {e}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
