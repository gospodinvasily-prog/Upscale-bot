"""
bt_loose.py — что будет, если ослабить условия заряда.

Диагностика (bt_why) показала: порог силы отсеивает 1888 из 1889 кандидатов —
без данных по OI набрать 6 очков почти невозможно, нужно идеальное совпадение
четырёх условий сразу. Здесь проверяем, что даст снижение порогов И НЕ ПОТЕРЯЕМ ЛИ
при этом преимущество.

Считается за ОДИН проход: детектор запускается с самыми мягкими порогами, у каждого
заряда запоминаются фактические сила и объём, а варианты отбираются уже потом.
Поэтому сравнение честное — одни и те же сделки, разные отсечки.

Торгуем по лучшей схеме из bt_long: вход по уклону, стоп 1%, три цели по трети
(1% → граница коридора → 2%), стоп подтягивается после каждой из первых двух.

Запуск: RUN_BACKTEST=loose
Настройки: LO_DAYS (60), LO_TF (15m), LO_PAIRS (0=все), LO_SLIP (0.25)
"""
import os
import time
import statistics

import bot as B

DAYS    = int(os.environ.get("LO_DAYS", "60"))
FINE_TF = os.environ.get("LO_TF", "15m")
PAIRS_N = int(os.environ.get("LO_PAIRS", "0"))
FEE_PCT = float(os.environ.get("LO_FEE", "0.05"))
SLIP    = float(os.environ.get("LO_SLIP", "0.25"))
HOLD_H  = int(os.environ.get("LO_HOLD_H", "12"))
TF_SEC = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}

SCORE_GRID = [6, 5, 4, 3, 2]      # 6 — как сейчас
RVOL_GRID  = [1.3, 1.1, 1.0, 0.8] # 1.3 — как сейчас
TP1, TP3 = 1.0, 2.0               # лучшая схема из bt_long
STOP = 1.0


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


def _sim3(bars, side, entry, stop, t1, t2, t3):
    """Три цели по трети, стоп: после TP1 в безубыток, после TP2 на цену TP1."""
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
    parts = (1 / 3, 1 / 3, 1 / 3)
    done, cur_stop, acc = 0, stop, 0.0
    for c in bars:
        if (c["l"] <= cur_stop) if is_long else (c["h"] >= cur_stop):
            left = sum(parts[done:])
            r = (cur_stop - entry) / risk if is_long else (entry - cur_stop) / risk
            return acc + r * left - FEE_PCT / 100 * entry / risk
        while done < 3:
            t = tg[done]
            if (c["h"] >= t) if is_long else (c["l"] <= t):
                acc += parts[done] * (abs(t - entry) / risk)
                done += 1
                if done == 1:
                    cur_stop = entry
                elif done == 2:
                    cur_stop = tg[0]
            else:
                break
        if done >= 3:
            return acc - FEE_PCT / 100 * entry / risk
    last = bars[-1]["c"] if bars else entry
    left = sum(parts[done:])
    r = (last - entry) / risk if is_long else (entry - last) / risk
    return acc + r * left - FEE_PCT / 100 * entry / risk


def _line(rs, label):
    if not rs:
        return f"  {label}: сделок нет"
    n = len(rs)
    exp = sum(rs) / n
    wins = sum(1 for r in rs if r > 0)
    gl = abs(sum(r for r in rs if r <= 0)) or 0.0
    pf = (sum(r for r in rs if r > 0) / gl) if gl > 0 else float("inf")
    se = (statistics.pstdev(rs) / (n ** 0.5)) if n > 1 else 0.0
    mark = "✅" if exp - 1.96 * se > 0 else "❌" if exp + 1.96 * se < 0 else "  "
    return (f"  {mark} {label}: {n:4} сд, ВР {wins/n*100:3.0f}%, <b>{exp:+.3f}R</b> "
            f"(±{1.96*se:.3f}), ПФ {pf:.2f}, {sum(rs):+.0f}R")


def run():
    pairs = B.UPSCALE_PAIRS[:PAIRS_N] if PAIRS_N else B.UPSCALE_PAIRS
    step = TF_SEC[FINE_TF]
    hold = max(6, int(HOLD_H * 3600 / step))

    # один проход с самыми мягкими порогами, отбор — потом
    saved_score, saved_rvol = B.ACC_MIN_SCORE, B.ACC_RVOL_MIN
    B.ACC_MIN_SCORE = min(SCORE_GRID)
    B.ACC_RVOL_MIN = min(RVOL_GRID)

    trades = []          # (score, rvol, R)
    n_ch = 0
    t0 = time.time()
    cov = 0.0

    try:
        for i, sym in enumerate(pairs, 1):
            try:
                fine = _fetch(sym, FINE_TF, DAYS)
                base = _fetch(sym, "1h", DAYS + 5)
            except Exception:
                continue
            if len(fine) < 500 or len(base) < 150:
                continue
            fine, base = fine[:-1], base[:-1]
            if not cov:
                cov = (fine[-1].get("t", 0) - fine[0].get("t", 0)) / 86400
            idx = {c.get("t"): k for k, c in enumerate(fine)}
            fine_from = fine[0].get("t", 0)
            last_ts = 0

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
                if cts < fine_from or cts <= last_ts:
                    continue
                c = B.detect_charge(sym, upto, upto[-1]["c"], vb, ab,
                                    {"btc_chg_win": 0.0, "do_charge": True},
                                    {"funding": 0.0, "change_24h": 0.0},
                                    lambda s: None, P=B.ALT_P)
                if not c or c["side"] not in ("long", "short"):
                    continue
                n_ch += 1
                k0 = idx.get(cts)
                if k0 is None:
                    for off in range(1, int(3600 / step) + 1):
                        k0 = idx.get(cts + off * step)
                        if k0 is not None:
                            break
                if k0 is None:
                    continue
                px = fine[k0]["c"]
                is_l = c["side"] == "long"
                bnd = c["hi"] if is_l else c["lo"]
                room = (bnd - px) / px * 100 if is_l else (px - bnd) / px * 100
                if room < B.TILT_MIN_DIST_PCT:
                    continue
                fut = fine[k0 + 1:k0 + 1 + hold]
                if len(fut) < 4:
                    continue
                ent = px * (1 + SLIP / 100) if is_l else px * (1 - SLIP / 100)
                stp = px * (1 - STOP / 100) if is_l else px * (1 + STOP / 100)
                y1 = px * (1 + TP1 / 100) if is_l else px * (1 - TP1 / 100)
                y3 = px * (1 + TP3 / 100) if is_l else px * (1 - TP3 / 100)
                r = _sim3(fut, c["side"], ent, stp, y1, bnd, y3)
                if r is None:
                    continue
                trades.append((c["score"], c.get("rvol_half", 0), r))
                last_ts = fut[-1].get("t", 0)
            if i % 20 == 0:
                print(f"[LOOSE] {i}/{len(pairs)} | зарядов {n_ch} | сделок {len(trades)} "
                      f"| {time.time()-t0:.0f}с")
    finally:
        B.ACC_MIN_SCORE, B.ACC_RVOL_MIN = saved_score, saved_rvol

    took = time.time() - t0
    L = [f"🔓 <b>Что даст ослабление условий заряда</b> (1h заряд, сделки по {FINE_TF}, "
         f"~{cov:.0f} дн, {len(pairs)} пар)",
         f"Схема: вход по уклону, стоп {STOP}%, три цели по трети "
         f"({TP1}% → граница → {TP3}%), стоп подтягивается",
         f"Проскальзывание {SLIP}%, комиссия {FEE_PCT}%, время {took/60:.1f} мин",
         f"Всего кандидатов при самых мягких порогах: {len(trades)}",
         "<i>OI за историю не восстановить — сила ниже живой на 2-3, "
         "поэтому смотри на СРАВНЕНИЕ вариантов, а не на абсолютный порог</i>",
         "", "<b>Порог силы заряда</b> (объём ≥1.3 как сейчас):"]
    for s_ in SCORE_GRID:
        rs = [r for sc, rv, r in trades if sc >= s_ and rv >= 1.3]
        L.append(_line(rs, f"сила ≥{s_}" + (" (как сейчас)" if s_ == 6 else "")))

    L += ["", "<b>Порог объёма</b> (сила ≥6 как сейчас):"]
    for v_ in RVOL_GRID:
        rs = [r for sc, rv, r in trades if sc >= 6 and rv >= v_]
        L.append(_line(rs, f"объём ≥{v_}×" + (" (как сейчас)" if v_ == 1.3 else "")))

    L += ["", "<b>Оба вместе</b> — ищем, где больше сделок без потери качества:"]
    best = None
    for s_ in SCORE_GRID:
        row = []
        for v_ in RVOL_GRID:
            rs = [r for sc, rv, r in trades if sc >= s_ and rv >= v_]
            e = sum(rs) / len(rs) if rs else 0
            row.append(f"{len(rs):4}шт {e:+.2f}")
            if rs and len(rs) >= 30:
                se = (statistics.pstdev(rs) / (len(rs) ** 0.5)) if len(rs) > 1 else 0
                if e - 1.96 * se > 0 and (best is None or len(rs) > best[2]):
                    best = (s_, v_, len(rs), e)
        L.append(f"  сила ≥{s_} | " + " | ".join(f"об.{v_}: {x}" for v_, x in zip(RVOL_GRID, row)))
    if best:
        L.append(f"  → <b>больше всего сделок при уверенном плюсе: сила ≥{best[0]}, "
                 f"объём ≥{best[1]} — {best[2]} сделок, {best[3]:+.3f}R</b>")
    else:
        L.append("  → ни одно сочетание не дало уверенного плюса на выборке ≥30 сделок")

    msg = "\n".join(L)
    print(msg.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    try:
        B.send_blocks(msg.split("\n"))
    except Exception as e:
        print(f"[LOOSE] отправка не удалась: {e}")


def main():
    try:
        run()
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            B.send_telegram(f"⚠️ Прогон ослаблений упал: {e}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
