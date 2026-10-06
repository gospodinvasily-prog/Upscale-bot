"""
bt_pairs.py — НАША СТРАТЕГИЯ НА ПОЛНЫХ ДАННЫХ.

Почему это не повтор пройденного. Все прежние прогоны считали заряд БЕЗ открытого
интереса, тейкеров и фандинга — я ошибочно считал, что истории contract_stats нет.
В шапке каждого отчёта так и стояло: «OI за историю не восстановить, сила ниже живой
на 2-3». То есть мы проверяли УРЕЗАННЫЙ детектор, а не тот, которым торгует бот.

Разведка показала: contract_stats отдаёт историю минимум на 60 дней, и в ней есть
open_interest, lsr_taker, last_funding_rate, ликвидации и позиционирование счетов.
Значит сигнал можно посчитать точно так же, как вживую.

Что считается:
  1. Заряд с ПОЛНОЙ силой (OI + тейкеры + фандинг) против урезанной — видно, что
     давали недостающие очки.
  2. Фильтры как в боте v10.4: уклон BTC по его VWAP, сторона VWAP пары, ход до границы.
  3. Разбивка по силе заряда и по вкладу OI.
  4. Монетка рядом с каждой строкой — без неё цифры ничего не значат.

Тайминг честный: заряд по ЗАКРЫТЫМ часовым свечам, вход по открытию 15м свечи
после закрытия часа (:00) или через полчаса (:30) — как сканирует бот.

Настройки:
  PB_DAYS (45)     — дней истории
  PB_OFFSET (0)    — сдвиг периода назад, для проверки на чужих данных
  PB_PAIRS (0)     — сколько пар (0 = все)
  PB_DIST (1.5)    — ход до границы, %
  PB_BTC (0.2)     — запас BTC от его VWAP, %
  PB_NO_BTC (0)    — 1 = выключить фильтр BTC
  PB_NO_PVWAP (0)  — 1 = выключить фильтр стороны VWAP пары
"""
import os
import math
import time
import statistics

import bot as B

DAYS      = int(os.environ.get("PB_DAYS", "45"))
OFFSET    = int(os.environ.get("PB_OFFSET", "0"))
PAIRS_N   = int(os.environ.get("PB_PAIRS", "0"))
DIST      = float(os.environ.get("PB_DIST", "1.5"))
BTC_MARG  = float(os.environ.get("PB_BTC", "0.2"))
NO_BTC    = os.environ.get("PB_NO_BTC", "0") == "1"
NO_PVWAP  = os.environ.get("PB_NO_PVWAP", "0") == "1"
SLIP      = float(os.environ.get("PB_SLIP", "0.10"))
COST      = float(os.environ.get("PB_COST", "0.065"))
STOP_SLIP = float(os.environ.get("PB_STOP_SLIP", "0.05"))
HOLD_H    = int(os.environ.get("PB_HOLD_H", "12"))

FINE_SEC = 900
STOP, TP1, TP3 = 1.0, 1.0, 2.0
PARTS = (1 / 3, 1 / 3, 1 / 3)
VW_BARS = 97
ENTRY_MINS = [0, 30]


def _fetch_candles(sym, tf, days):
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
            if probes > 25:
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


def _fetch_stats(sym, days):
    """contract_stats часовыми строками: OI, тейкеры, фандинг.
    Час вместо 5 минут — иначе на 45 дней ушло бы 130 запросов на пару."""
    now = int(time.time()) - OFFSET * 86400
    out, cur, probes = [], now - days * 86400, 0
    while cur < now:
        to = min(now, cur + 100 * 3600)
        raw = B.api_get("contract_stats", {"contract": f"{sym}_USDT", "interval": "1h",
                                           "from": cur, "to": to, "limit": 100})
        rows = raw if isinstance(raw, list) else []
        if not rows:
            probes += 1
            if probes > 20:
                break
            cur += 5 * 86400
            continue
        out.extend(rows)
        nxt = max(int(B.fnum(r.get("time", 0))) for r in rows) + 3600
        if nxt <= cur:
            break
        cur = nxt
    res = {}
    for r in out:
        t = int(B.fnum(r.get("time", 0)))
        if t <= 0:
            continue
        res[t - t % 3600] = {
            "oi": B.fnum(r.get("open_interest", 0)),
            "oiu": B.fnum(r.get("open_interest_usd", 0)),
            "taker": B.fnum(r.get("lsr_taker", 0)),
            "fund": B.fnum(r.get("last_funding_rate", 0)) * 100,
        }
    return res


def _stats_at(st, t_end, win_h=12):
    """Срез статистики на момент t_end по тем же правилам, что в боте:
    изменение OI за окно и геометрическое среднее lsr_taker."""
    if not st:
        return None
    key_rows = [(t, v) for t, v in st.items() if t_end - win_h * 3600 < t <= t_end]
    if len(key_rows) < 4:
        return None
    key_rows.sort()
    use_oi = all(v["oi"] > 0 for _, v in key_rows)
    first = key_rows[0][1]
    last = key_rows[-1][1]
    base = first["oi"] if use_oi else first["oiu"]
    cur = last["oi"] if use_oi else last["oiu"]
    oi_win = round((cur / base - 1) * 100, 2) if base > 0 else None
    tk = [v["taker"] for _, v in key_rows if 0.05 < v["taker"] < 20]
    taker = round(math.exp(sum(math.log(x) for x in tk) / len(tk)), 2) if tk else None
    return {"oi_win": oi_win, "oi_15m": None, "taker_win": taker,
            "taker_15m": None, "taker_1h": taker, "oi_1h": oi_win,
            "liq_long_win": 0, "liq_short_win": 0,
            "liq_long_1h": 0, "liq_short_1h": 0,
            "funding": last["fund"]}


def _vwap(win):
    vol = sum(x["v"] for x in win)
    if not win or vol <= 0:
        return None
    return sum((x["h"] + x["l"] + x["c"]) / 3 * x["v"] for x in win) / vol


def _sim(bars, side, entry, stop, t1, t2, t3):
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    is_l = side == "long"
    tg = [t1, t2, t3]
    for i in range(1, 3):
        if is_l and tg[i] <= tg[i - 1]:
            tg[i] = tg[i - 1] * 1.001
        if not is_l and tg[i] >= tg[i - 1]:
            tg[i] = tg[i - 1] * 0.999
    done, cur_stop, acc = 0, stop, 0.0
    cost = COST / 100 * entry / risk
    for c in bars:
        if (c["l"] <= cur_stop) if is_l else (c["h"] >= cur_stop):
            fill = cur_stop * (1 - STOP_SLIP / 100) if is_l else cur_stop * (1 + STOP_SLIP / 100)
            r = (fill - entry) / risk if is_l else (entry - fill) / risk
            return acc + r * sum(PARTS[done:]) - cost
        while done < 3:
            t = tg[done]
            if (c["h"] >= t) if is_l else (c["l"] <= t):
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
    r = (last - entry) / risk if is_l else (entry - last) / risk
    return acc + r * sum(PARTS[done:]) - cost


def _stat(rs):
    if not rs:
        return None
    n = len(rs)
    m = sum(rs) / n
    se = (statistics.pstdev(rs) / (n ** 0.5)) if n > 1 else 0.0
    return n, m, 1.96 * se, sum(1 for r in rs if r > 0) / n * 100


def _line(rs, label, ctl=None):
    st = _stat(rs)
    if not st:
        return f"  {label}: сделок нет"
    n, m, ci, wr = st
    mark = "✅" if m - ci > 0 else "❌" if m + ci < 0 else "  "
    s = f"  {mark} {label}: {n:4} сд, ВР {wr:3.0f}%, <b>{m:+.3f}R</b> (±{ci:.3f})"
    sc = _stat(ctl) if ctl else None
    if sc:
        s += f" | монетка {sc[1]:+.3f}R, эдж {m - sc[1]:+.3f}R"
    return s


def run():
    import random as _rnd
    rng = _rnd.Random(31337)
    pairs = B.UPSCALE_PAIRS[:PAIRS_N] if PAIRS_N else B.UPSCALE_PAIRS
    hold = max(6, int(HOLD_H * 3600 / FINE_SEC))
    t0 = time.time()

    # ── уклон BTC по его VWAP ──
    btc15 = _fetch_candles("BTC", "15m", DAYS + 2)
    vw_dev = {}
    for k in range(VW_BARS, len(btc15)):
        vw = _vwap(btc15[max(0, k - VW_BARS + 1):k + 1])
        if vw:
            vw_dev[btc15[k].get("t", 0) + FINE_SEC] = (btc15[k]["c"] - vw) / vw * 100

    def btc_side(ts):
        t = ts - ts % FINE_SEC
        for back in range(8):
            v = vw_dev.get(t - back * FINE_SEC)
            if v is not None:
                return "long" if v > BTC_MARG else "short" if v < -BTC_MARG else "neutral"
        return None

    trades = []     # (R_полный, score, oi_win, has_oi, side, px, bnd, seg)
    n_ch_full = n_ch_slim = 0
    n_pairs = 0
    cov = 0.0

    for i, sym in enumerate(pairs, 1):
        if sym == "BTC":
            continue
        try:
            fine = _fetch_candles(sym, "15m", DAYS)
            base = _fetch_candles(sym, "1h", DAYS + 5)
            st = _fetch_stats(sym, DAYS + 1)
        except Exception:
            continue
        if len(fine) < 400 or len(base) < 120:
            continue
        fine, base = fine[:-1], base[:-1]
        if not cov:
            cov = (fine[-1].get("t", 0) - fine[0].get("t", 0)) / 86400
        n_pairs += 1
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

            snap = _stats_at(st, cts, 12)
            tick = {"funding": (snap or {}).get("funding", 0.0), "change_24h": 0.0}
            # ПОЛНАЯ сила: детектор получает OI, тейкеров и фандинг — как вживую
            c_full = B.detect_charge(sym, upto, upto[-1]["c"], vb, ab,
                                     {"btc_chg_win": 0.0, "do_charge": True}, tick,
                                     lambda s, _sn=snap: _sn, P=B.ALT_P)
            # УРЕЗАННАЯ: как во всех прежних прогонах — ничего этого нет
            c_slim = B.detect_charge(sym, upto, upto[-1]["c"], vb, ab,
                                     {"btc_chg_win": 0.0, "do_charge": True},
                                     {"funding": 0.0, "change_24h": 0.0},
                                     lambda s: None, P=B.ALT_P)
            if c_full:
                n_ch_full += 1
            if c_slim:
                n_ch_slim += 1
            if not c_full:
                continue

            for em in ENTRY_MINS:
                ts = sig_ts + em * 60
                k = idx.get(ts)
                if k is None or k + hold + 2 >= len(fine) or k < VW_BARS + 2:
                    continue
                px = fine[k]["o"]
                bs = btc_side(ts)
                side = c_full["side"]
                if not NO_BTC:
                    if bs is None or bs == "neutral":
                        continue
                    if side in ("long", "short"):
                        if side != bs:
                            continue
                    else:
                        side = bs
                elif side not in ("long", "short"):
                    continue
                if not NO_PVWAP:
                    vw = _vwap(fine[k - VW_BARS:k])
                    if not vw:
                        continue
                    if ("long" if fine[k - 1]["c"] > vw else "short") != side:
                        continue
                is_l = side == "long"
                bnd = c_full["hi"] if is_l else c_full["lo"]
                room = (bnd - px) / px * 100 if is_l else (px - bnd) / px * 100
                if room < DIST:
                    continue
                seg = fine[k:k + hold]
                ent = px * (1 + SLIP / 100) if is_l else px * (1 - SLIP / 100)
                stp = px * (1 - STOP / 100) if is_l else px * (1 + STOP / 100)
                y1 = px * (1 + TP1 / 100) if is_l else px * (1 - TP1 / 100)
                y3 = px * (1 + TP3 / 100) if is_l else px * (1 - TP3 / 100)
                r = _sim(seg, side, ent, stp, y1, bnd, y3)
                if r is None:
                    continue
                oi_w = (snap or {}).get("oi_win")
                slim_sc = c_slim["score"] if c_slim else None
                trades.append({"R": r, "score": c_full["score"], "oi": oi_w,
                               "slim": slim_sc, "side": side, "px": px, "bnd": bnd,
                               "seg": seg, "taker": (snap or {}).get("taker_win"),
                               "fund": tick["funding"]})
                last_end = fine[min(k + hold, len(fine) - 1)].get("t", 0)
                break
        if i % 10 == 0:
            print(f"[FULL] {i}/{len(pairs)} | пар {n_pairs} | сделок {len(trades)} | "
                  f"{time.time() - t0:.0f}с")

    L = [f"🔋 <b>СТРАТЕГИЯ НА ПОЛНЫХ ДАННЫХ</b> (~{cov:.0f} дн, {n_pairs} пар)"
         + (f"\n⏪ <b>ПЕРИОД СДВИНУТ НАЗАД НА {OFFSET} ДНЕЙ</b>" if OFFSET else ""),
         "<i>впервые заряд считается с настоящими OI, тейкерами и фандингом — "
         "раньше в бэктестах их не было, и проверялся урезанный детектор</i>",
         f"Фильтры: ход ≥{DIST}%" + ("" if NO_BTC else f", BTC ≥{BTC_MARG}% от VWAP")
         + ("" if NO_PVWAP else ", сторона VWAP пары"),
         f"Зарядов с полными данными: {n_ch_full} | тем же детектором без них: {n_ch_slim}",
         f"Сделок: {len(trades)} | издержки {COST}%, проскальзывание {SLIP}%", ""]

    if len(trades) < 30:
        L.append("⚠️ Сделок мало для выводов. Попробуй PB_DAYS=60 или PB_NO_BTC=1")
        B.send_blocks(L)
        print("\n".join(L))
        return

    def coin(sel):
        out = []
        for t in sel:
            sd = rng.choice(("long", "short"))
            is_l = sd == "long"
            px = t["px"]
            b = t["bnd"] if sd == t["side"] else (px * (1 + DIST / 100) if is_l
                                                 else px * (1 - DIST / 100))
            ent = px * (1 + SLIP / 100) if is_l else px * (1 - SLIP / 100)
            stp = px * (1 - STOP / 100) if is_l else px * (1 + STOP / 100)
            y1 = px * (1 + TP1 / 100) if is_l else px * (1 - TP1 / 100)
            y3 = px * (1 + TP3 / 100) if is_l else px * (1 - TP3 / 100)
            r = _sim(t["seg"], sd, ent, stp, y1, b, y3)
            if r is not None:
                out.append(r)
        return out

    allR = [t["R"] for t in trades]
    L.append("<b>1. ВСЕ СДЕЛКИ</b>")
    L.append(_line(allR, "полный детектор", coin(trades)))
    L.append("")

    L.append("<b>2. ПО СИЛЕ ЗАРЯДА</b> (теперь сила настоящая, с очками за OI)")
    for lo, hi in ((4, 5), (6, 7), (8, 9), (10, 99)):
        sel = [t for t in trades if lo <= t["score"] <= hi]
        if len(sel) >= 25:
            lbl = f"сила {lo}-{hi}" if hi < 99 else f"сила ≥{lo}"
            L.append(_line([t["R"] for t in sel], lbl, coin(sel)))
    L.append("")

    L.append("<b>3. ЧТО ДАЁТ ОТКРЫТЫЙ ИНТЕРЕС</b>")
    L.append("  <i>главный вопрос прогона: очки за OI были слепой зоной прежних тестов</i>")
    for lbl, cond in (("OI падает (≤-2%)", lambda o: o is not None and o <= -2),
                      ("OI стоит (-2…+1%)", lambda o: o is not None and -2 < o < 1),
                      ("OI растёт (+1…+4%)", lambda o: o is not None and 1 <= o < 4),
                      ("OI сильно растёт (≥+4%)", lambda o: o is not None and o >= 4)):
        sel = [t for t in trades if cond(t["oi"])]
        if len(sel) >= 20:
            L.append(_line([t["R"] for t in sel], lbl, coin(sel)))
    L.append("")

    L.append("<b>4. ТЕЙКЕРЫ И ФАНДИНГ</b>")
    for lbl, cond in (("тейкеры продают (L/S ≤0.9)", lambda t: t["taker"] is not None and t["taker"] <= 0.9),
                      ("тейкеры нейтральны", lambda t: t["taker"] is not None and 0.9 < t["taker"] < 1.1),
                      ("тейкеры покупают (L/S ≥1.1)", lambda t: t["taker"] is not None and t["taker"] >= 1.1)):
        sel = [t for t in trades if cond(t)]
        if len(sel) >= 20:
            L.append(_line([t["R"] for t in sel], lbl, coin(sel)))
    for lbl, cond in (("фандинг отрицательный", lambda t: t["fund"] < -0.005),
                      ("фандинг положительный", lambda t: t["fund"] > 0.01)):
        sel = [t for t in trades if cond(t)]
        if len(sel) >= 20:
            L.append(_line([t["R"] for t in sel], lbl, coin(sel)))
    L.append("")

    L.append("<b>5. ЧИСТОЕ ДВИЖЕНИЕ</b> (без стопов и целей)")
    for hh, nb in ((1, 4), (2, 8), (4, 16), (12, 48)):
        mv = []
        for t in trades:
            seg = t["seg"]
            if len(seg) <= nb or t["px"] <= 0:
                continue
            m = (seg[nb]["c"] - t["px"]) / t["px"] * 100
            mv.append(m if t["side"] == "long" else -m)
        if len(mv) < 30:
            continue
        n = len(mv)
        avg = sum(mv) / n
        se = statistics.pstdev(mv) / (n ** 0.5) if n > 1 else 0
        mark = "✅" if avg - 1.96 * se > 0 else "❌" if avg + 1.96 * se < 0 else "  "
        L.append(f"  {mark} через {hh:2}ч: {avg:+.3f}% (±{1.96 * se:.3f}), "
                 f"в плюс {sum(1 for x in mv if x > 0) / n * 100:.0f}%")

    msg = "\n".join(L)
    print(msg.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    try:
        B.send_blocks(msg.split("\n"))
    except Exception as e:
        print(f"[FULL] отправка не удалась: {e}")


def main():
    try:
        run()
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            B.send_telegram(f"⚠️ Прогон на полных данных упал: {e}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
