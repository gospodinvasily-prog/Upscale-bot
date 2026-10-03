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
from datetime import datetime, timezone

import bot as B

DAYS    = int(os.environ.get("LO_DAYS", "60"))
FINE_TF = os.environ.get("LO_TF", "15m")
PAIRS_N = int(os.environ.get("LO_PAIRS", "0"))
FEE_PCT = float(os.environ.get("LO_FEE", "0.05"))
SLIP    = float(os.environ.get("LO_SLIP", "0.25"))
HOLD_H  = int(os.environ.get("LO_HOLD_H", "12"))
OFFSET  = int(os.environ.get("LO_OFFSET", "0"))   # на сколько дней сдвинуть окно назад
TF_SEC = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}

SCORE_GRID = [6, 5, 4, 3, 2]      # 6 — как было до v9.2
RVOL_GRID  = [1.3, 1.1, 1.0, 0.8] # 1.3 — как было до v9.2
SQ_GRID    = [25, 30, 35, 45]     # процентиль сжатия: 25 — как сейчас
RNG_GRID   = [9.0, 10.5, 12.0, 15.0]  # жёсткий предел размаха: 9 — как сейчас
TP1, TP3 = 1.0, 2.0               # лучшая схема из bt_long
STOP = 1.0
# Как считать ТРЕТЬЮ цель. Сейчас в боте: вход + TP3%. Беда в том, что при широком
# коридоре эта цена оказывается ВНУТРИ коридора, ниже границы, и код отодвигает её
# на символические 0.1% за TP2 — то есть третья цель вырождается в дубль второй.
# Проверяем вариант «от ГРАНИЦЫ»: граница + доля высоты коридора. Тогда цель всегда
# стоит за пробоем, независимо от ширины.
# Фильтр VWAP для УКЛОНА никогда не проверялся: бэктест (+0.32R) торговал заряды
# подряд, без него. Сейчас в боте стоит 2.5 ATR по инерции от ПРОБОЯ, который отключён.
VWAP_GRID = [None, 1.5, 2.0, 2.5, 3.0, 4.0]   # None = фильтра нет
# Минимальный ход до границы коридора. Сейчас 1%. По живому журналу за 01.10 этот
# фильтр отсеял 24 заряда из 46 — больше, чем все остальные вместе. Причина понятна:
# когда уклон шортовый, цена прижата к НИЖНЕЙ границе, и ходу вниз мало по определению.
# Но порог 1% стоял и в бэктесте, давшем +0.32R, — возможно, он не режет, а защищает.
DIST_GRID = [0.0, 0.5, 0.75, 1.0, 1.5, 2.0]
# Перекрёстная таблица: расстояние до границы × порог VWAP. Плюс замер ПАЧЕК —
# сколько входов приходится на один скан в одну сторону. Пачка из пяти позиций
# это одна ставка: при развороте выбивает все разом, и бэктест этого не видит,
# потому что считает сделки независимыми.
CROSS_DIST = [1.0, 1.5, 2.0]
CROSS_VWAP = [2.0, 2.5]
# ПРОБОЙ на новых зарядах и по НОВОЙ схеме выходов. Раньше его гоняли при старых
# порогах заряда и со старыми целями — схему «три цели по трети + двойная подтяжка»
# на нём не проверяли ни разу, а именно она вытащила УКЛОН (+0.174R → +0.264R).
BRK_SCORE = [2, 4]               # порог силы заряда (шкала бэктеста, живая выше на 2-3)
BRK_RVOL  = [1.0, 1.5]           # объём свечи пробоя на мелком ТФ
BRK_VWAP  = [None, 2.0, 2.5, 3.0]  # VWAP для ПРОБОЯ: None = без фильтра
# Разбор по часам МСК. Наборы окон для сравнения целиком — отдельные часы шумят
# (на ~60 сделках погрешность ±0.15R), поэтому смотрим и зоны, и итог по набору.
WINDOW_SETS = {
    "сейчас (04-08:30, 10:30-11:30, 14:30-21:30)":
        [(4, 0, 8, 30), (10, 30, 11, 30), (14, 30, 21, 30)],
    "без окон (круглосуточно)": [(0, 0, 24, 0)],
    "сейчас минус 16:00-17:00":
        [(4, 0, 8, 30), (10, 30, 11, 30), (14, 30, 16, 0), (17, 0, 21, 30)],
    "сейчас + Нью-Йорк до 00:00":
        [(4, 0, 8, 30), (10, 30, 11, 30), (14, 30, 24, 0)],
    "только Европа+NY (14:30-23:00)": [(14, 30, 23, 0)],
    "только Азия (04:00-11:30)": [(4, 0, 11, 30)],
}
TP3_MODES = [("от входа +2% (как сейчас)", None),
             ("граница + 25% высоты", 0.25),
             ("граница + 50% высоты", 0.50),
             ("граница + 75% высоты", 0.75),
             ("граница + 100% высоты", 1.00)]


def _fetch(sym, tf, days):
    """OFFSET сдвигает окно НАЗАД: LO_OFFSET=60 даст предыдущие 60 дней, а не последние.
    Это проверка на подгонку — если на другом периоде цифры похожие, эдж настоящий."""
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


def _vwap_dist(fine, k, bars_day):
    """Насколько цена ушла от дневного VWAP, в ATR. Только прошлые свечи."""
    lo = max(0, k - bars_day)
    seg = fine[lo:k + 1]
    if len(seg) < 10:
        return None
    vv = sum(c["v"] for c in seg)
    if vv <= 0:
        return None
    vwap = sum((c["h"] + c["l"] + c["c"]) / 3 * c["v"] for c in seg) / vv
    trs = B.true_ranges(seg)
    atr = B.trimmed_mean(trs) if trs else 0
    return abs(fine[k]["c"] - vwap) / atr if atr else None


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
    bars_day = int(24 * 3600 / step)

    # один проход с самыми мягкими порогами, отбор — потом
    saved = (B.ACC_MIN_SCORE, B.ACC_RVOL_MIN, B.ACC_SQUEEZE_PCTL, B.ACC_MAX_RANGE_ABS)
    B.ACC_MIN_SCORE = min(SCORE_GRID)
    B.ACC_RVOL_MIN = min(RVOL_GRID)
    B.ACC_SQUEEZE_PCTL = max(SQ_GRID)
    B.ACC_MAX_RANGE_ABS = max(RNG_GRID)

    trades = []          # (score, rvol, sq, tr, rng, R)
    tp3res = {name: [] for name, _ in TP3_MODES}
    vwres = {v: [] for v in VWAP_GRID}
    distres = {v: [] for v in DIST_GRID}
    cross = {(d_, v_): [] for d_ in CROSS_DIST for v_ in CROSS_VWAP}
    batches = {(d_, v_): {} for d_ in CROSS_DIST for v_ in CROSS_VWAP}
    brk = {(sc, rv, vw): [] for sc in BRK_SCORE for rv in BRK_RVOL for vw in BRK_VWAP}
    byhour = {h: [] for h in range(24)}     # час входа по МСК -> результаты
    byday = {d: [] for d in range(7)}       # день недели (0=пн) -> результаты
    bydate = {1.0: {}, 1.5: {}}             # дата -> [результаты] для двух порогов
    bywidth = {}                            # ширина коридора -> результаты
    # КОНТРОЛЬ: то же самое, но вход в СЛУЧАЙНЫЙ момент. Если преимущество даёт схема
    # выходов (три цели + двойная подтяжка), а не сигнал, случайный вход покажет тот же
    # плюс. Если сигнал настоящий — случайный уйдёт в ноль или минус.
    ctl_time, ctl_dir = [], []
    import random as _rnd
    _rnd.seed(12345)
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
            # норма объёма мелкой свечи — по первой четверти ряда (только прошлое)
            vol_fine = B.trimmed_mean([x["v"] for x in fine[:max(50, len(fine) // 4)]]) or 0
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
                if room < min(DIST_GRID):          # самый мягкий порог сетки
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
                for dt_ in DIST_GRID:
                    if room >= dt_:
                        distres[dt_].append(r)
                vd0 = _vwap_dist(fine, k0, bars_day)
                scan_key = (int(cts) // 1800, c["side"])   # скан раз в 30 мин + сторона
                for d_ in CROSS_DIST:
                    for v_ in CROSS_VWAP:
                        if room >= d_ and (vd0 is None or vd0 <= v_):
                            cross[(d_, v_)].append(r)
                            b = batches[(d_, v_)]
                            b[scan_key] = b.get(scan_key, 0) + 1
                hmsk = (datetime.fromtimestamp(cts, timezone.utc).hour + 3) % 24
                byhour[hmsk].append((r, room, vd0))
                _dt = datetime.fromtimestamp(cts, timezone.utc)
                _msk = _dt.timestamp() + 3 * 3600
                _d = datetime.fromtimestamp(_msk, timezone.utc)
                byday[_d.weekday()].append((r, room, vd0))
                # ширина коридора: влияет ли она на результат?
                if room >= B.TILT_MIN_DIST_PCT and (vd0 is None or vd0 <= B.VWAP_MAX_ATR):
                    w_ = (c["hi"] - c["lo"]) / c["lo"] * 100 if c["lo"] > 0 else 0
                    for lo_w, hi_w in ((0, 2), (2, 3), (3, 4), (4, 5), (5, 7), (7, 99)):
                        if lo_w <= w_ < hi_w:
                            bywidth.setdefault((lo_w, hi_w), []).append(r)
                            break
                for thr in (1.0, 1.5):      # кривая счёта по дням для двух порогов
                    if room >= thr and (vd0 is None or vd0 <= B.VWAP_MAX_ATR):
                        bydate[thr].setdefault(_d.strftime("%Y-%m-%d"), []).append(r)
                if room < B.TILT_MIN_DIST_PCT:     # дальше — только то, что берёт бот
                    continue
                # фильтр VWAP: на тех же входах, разные пороги
                vd = _vwap_dist(fine, k0, bars_day)
                for vt in VWAP_GRID:
                    if vt is None or vd is None or vd <= vt:
                        vwres[vt].append(r)
                # варианты третьей цели — на тех же входах
                hgt = c["hi"] - c["lo"]
                for name, frac in TP3_MODES:
                    if frac is None:
                        t3 = y3
                    else:
                        t3 = (bnd + hgt * frac) if is_l else (bnd - hgt * frac)
                    rr = _sim3(fut, c["side"], ent, stp, y1, bnd, t3)
                    if rr is not None:
                        tp3res[name].append(rr)
                # контроль 1: случайный момент входа, всё остальное то же
                for _ in range(2):
                    kr = _rnd.randrange(50, max(51, len(fine) - hold - 2))
                    pr = fine[kr]["c"]
                    roomr = (bnd - pr) / pr * 100 if is_l else (pr - bnd) / pr * 100
                    if roomr < B.TILT_MIN_DIST_PCT:
                        continue
                    er = pr * (1 + SLIP / 100) if is_l else pr * (1 - SLIP / 100)
                    sr = pr * (1 - STOP / 100) if is_l else pr * (1 + STOP / 100)
                    a1 = pr * (1 + TP1 / 100) if is_l else pr * (1 - TP1 / 100)
                    a3 = pr * (1 + TP3 / 100) if is_l else pr * (1 - TP3 / 100)
                    fr = fine[kr + 1:kr + 1 + hold]
                    if len(fr) < 4:
                        continue
                    rr_ = _sim3(fr, c["side"], er, sr, a1, bnd, a3)
                    if rr_ is not None:
                        ctl_time.append(rr_)
                    break
                # контроль 2: тот же момент, но направление монеткой
                sd_r = _rnd.choice(("long", "short"))
                isr = sd_r == "long"
                bndr = c["hi"] if isr else c["lo"]
                roomr = (bndr - px) / px * 100 if isr else (px - bndr) / px * 100
                if roomr >= B.TILT_MIN_DIST_PCT:
                    er = px * (1 + SLIP / 100) if isr else px * (1 - SLIP / 100)
                    sr = px * (1 - STOP / 100) if isr else px * (1 + STOP / 100)
                    a1 = px * (1 + TP1 / 100) if isr else px * (1 - TP1 / 100)
                    a3 = px * (1 + TP3 / 100) if isr else px * (1 - TP3 / 100)
                    rr_ = _sim3(fut, sd_r, er, sr, a1, bndr, a3)
                    if rr_ is not None:
                        ctl_dir.append(rr_)
                # ── ПРОБОЙ по новой схеме: ждём закрытия за уровнем ──
                watch_n = max(6, int(B.WATCH_TTL_HOURS * 3600 / step))
                hi_t = B.order_trigger(c["hi"], True, c["atr"])
                lo_t = B.order_trigger(c["lo"], False, c["atr"])
                for kb in range(k0 + 1, min(k0 + watch_n, len(fine) - hold - 2)):
                    bar_b = fine[kb]
                    sd_b = ("long" if bar_b["c"] > hi_t else
                            "short" if bar_b["c"] < lo_t else None)
                    if sd_b is None:
                        continue
                    fb = fine[kb + 1:kb + 1 + hold]
                    if len(fb) < 4:
                        break
                    pxb = bar_b["c"]
                    ilb = sd_b == "long"
                    eb = pxb * (1 + SLIP / 100) if ilb else pxb * (1 - SLIP / 100)
                    sb = pxb * (1 - STOP / 100) if ilb else pxb * (1 + STOP / 100)
                    b1 = pxb * (1 + TP1 / 100) if ilb else pxb * (1 - TP1 / 100)
                    b2 = c["hi"] if ilb else c["lo"]          # граница, из которой вышли
                    hg = c["hi"] - c["lo"]
                    b3 = (c["hi"] + hg * 0.5) if ilb else (c["lo"] - hg * 0.5)
                    rb = _sim3(fb, sd_b, eb, sb, b1, b2, b3)
                    if rb is not None:
                        rvb = bar_b["v"] / vol_fine if vol_fine else 0
                        vdb = _vwap_dist(fine, kb, bars_day)   # VWAP в момент ПРОБОЯ
                        for sc_ in BRK_SCORE:
                            for rv_ in BRK_RVOL:
                                for vw_ in BRK_VWAP:
                                    if (c["score"] >= sc_ and rvb >= rv_
                                            and (vw_ is None or vdb is None or vdb <= vw_)):
                                        brk[(sc_, rv_, vw_)].append(rb)
                    break

                trades.append((c["score"], c.get("rvol_half", 0),
                               c.get("sq_pct"), c.get("tr_ratio", 9),
                               c.get("rng_pct", 0), r))
                last_ts = fut[-1].get("t", 0)
            if i % 20 == 0:
                print(f"[LOOSE] {i}/{len(pairs)} | зарядов {n_ch} | сделок {len(trades)} "
                      f"| {time.time()-t0:.0f}с")
    finally:
        (B.ACC_MIN_SCORE, B.ACC_RVOL_MIN,
         B.ACC_SQUEEZE_PCTL, B.ACC_MAX_RANGE_ABS) = saved

    took = time.time() - t0
    L = [f"🔓 <b>Что даст ослабление условий заряда</b> (1h заряд, сделки по {FINE_TF}, "
         f"~{cov:.0f} дн, {len(pairs)} пар)"
         + (f"\n⏪ <b>ПЕРИОД СДВИНУТ НАЗАД НА {OFFSET} ДНЕЙ</b> — проверка на подгонку: "
            f"сравнивай с прогоном без сдвига" if OFFSET else ""),
         f"Схема: вход по уклону, стоп {STOP}%, три цели по трети "
         f"({TP1}% → граница → {TP3}%), стоп подтягивается",
         f"Проскальзывание {SLIP}%, комиссия {FEE_PCT}%, время {took/60:.1f} мин",
         f"Всего кандидатов при самых мягких порогах: {len(trades)}",
         "<i>OI за историю не восстановить — сила ниже живой на 2-3, "
         "поэтому смотри на СРАВНЕНИЕ вариантов, а не на абсолютный порог</i>",
         "", "<b>Порог силы заряда</b> (объём ≥1.3 как сейчас):"]
    def sel(score_thr=5, rvol_thr=1.0, sq_thr=25, rng_thr=9.0):
        """Отбор под заданные пороги. Сила пересчитана: прогон шёл с мягким
        порогом сжатия, при строгом отборе лишнее очко за сжатие снимаем."""
        out = []
        for sc, rv, sq, tr, rng, r in trades:
            adj = sc
            if sq is not None and sq > sq_thr:
                if sq <= max(SQ_GRID):
                    adj -= 1                     # очко за сжатие не положено
                squeezed = tr <= B.ACC_TR_RATIO_MAX
            else:
                squeezed = True
            if not squeezed:
                continue
            if adj >= score_thr and rv >= rvol_thr and rng <= rng_thr:
                out.append(r)
        return out

    for s_ in SCORE_GRID:
        L.append(_line(sel(score_thr=s_), f"сила ≥{s_}" + (" (сейчас 5)" if s_ == 5 else "")))

    L += ["", "<b>Порог объёма</b> (сила ≥5, сжатие ≤25, размах ≤9%):"]
    for v_ in RVOL_GRID:
        L.append(_line(sel(rvol_thr=v_), f"объём ≥{v_}×" + (" (сейчас)" if v_ == 1.0 else "")))

    L += ["", "<b>Процентиль сжатия</b> — НЕ проверялся раньше (сила ≥5, объём ≥1.0):"]
    for q_ in SQ_GRID:
        L.append(_line(sel(sq_thr=q_), f"сжатие ≤{q_}" + (" (сейчас)" if q_ == 25 else "")))

    L += ["", "<b>Предел размаха коридора</b> — НЕ проверялся раньше:"]
    for g_ in RNG_GRID:
        L.append(_line(sel(rng_thr=g_), f"размах ≤{g_}%" + (" (сейчас)" if g_ == 9.0 else "")))

    L += ["", "<b>КАК СЧИТАТЬ ТРЕТЬЮ ЦЕЛЬ</b> (сила ≥4, объём ≥0.8 — как в боте):",
          "  <i>сейчас цель = вход +2%. При широком коридоре она попадает ВНУТРЬ коридора,",
          "   код отодвигает её на 0.1% за вторую — и третья цель вырождается в дубль второй</i>"]
    for name, _f in TP3_MODES:
        L.append(_line(tp3res[name], name))

    L += ["", "<b>МИНИМАЛЬНЫЙ ХОД ДО ГРАНИЦЫ</b> — главный резак по живому журналу",
          "  <i>за 01.10 отсеял 24 заряда из 46. При шортовом уклоне цена прижата",
          "   к нижней границе, и ходу мало по определению. Режет или защищает?</i>"]
    for dt_ in DIST_GRID:
        L.append(_line(distres[dt_], ("без фильтра" if dt_ == 0 else f"ход ≥{dt_}%")
                       + (" (сейчас)" if dt_ == 1.0 else "")))
    cur = distres.get(1.0) or []
    if cur:
        e_cur = sum(cur) / len(cur)
        best = max(((v, a) for v, a in distres.items() if len(a) >= 100),
                   key=lambda kv: sum(kv[1]) / len(kv[1]), default=None)
        if best:
            e_b = sum(best[1]) / len(best[1])
            L.append(f"  → лучший порог {best[0]}%: {len(best[1])} сд, {e_b:+.3f}R против "
                     f"{len(cur)} сд и {e_cur:+.3f}R при нынешнем 1%")

    L += ["", "═══ <b>РАССТОЯНИЕ × VWAP: меньше ли станет пачек?</b> ═══",
          "  <i>пачка — сколько входов в одну сторону на одном скане. Пять позиций",
          "   разом это одна ставка: разворот выбивает все сразу</i>", ""]
    for d_ in CROSS_DIST:
        for v_ in CROSS_VWAP:
            rs = cross[(d_, v_)]
            b = batches[(d_, v_)]
            if not rs:
                L.append(f"  ход ≥{d_}% + VWAP ≤{v_}: сделок нет")
                continue
            n_ = len(rs)
            e_ = sum(rs) / n_
            se = (statistics.pstdev(rs) / (n_ ** 0.5)) if n_ > 1 else 0
            mark = "✅" if e_ - 1.96 * se > 0 else "  "
            sizes = sorted(b.values())
            avg_b = sum(sizes) / len(sizes) if sizes else 0
            big = sum(1 for x in sizes if x >= 4)
            L.append(f"  {mark} <b>ход ≥{d_}% + VWAP ≤{v_}</b>: {n_:4} сд, "
                     f"ВР {sum(1 for x in rs if x > 0)/n_*100:3.0f}%, <b>{e_:+.3f}R</b> "
                     f"(±{1.96*se:.3f}), итого {sum(rs):+.0f}R")
            L.append(f"       пачки: в среднем {avg_b:.1f} входа на скан, "
                     f"максимум {max(sizes)}, пачек по 4+ — {big}")
    base_c = cross.get((1.0, 2.5)) or []
    if base_c:
        e0 = sum(base_c) / len(base_c)
        b0 = batches[(1.0, 2.5)]
        s0 = sorted(b0.values())
        L.append("")
        L.append(f"  <i>сейчас в боте: ход ≥1.0% + VWAP ≤2.5 — {len(base_c)} сд, {e0:+.3f}R, "
                 f"пачки в среднем {sum(s0)/len(s0):.1f}, максимум {max(s0)}</i>")

    L += ["", "<b>ФИЛЬТР VWAP для УКЛОНА</b> — никогда не проверялся",
          "  <i>в боте стоит 2.5 ATR по инерции от ПРОБОЯ. Бэктест, давший +0.32R,",
          "   работал БЕЗ него — торговал заряды подряд</i>"]
    for vt in VWAP_GRID:
        L.append(_line(vwres[vt], "без фильтра" if vt is None else
                       f"не дальше {vt} ATR от VWAP" + (" (сейчас)" if vt == 2.5 else "")))
    _off = vwres.get(None) or []
    if _off:
        e_off = sum(_off) / len(_off)
        cand = [(v, a) for v, a in vwres.items() if v is not None and len(a) >= 100]
        if cand:
            bv, ba = max(cand, key=lambda kv: sum(kv[1]) / len(kv[1]))
            e_b = sum(ba) / len(ba)
            L.append(f"  → лучший порог {bv} ATR: {len(ba)} сд, {e_b:+.3f}R против "
                     f"{len(_off)} сд и {e_off:+.3f}R без фильтра "
                     f"({'стоит оставить' if e_b - e_off > 0.02 else 'разницы нет — можно снять'})")

    # ── разбор по часам ──
    def _st(rs):
        if not rs:
            return 0, 0.0, 0.0, 0.0
        n = len(rs)
        e = sum(rs) / n
        se = (statistics.pstdev(rs) / (n ** 0.5)) if n > 1 else 0
        return n, e, 1.96 * se, sum(1 for x in rs if x > 0) / n * 100

    cur_d, cur_v = B.TILT_MIN_DIST_PCT, B.VWAP_MAX_ATR
    hr = {h: [r for r, room, vd in byhour[h]
              if room >= cur_d and (vd is None or vd <= cur_v)] for h in range(24)}
    L += ["", "═══ <b>РАЗБОР ПО ЧАСАМ (МСК)</b> ═══",
          f"  <i>при нынешних настройках: ход ≥{cur_d}%, VWAP ≤{cur_v}</i>",
          "  час | сделок | ВР  | матожидание      | 3 часа подряд"]
    for h in range(24):
        n, e, ci, wr = _st(hr[h])
        if not n:
            continue
        # скользящее по трём часам — сглаживает шум одиночного часа
        sm = hr[(h - 1) % 24] + hr[h] + hr[(h + 1) % 24]
        n3, e3, _, _ = _st(sm)
        mark = "✅" if e - ci > 0 else "❌" if e + ci < 0 else "  "
        inw = "●" if any(h1 * 60 <= h * 60 + 30 < h2 * 60 + m2
                         for h1, m1, h2, m2 in B.TILT_WINDOWS) else "○"
        L.append(f"  {mark}{inw} {h:02d} | {n:5} | {wr:3.0f}% | {e:+.3f}R ±{ci:.3f} | "
                 f"{e3:+.3f}R ({n3})")
    L.append("  <i>● — час внутри нынешних окон, ○ — вне. ✅/❌ — отличие от нуля значимо</i>")

    L += ["", "<b>Наборы окон целиком:</b>"]
    def _in_set(h, mins, wins):
        t = h * 60 + mins
        return any(h1 * 60 + m1 <= t < h2 * 60 + m2 for h1, m1, h2, m2 in wins)
    for name, wins in WINDOW_SETS.items():
        rs = [r for h in range(24) for r in hr[h] if _in_set(h, 30, wins)]
        L.append(_line(rs, name))

    DN = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    dr = {d: [r for r, room, vd in byday[d]
              if room >= cur_d and (vd is None or vd <= cur_v)] for d in range(7)}
    L += ["", "<b>По дням недели (МСК):</b>"]
    for d in range(7):
        n, e, ci, wr = _st(dr[d])
        if not n:
            L.append(f"  {DN[d]}: сделок нет")
            continue
        mark = "✅" if e - ci > 0 else "❌" if e + ci < 0 else "  "
        L.append(f"  {mark} {DN[d]:13} {n:4} сд, ВР {wr:3.0f}%, {e:+.3f}R ±{ci:.3f}")
    wd = [r for d in range(5) for r in dr[d]]
    we = [r for d in (5, 6) for r in dr[d]]
    if wd and we:
        ew, ee = sum(wd) / len(wd), sum(we) / len(we)
        L.append(f"  будни {len(wd)} сд {ew:+.3f}R | выходные {len(we)} сд {ee:+.3f}R "
                 f"→ разница {ee - ew:+.3f}R")
    L.append("  <i>гипотезы без объяснения лучше не принимать: на ~160 сделках в день "
             "погрешность ±0.09R, и один день почти наверняка вылезет случайно</i>")

    # ── кривая счёта по ДНЯМ: пачка считается целиком, как на счёте ──
    if bywidth:
        L += ["", "<b>ШИРИНА КОРИДОРА — узкие лучше широких?</b>",
              "  <i>от неё зависит, где стоит вторая цель (на границе)</i>"]
        for k_ in sorted(bywidth):
            lbl = f"{k_[0]}–{k_[1]}%" if k_[1] < 99 else f"от {k_[0]}%"
            L.append(_line(bywidth[k_], f"коридор {lbl}"))

    L += ["", "═══ <b>ПО ДНЯМ: насколько больно бывает</b> ═══",
          "  <i>сделки одного дня складываются целиком — пять позиций в одну сторону",
          "   это одно событие, а не пять независимых. Риск $20, счёт $10 000,",
          "   лимиты Upscale: −$500 за день, −$1000 всего</i>"]
    for thr in (1.0, 1.5):
        dd = bydate[thr]
        if not dd:
            continue
        days_sorted = sorted(dd)
        dres = [(d, sum(dd[d]) * 20, len(dd[d])) for d in days_sorted]   # день -> $, сделок
        tot_ = sum(x[1] for x in dres)
        plus = [x for x in dres if x[1] > 0]
        worst = min(dres, key=lambda x: x[1])
        best = max(dres, key=lambda x: x[1])
        # просадка эквити по дням
        eq, peak, dd_max = 0.0, 0.0, 0.0
        streak, worst_streak = 0, 0
        hit500 = hit1000 = 0
        for _, p_, _n in dres:
            eq += p_
            peak = max(peak, eq)
            dd_max = min(dd_max, eq - peak)
            if p_ < 0:
                streak += 1
                worst_streak = max(worst_streak, streak)
            else:
                streak = 0
            if p_ <= -500:
                hit500 += 1
            if eq - peak <= -1000:
                hit1000 += 1
        # сколько дней до +$500 по реальной кривой
        run, to_goal = 0.0, None
        for i_, (_, p_, _n) in enumerate(dres, 1):
            run += p_
            if run >= 500 and to_goal is None:
                to_goal = i_
        mark = "ПОРОГ 1.0%" if thr == 1.0 else "ПОРОГ 1.5% (сейчас)"
        L += ["", f"  <b>{mark}</b> — {len(dres)} дней, {sum(x[2] for x in dres)} сделок",
              f"    итого ${tot_:+.0f} | прибыльных дней {len(plus)}/{len(dres)} "
              f"({len(plus)/len(dres)*100:.0f}%)",
              f"    лучший день ${best[1]:+.0f} ({best[2]} сд) | "
              f"худший ${worst[1]:+.0f} ({worst[2]} сд)",
              f"    макс. просадка эквити <b>${dd_max:.0f}</b> | "
              f"убыточных дней подряд максимум {worst_streak}",
              f"    дней с убытком ≥$500: <b>{hit500}</b> | "
              f"пробитий общего лимита −$1000: <b>{hit1000}</b>",
              f"    до цели +$500 дошли бы за <b>{to_goal if to_goal else '—'}</b> дней"]
        big = [x for x in dres if x[2] >= 5]
        if big:
            avgb = sum(x[1] for x in big) / len(big)
            L.append(f"    дни с 5+ сделками: {len(big)} шт, в среднем ${avgb:+.0f} за день")

    L += ["", "═══ <b>ПРОБОЙ на новых зарядах и по новой схеме</b> ═══",
          "  <i>раньше его гоняли при старых порогах и со старыми целями. Здесь —",
          "   три цели по трети (1% → граница коридора → +половина высоты) и двойная",
          "   подтяжка стопа, то есть ровно то, что вытащило УКЛОН</i>"]
    for sc_ in BRK_SCORE:
        for rv_ in BRK_RVOL:
            L.append(f"  <b>сила ≥{sc_}, объём свечи ≥{rv_}×:</b>")
            for vw_ in BRK_VWAP:
                lbl = "без VWAP" if vw_ is None else f"VWAP ≤{vw_}"
                L.append(_line(brk[(sc_, rv_, vw_)], "   " + lbl))
    allb = [r for v in brk.values() for r in v]
    if allb:
        best_b = max(((k, v) for k, v in brk.items() if len(v) >= 100),
                     key=lambda kv: sum(kv[1]) / len(kv[1]), default=None)
        if best_b:
            eb_ = sum(best_b[1]) / len(best_b[1])
            vwtxt = "без VWAP" if best_b[0][2] is None else f"VWAP ≤{best_b[0][2]}"
            L.append(f"  → лучшее: сила ≥{best_b[0][0]}, объём ≥{best_b[0][1]}×, {vwtxt} — "
                     f"{len(best_b[1])} сд, {eb_:+.3f}R")
            L.append("  <i>для сравнения: УКЛОН на тех же зарядах даёт около +0.3R. "
                     "Если ПРОБОЙ в плюсе — его можно вернуть вторым сигналом</i>")

    L += ["", "═══ <b>КОНТРОЛЬ: а не схема ли выходов даёт плюс?</b> ═══",
          "  <i>та же схема (3 цели по трети, двойная подтяжка стопа), но вход",
          "   не по сигналу. Если плюс останется — сигнал ничего не стоит</i>"]
    L.append(_line(sel(4, 0.8), "НАШ СИГНАЛ (сила ≥4, объём ≥0.8)"))
    L.append(_line(ctl_time, "тот же коридор и направление, вход в СЛУЧАЙНЫЙ момент"))
    L.append(_line(ctl_dir, "тот же момент, направление МОНЕТКОЙ"))
    base_r = sel(4, 0.8)
    if base_r and ctl_time:
        d1 = sum(base_r) / len(base_r) - sum(ctl_time) / len(ctl_time)
        d2 = (sum(base_r) / len(base_r) - sum(ctl_dir) / len(ctl_dir)) if ctl_dir else 0
        L.append(f"  → сигнал лучше случайного момента на <b>{d1:+.3f}R</b>, "
                 f"случайного направления на <b>{d2:+.3f}R</b>")
        L.append("  <i>если обе разницы около нуля — преимущество даёт схема выходов, "
                 "а не отбор сигналов, и строить на нём челлендж нельзя</i>")

    L += ["", "<b>ВСЁ ВМЕСТЕ</b> — перебор всех четырёх, ищем максимум сделок при плюсе:"]
    best = None
    for s_ in SCORE_GRID:
        for v_ in RVOL_GRID:
            for q_ in SQ_GRID:
                for g_ in RNG_GRID:
                    rs = sel(s_, v_, q_, g_)
                    if len(rs) < 100:
                        continue
                    e = sum(rs) / len(rs)
                    se = statistics.pstdev(rs) / (len(rs) ** 0.5)
                    if e - 1.96 * se > 0 and (best is None or len(rs) > best[4]):
                        best = (s_, v_, q_, g_, len(rs), e)
    if best:
        s_, v_, q_, g_, n_, e_ = best
        now = sel()
        e_now = sum(now) / len(now) if now else 0
        L.append(f"  сейчас: сила ≥5, объём ≥1.0, сжатие ≤25, размах ≤9% — "
                 f"{len(now)} сд, {e_now:+.3f}R")
        L.append(f"  → <b>лучшее: сила ≥{s_}, объём ≥{v_}, сжатие ≤{q_}, размах ≤{g_}% — "
                 f"{n_} сделок, {e_:+.3f}R</b>")
        L.append(f"  <i>сделок {'больше' if n_ > len(now) else 'меньше'} в "
                 f"{max(n_, len(now)) / max(1, min(n_, len(now))):.1f}×, "
                 f"качество {e_ - e_now:+.3f}R</i>")
    else:
        L.append("  → ни одно сочетание не дало уверенного плюса на выборке ≥100 сделок")
    L.append("  <i>очко за сжатие пересчитывается под выбранный порог; "
             "прочие слагаемые силы от этих параметров не зависят</i>")

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
