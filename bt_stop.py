"""
bt_stop.py — шаг 1: стоп и подтяжка.

Сигнал зафиксирован (заряд 1ч, уклон, ход до границы ≥1.5%), цели пока прежние.
Меняются только две вещи: ширина стопа и то, подтягиваем ли мы его.

Зачем именно это сначала. Монетка с нашей схемой выхода даёт −0.33R — то есть
схема сама по себе теряет треть риска на сделке. При стопе 1% и первой цели 1%
удачная сделка даёт около +0.25R (треть позиции на цели 0.74R с учётом входа),
а неудачная −1R. Для безубытка нужен винрейт около 80%, у нас 55%.
Значит дело не в сигнале, а в геометрии сделки — с неё и начинаем.

Рядом с каждым вариантом считается МОНЕТКА: если наш сигнал не лучше случайного
направления, вариант бессмыслен, каким бы ни было матожидание.

Запуск: RUN_BACKTEST=stop
Настройки: ST_DAYS (60), ST_PAIRS (0=все), ST_DIST (1.5)
"""
import os
import time
import statistics

import bot as B

DAYS    = int(os.environ.get("ST_DAYS", "60"))
PAIRS_N = int(os.environ.get("ST_PAIRS", "0"))
DIST    = float(os.environ.get("ST_DIST", "1.5"))
SLIP    = float(os.environ.get("ST_SLIP", "0.15"))
COST    = float(os.environ.get("ST_COST", "0.065"))
STOP_SLIP = float(os.environ.get("ST_STOP_SLIP", "0.10"))
HOLD_H  = int(os.environ.get("ST_HOLD_H", "12"))

FINE_SEC = 900
TP1, TP3 = 1.0, 2.0              # цели пока не трогаем — это следующий шаг
PARTS = (1 / 3, 1 / 3, 1 / 3)

STOP_GRID = [0.5, 0.75, 1.0, 1.5, 2.0, 2.5]
TRAIL_MODES = {
    "без подтяжки": (False, False),
    "безубыток после TP1": (True, False),
    "двойная (как сейчас)": (True, True),
}


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


def _sim(bars, side, entry, stop, t1, t2, t3, be=True, double=True):
    """be — переводим ли стоп в безубыток после первой цели.
    double — переносим ли его на уровень первой цели после второй."""
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
                if done == 1 and be:
                    cur_stop = entry
                elif done == 2 and double:
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
    wr = sum(1 for r in rs if r > 0) / n * 100
    return n, exp, 1.96 * se, wr


def _line(rs, label):
    st = _stat(rs)
    if not st:
        return f"  {label}: сделок нет"
    n, exp, ci, wr = st
    mark = "✅" if exp - ci > 0 else "❌" if exp + ci < 0 else "  "
    return f"  {mark} {label}: {n:4} сд, ВР {wr:3.0f}%, <b>{exp:+.3f}R</b> (±{ci:.3f})"


def run():
    import random as _rnd
    rng = _rnd.Random(9001)
    pairs = B.UPSCALE_PAIRS[:PAIRS_N] if PAIRS_N else B.UPSCALE_PAIRS
    hold = max(6, int(HOLD_H * 3600 / FINE_SEC))

    setups = []          # (сегмент свечей, side, px, bnd)
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
            if k is None or k + hold + 2 >= len(fine):
                continue
            px = fine[k]["o"]
            is_l = c["side"] == "long"
            bnd = c["hi"] if is_l else c["lo"]
            room = (bnd - px) / px * 100 if is_l else (px - bnd) / px * 100
            if room < DIST:
                continue
            seg = fine[k:k + hold]
            setups.append((seg, c["side"], px, bnd))
            last_end = seg[-1].get("t", 0)
        if i % 20 == 0:
            print(f"[STOP] {i}/{len(pairs)} | сделок {len(setups)} | {time.time()-t0:.0f}с")

    def collect(stop_pct, be, double, coin=False):
        out = []
        for seg, side, px, bnd in setups:
            sd = rng.choice(("long", "short")) if coin else side
            is_l = sd == "long"
            b = bnd if sd == side else (px * (1 + DIST / 100) if is_l else px * (1 - DIST / 100))
            ent = px * (1 + SLIP / 100) if is_l else px * (1 - SLIP / 100)
            stp = px * (1 - stop_pct / 100) if is_l else px * (1 + stop_pct / 100)
            y1 = px * (1 + TP1 / 100) if is_l else px * (1 - TP1 / 100)
            y3 = px * (1 + TP3 / 100) if is_l else px * (1 - TP3 / 100)
            r = _sim(seg, sd, ent, stp, y1, b, y3, be=be, double=double)
            if r is not None:
                out.append(r)
        return out

    L = [f"🛑 <b>ШАГ 1: СТОП И ПОДТЯЖКА</b> (~{cov:.0f} дн, {len(pairs)} пар)",
         f"<i>сигнал зафиксирован: заряд 1ч, ход до границы ≥{DIST}%. "
         f"Цели пока прежние ({TP1}% → граница → {TP3}%)</i>",
         f"Проскальзывание {SLIP}%, издержки {COST}%, стоп исполняется хуже на {STOP_SLIP}%",
         f"Зарядов: {n_ch} | сделок в выборке: {len(setups)}", ""]

    best = None
    for name, (be, dbl) in TRAIL_MODES.items():
        L.append(f"<b>{name}:</b>")
        for sp in STOP_GRID:
            rs = collect(sp, be, dbl)
            cl = collect(sp, be, dbl, coin=True)
            st, sc = _stat(rs), _stat(cl)
            if not st:
                continue
            edge = st[1] - sc[1] if sc else 0
            L.append(_line(rs, f"стоп {sp}%") + f"  <i>монетка {sc[1]:+.3f}R, эдж {edge:+.3f}R</i>")
            if st[0] >= 100 and (best is None or st[1] > best[3]):
                best = (name, sp, st[0], st[1], edge)
        L.append("")

    if best:
        L.append(f"→ <b>лучшее: {best[0]}, стоп {best[1]}%</b> — {best[2]} сд, "
                 f"{best[3]:+.3f}R, эдж над монеткой {best[4]:+.3f}R")
        L.append("<i>если лучшее всё ещё в минусе — дело не в стопе, "
                 "переходим к целям</i>")

    msg = "\n".join(L)
    print(msg.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    try:
        B.send_blocks(msg.split("\n"))
    except Exception as e:
        print(f"[STOP] отправка не удалась: {e}")


def main():
    try:
        run()
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            B.send_telegram(f"⚠️ Прогон стопа упал: {e}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
