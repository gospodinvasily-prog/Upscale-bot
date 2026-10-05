"""
bt_vbreak.py — сигнал по ПРОБОЮ VWAP.

Другой механизм, чем всё, что мы проверяли раньше. Раньше сигналом был коридор
с уклоном. Здесь сигнал — свеча ЗАКРЫЛАСЬ по другую сторону дневного VWAP,
то есть цена перешла через опорный уровень дня.

Проверяются развилки, которые ты назвал:
  1. На какой свече ловим пробой: часовой или 15-минутной.
  2. Нужны ли наши условия заряда (сжатие, объём, стоящая цена) — с ними и без.
  3. Нужен ли коридор: вторая цель на его границе и требование хода до неё.

Тайминг честный: свеча закрылась → вход по открытию следующей 15м свечи.
Рядом с каждым вариантом — монетка.

Запуск: RUN_BACKTEST=vbreak
Настройки: VB_DAYS (60), VB_PAIRS (0=все)
"""
import os
import time
import statistics

import bot as B

DAYS    = int(os.environ.get("VB_DAYS", "60"))
PAIRS_N = int(os.environ.get("VB_PAIRS", "0"))
SLIP    = float(os.environ.get("VB_SLIP", "0.15"))
COST    = float(os.environ.get("VB_COST", "0.065"))
STOP_SLIP = float(os.environ.get("VB_STOP_SLIP", "0.10"))
HOLD_H  = int(os.environ.get("VB_HOLD_H", "12"))

FINE_SEC = 900
STOP, TP1, TP3 = 1.0, 1.0, 2.0
PARTS = (1 / 3, 1 / 3, 1 / 3)
DIST = float(os.environ.get("VB_DIST", "1.5"))


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
    rng = _rnd.Random(2468)
    pairs = B.UPSCALE_PAIRS[:PAIRS_N] if PAIRS_N else B.UPSCALE_PAIRS
    hold = max(6, int(HOLD_H * 3600 / FINE_SEC))
    bars_day = int(86400 / FINE_SEC)

    # (тф, есть_заряд, side, px, bnd_или_None, room)
    setups = []
    n_cross = {"1h": 0, "15m": 0}
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

        # дневной VWAP на каждой 15м свече (нарастающим итогом, только прошлое)
        vwaps = []
        for k in range(len(fine)):
            lo = max(0, k - bars_day)
            seg = fine[lo:k + 1]
            vv = sum(x["v"] for x in seg)
            vwaps.append(sum((x["h"] + x["l"] + x["c"]) / 3 * x["v"] for x in seg) / vv
                         if vv > 0 else None)

        # заряды по часу — чтобы знать, был ли заряд в этот момент
        charge_at = {}
        for e in range(B.BASE_FROM + B.ACC_WINDOW, len(base)):
            upto = base[:e]
            sl = upto[-B.BASE_FROM:-B.BASE_TO]
            if len(sl) < 10:
                continue
            vb = B.trimmed_mean([c["v"] for c in sl])
            ab = B.trimmed_mean(B.true_ranges(upto)[-B.BASE_FROM:-B.BASE_TO])
            if not vb or not ab or vb <= 0 or ab <= 0:
                continue
            c = B.detect_charge(sym, upto, upto[-1]["c"], vb, ab,
                                {"btc_chg_win": 0.0, "do_charge": True},
                                {"funding": 0.0, "change_24h": 0.0},
                                lambda s: None, P=B.ALT_P)
            if c:
                charge_at[upto[-1].get("t", 0) + 3600] = (c["hi"], c["lo"])

        def add(tf, k_enter, side, cross_ts):
            """k_enter — индекс 15м свечи, по открытию которой входим."""
            if k_enter is None or k_enter + hold + 2 >= len(fine) or k_enter < bars_day // 2:
                return
            px = fine[k_enter]["o"]
            is_l = side == "long"
            # коридор берём от ближайшего заряда не старше 2 часов
            hl = None
            for back in (0, 3600, 7200):
                hl = charge_at.get(cross_ts - cross_ts % 3600 - back)
                if hl:
                    break
            bnd, room = None, None
            if hl:
                bnd = hl[0] if is_l else hl[1]
                room = (bnd - px) / px * 100 if is_l else (px - bnd) / px * 100
            setups.append((tf, hl is not None, side, px, bnd, room,
                           fine[k_enter:k_enter + hold]))

        # пробой VWAP часовой свечой
        last_side = {}
        for e in range(2, len(base)):
            cts = base[e - 1].get("t", 0)
            sig_ts = cts + 3600
            k = idx.get(sig_ts)
            if k is None or k < 2:
                continue
            vw = vwaps[k - 1]
            if vw is None:
                continue
            prev_c, cur_c = base[e - 2]["c"], base[e - 1]["c"]
            if prev_c <= vw < cur_c:
                n_cross["1h"] += 1
                add("1h", k, "long", sig_ts)
            elif prev_c >= vw > cur_c:
                n_cross["1h"] += 1
                add("1h", k, "short", sig_ts)

        # пробой VWAP 15-минутной свечой
        for k in range(2, len(fine) - 1):
            vw = vwaps[k - 1]
            if vw is None:
                continue
            if fine[k - 2]["c"] <= vw < fine[k - 1]["c"]:
                n_cross["15m"] += 1
                add("15m", k, "long", fine[k].get("t", 0))
            elif fine[k - 2]["c"] >= vw > fine[k - 1]["c"]:
                n_cross["15m"] += 1
                add("15m", k, "short", fine[k].get("t", 0))

        if i % 20 == 0:
            print(f"[VBREAK] {i}/{len(pairs)} | сделок {len(setups)} | {time.time()-t0:.0f}с")

    def collect(tf, need_charge, use_corridor, need_room, coin=False):
        out = []
        for s_tf, has_ch, side, px, bnd, room, seg in setups:
            if s_tf != tf:
                continue
            if need_charge and not has_ch:
                continue
            if use_corridor and bnd is None:
                continue
            if need_room and (room is None or room < DIST):
                continue
            sd = rng.choice(("long", "short")) if coin else side
            is_l = sd == "long"
            if use_corridor and sd == side and bnd is not None:
                b = bnd
            else:
                b = px * (1 + 1.5 / 100) if is_l else px * (1 - 1.5 / 100)
            ent = px * (1 + SLIP / 100) if is_l else px * (1 - SLIP / 100)
            stp = px * (1 - STOP / 100) if is_l else px * (1 + STOP / 100)
            y1 = px * (1 + TP1 / 100) if is_l else px * (1 - TP1 / 100)
            y3 = px * (1 + TP3 / 100) if is_l else px * (1 - TP3 / 100)
            r = _sim(seg, sd, ent, stp, y1, b, y3)
            if r is not None:
                out.append(r)
        return out

    L = [f"⚡ <b>ПРОБОЙ VWAP</b> (~{cov:.0f} дн, {len(pairs)} пар)",
         "<i>сигнал: свеча ЗАКРЫЛАСЬ по другую сторону дневного VWAP. "
         "Тайминг честный — вход по открытию следующей 15м свечи</i>",
         f"Пробоев найдено: час {n_cross['1h']}, 15м {n_cross['15m']}",
         f"Стоп {STOP}%, цели {TP1}% → граница/{1.5}% → {TP3}%, "
         f"проскальзывание {SLIP}%", ""]

    best = None
    for tf in ("1h", "15m"):
        L.append(f"<b>Пробой на {tf} свече:</b>")
        for nc in (False, True):
            for uc in (False, True):
                for nr in (False, True):
                    if nr and not uc:
                        continue          # ход до границы без коридора бессмыслен
                    rs = collect(tf, nc, uc, nr)
                    st = _stat(rs)
                    if not st or st[0] < 30:
                        continue
                    cl = collect(tf, nc, uc, nr, coin=True)
                    parts = []
                    parts.append("с зарядом" if nc else "без заряда")
                    parts.append("цель на границе" if uc else "цель 1.5%")
                    if nr:
                        parts.append(f"ход ≥{DIST}%")
                    L.append(_line(rs, " | ".join(parts), cl))
                    sc = _stat(cl)
                    edge = st[1] - (sc[1] if sc else 0)
                    if st[0] >= 80 and (best is None or st[1] > best[2]):
                        best = (tf, " | ".join(parts), st[1], st[0], edge)
        L.append("")

    if best:
        L.append(f"→ <b>лучшее: {best[0]}, {best[1]}</b> — {best[3]} сд, "
                 f"{best[2]:+.3f}R, эдж над монеткой {best[4]:+.3f}R")
    else:
        L.append("→ нигде не набралось 80 сделок для вывода")

    msg = "\n".join(L)
    print(msg.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    try:
        B.send_blocks(msg.split("\n"))
    except Exception as e:
        print(f"[VBREAK] отправка не удалась: {e}")


def main():
    try:
        run()
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            B.send_telegram(f"⚠️ Прогон пробоя VWAP упал: {e}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
