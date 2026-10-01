"""
bt_long.py — перебор на ДЛИННОЙ истории, только часовой заряд.

Отличия от bt_sweep.py:
  • история качается КУСКАМИ (Gate отдаёт 2000 свечей за запрос, но принимает
    from/to) — можно взять 60-90 дней мелких свечей вместо 20;
  • сравнения 1h против 15m больше нет: прошлый прогон показал, что на 15м
    и ПРОБОЙ, и УКЛОН уверенно убыточны, вопрос закрыт;
  • добавлено то, чего не хватало:
      – ЧАСТОТА ПРОВЕРКИ ВХОДА УКЛОНА. Коридор остаётся часовым, но условие
        «до границы ≥1%» зависит от текущей цены. Сейчас оно проверяется один
        раз, в момент находки заряда. Если цена откатилась через 20 минут и дала
        лучший вход — бот этого не увидит до следующего часа. Перебираем, как
        часто перепроверять: 60 (как сейчас) / 30 / 15 / 5 минут;
      – ЦЕЛИ УКЛОНА: TP1 в процентах и отступ TP2 от границы коридора;
      – ПРОСКАЛЬЗЫВАНИЕ: живьём было 0.21-0.77%, а не 0.03%. При стопе 0.5%
        это съедает почти половину дистанции, поэтому гоним несколько значений —
        возможно, тесный стоп выигрывает только на бумаге.

Запуск: RUN_BACKTEST=long
Настройки: BL_DAYS (60), BL_TF (15m), BL_PAIRS (0=все), BL_FEE (0.05)
Время: ~15-25 мин на 60 днях и 103 парах. Если Render рвёт — ставь BL_PAIRS=50.
"""
import os
import time
import statistics

import bot as B

DAYS     = int(os.environ.get("BL_DAYS", "60"))
FINE_TF  = os.environ.get("BL_TF", "15m")
PAIRS_N  = int(os.environ.get("BL_PAIRS", "0"))
FEE_PCT  = float(os.environ.get("BL_FEE", "0.05"))
MIN_SCORE = int(os.environ.get("BL_MIN_SCORE", str(max(1, B.ACC_MIN_SCORE - 2))))
TILT_SCORE = int(os.environ.get("BL_TILT_SCORE", str(max(1, B.TILT_MIN_SCORE - 2))))
MAX_HOLD_H = int(os.environ.get("BL_MAX_HOLD_H", "12"))
TF_SEC = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}

VWAP_GRID   = [2.0, 2.5, 3.0, 3.5, None]
SLIP_GRID   = [0.03, 0.10, 0.25, 0.50]      # % — живьём было 0.21-0.77
TSTOP_GRID  = [0.5, 0.75, 1.0, 1.25]
TP1_GRID    = [0.5, 0.75, 1.0, 1.5]         # % от входа
TP2M_GRID   = [0.0, 0.2, 0.5]               # отступ TP2 от границы коридора, %
RECHECK_MIN = [60, 30, 15, 5]               # как часто перепроверять вход УКЛОНА
# ПРОБОЙ: живой бот торгует только в окна отправки и требует объём ≥2× на минутной
# свече. Прошлый прогон этого не учитывал — туда попали все пробои подряд, круглосуточно.
BRVOL_GRID  = [1.0, 1.3, 1.6, 2.0]          # объём свечи пробоя (на 15m, не равно порогу бота)
BSCORE_GRID = [0, 1, 2, 3]                  # сила пробоя (в бэктесте ниже живой на 2-3)
# Цели ПРОБОЯ: сейчас структурные (свинги и высота диапазона), стоп доходит до 3%.
# В журнале стопы были 2.3-2.7%, то есть цели далеко, а позиция мелкая. У УКЛОНА
# сработало обратное — близкая цель и высокий винрейт. Проверяем, переносится ли.
BTGT_GRID = [None, (1.0, 0.5), (1.0, 0.75), (1.5, 0.75), (1.5, 1.0)]   # None = как сейчас
# ── УДАЛЁННОСТЬ ТОЧКИ ВХОДА ОТ ГРАНИЦЫ КОРИДОРА ───────────────────────────────
# ПРОБОЙ: насколько ДАЛЬШЕ за уровень должна уйти цена, прежде чем входим.
#   Сейчас буфер max(0.3%, 0.15×ATR). Близко к уровню — ловим свипы; далеко —
#   входим поздно и платим за движение. Где оптимум, не угадать, меряем.
BENTRY_GRID = [0.1, 0.3, 0.5, 0.8, 1.2]
# Вся позиция до ОДНОЙ цели против деления пополам. Цель в % от входа, стоп 1%.
SINGLE_GRID = [0.75, 1.0, 1.5, 2.0, 3.0]
# ── ОБЪЕДИНЁННАЯ: вход по УКЛОНУ, ведём через пробой тремя целями ─────────────
# Логика: заходим внутри коридора по уклону заряда (там вход дешёвый), первой целью
# снимаем риск, второй забираем ход до границы, третьей едем уже за пробоем.
# Стоп подтягивается дважды: после TP1 в безубыток, после TP2 на уровень TP1.
# Так пробой торгуется НЕ отдельной сделкой (она убыточна), а как продолжение уклона.
COMBO_TP1 = 1.0                              # первая цель, % — лучшая по сетке УКЛОНА
COMBO_TP3 = [1.5, 2.0, 2.5, 3.0, 4.0]        # третья цель, % от входа — перебираем
COMBO_PARTS = (1/3, 1/3, 1/3)                # доли позиции на три цели


def _ts(c):
    return c.get("t", 0)


def _fetch_chunked(sym, tf, days):
    """История длиннее 2000 свечей — несколько запросов с from/to."""
    sec = TF_SEC[tf]
    now = int(time.time())
    start = now - days * 86400
    out, cur = [], start
    while cur < now:
        to = min(now, cur + 1900 * sec)
        raw = B.api_get("candlesticks", {"contract": f"{sym}_USDT", "interval": tf,
                                         "from": cur, "to": to})
        part = B.parse_candles(raw) if raw else []
        if not part:
            break
        out.extend(part)
        nxt = _ts(part[-1]) + sec
        if nxt <= cur:
            break
        cur = nxt
    seen, uniq = set(), []
    for c in sorted(out, key=_ts):
        if _ts(c) not in seen:
            seen.add(_ts(c))
            uniq.append(c)
    return uniq


def _sim(bars, side, entry, stop, tp1, tp2, split=True):
    """split=True: половина на TP1, стоп в безубыток, остаток на TP2 (как в боте).
    split=False: ВСЯ позиция до одной цели tp1, без безубытка.
    Зачем проверять: при делении пополам самый частый исход — «взяли TP1, остаток
    вышел в ноль» — даёт лишь ПОЛОВИНУ от TP1, а проигрыш всегда полный −1R.
    При TP1 = 0.5R для безубыточности нужен винрейт 80%, при 1R — 67%.
    Без деления победа полная, но нет подстраховки безубытком."""
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    is_long = side == "long"
    if not split:
        for c in bars:
            hs = (c["l"] <= stop) if is_long else (c["h"] >= stop)
            ht = (c["h"] >= tp1) if is_long else (c["l"] <= tp1)
            if hs:
                return -1.0 - FEE_PCT / 100 * entry / risk
            if ht:
                return abs(tp1 - entry) / risk - FEE_PCT / 100 * entry / risk
        last = bars[-1]["c"] if bars else entry
        r = (last - entry) / risk if is_long else (entry - last) / risk
        return r - FEE_PCT / 100 * entry / risk
    half, cur_stop, acc = False, stop, 0.0
    for c in bars:
        hit_s = (c["l"] <= cur_stop) if is_long else (c["h"] >= cur_stop)
        tgt = tp1 if not half else tp2
        hit_t = (c["h"] >= tgt) if is_long else (c["l"] <= tgt)
        if hit_s:
            part = 0.5 if half else 1.0
            r = (cur_stop - entry) / risk if is_long else (entry - cur_stop) / risk
            return acc + r * part - FEE_PCT / 100 * entry / risk
        if hit_t:
            if not half:
                acc += 0.5 * (abs(tp1 - entry) / risk)
                half, cur_stop = True, entry
            else:
                acc += 0.5 * (abs(tp2 - entry) / risk)
                return acc - FEE_PCT / 100 * entry / risk
    last = bars[-1]["c"] if bars else entry
    part = 0.5 if half else 1.0
    r = (last - entry) / risk if is_long else (entry - last) / risk
    return acc + r * part - FEE_PCT / 100 * entry / risk


def _sim3(bars, side, entry, stop, t1, t2, t3, parts=COMBO_PARTS, trail=True):
    """Три цели, стоп подтягивается: после TP1 в безубыток, после TP2 на цену TP1.
    trail=False — стоп не двигаем вовсе (для сравнения, что даёт подтяжка)."""
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    is_long = side == "long"
    tgts = [t1, t2, t3]
    # цели должны идти по возрастанию в сторону сделки, иначе сдвигаем
    for i in range(1, 3):
        if is_long and tgts[i] <= tgts[i - 1]:
            tgts[i] = tgts[i - 1] * 1.001
        if not is_long and tgts[i] >= tgts[i - 1]:
            tgts[i] = tgts[i - 1] * 0.999
    done = 0
    cur_stop = stop
    acc = 0.0
    for c in bars:
        hit_s = (c["l"] <= cur_stop) if is_long else (c["h"] >= cur_stop)
        if hit_s:
            left = sum(parts[done:])
            r = (cur_stop - entry) / risk if is_long else (entry - cur_stop) / risk
            return acc + r * left - FEE_PCT / 100 * entry / risk
        while done < 3:
            t = tgts[done]
            if (c["h"] >= t) if is_long else (c["l"] <= t):
                acc += parts[done] * (abs(t - entry) / risk)
                done += 1
                if trail:
                    if done == 1:
                        cur_stop = entry                 # безубыток
                    elif done == 2:
                        cur_stop = tgts[0]               # фиксируем прибыль первой цели
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


def _in_window(ts):
    """Попадает ли время в окна отправки бота (МСК)."""
    import datetime as _dt
    t = _dt.datetime.fromtimestamp(ts, _dt.timezone.utc) + _dt.timedelta(hours=3)
    nm = t.hour * 60 + t.minute
    return any(h1 * 60 + m1 <= nm < h2 * 60 + m2 for h1, m1, h2, m2 in B.SIGNAL_WINDOWS)


def _vwap_atr(fine, k, bars_day):
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


def run():
    pairs = B.UPSCALE_PAIRS[:PAIRS_N] if PAIRS_N else B.UPSCALE_PAIRS
    step = TF_SEC[FINE_TF]
    hold = max(6, int(MAX_HOLD_H * 3600 / step))
    watch = max(6, int(B.WATCH_TTL_HOURS * 3600 / step))
    bars_day = int(24 * 3600 / step)

    res = {
        "vwap": {v: [] for v in VWAP_GRID},
        "stop": {(s, sl): [] for s in TSTOP_GRID for sl in SLIP_GRID},
        "tp1": {v: [] for v in TP1_GRID},
        "tp2m": {v: [] for v in TP2M_GRID},
        "recheck": {v: [] for v in RECHECK_MIN},
        "bwin": {True: [], False: []},
        "brvol": {v: [] for v in BRVOL_GRID},
        "bscore": {v: [] for v in BSCORE_GRID},
        "btgt": {v: [] for v in BTGT_GRID},
        "bentry": {v: [] for v in BENTRY_GRID},
        "t_single": {v: [] for v in SINGLE_GRID}, "t_split": [],
        "b_single": {v: [] for v in SINGLE_GRID}, "b_split": [],
        "combo": {v: [] for v in COMBO_TP3},
        "combo_nobe": {v: [] for v in COMBO_TP3},
    }
    n_ch = n_tilt = n_brk = 0
    saved = B.ACC_MIN_SCORE
    B.ACC_MIN_SCORE = MIN_SCORE
    t0 = time.time()
    cov = 0.0

    for i, sym in enumerate(pairs, 1):
        try:
            fine = _fetch_chunked(sym, FINE_TF, DAYS)
            base = _fetch_chunked(sym, "1h", DAYS + 5)
        except Exception:
            continue
        if len(fine) < 500 or len(base) < 150:
            continue
        fine, base = fine[:-1], base[:-1]
        if not cov:
            cov = (_ts(fine[-1]) - _ts(fine[0])) / 86400
        idx = {_ts(c): k for k, c in enumerate(fine)}
        fine_from = _ts(fine[0])
        vol_fine = B.trimmed_mean([c["v"] for c in fine[:max(50, len(fine) // 4)]])
        if not vol_fine or vol_fine <= 0:
            continue
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
            cts = _ts(upto[-1])
            if cts < fine_from or cts <= last_ts:
                continue
            c = B.detect_charge(sym, upto, upto[-1]["c"], vb, ab,
                                {"btc_chg_win": 0.0, "do_charge": True},
                                {"funding": 0.0, "change_24h": 0.0}, lambda s: None, P=B.ALT_P)
            if not c:
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

            # ── УКЛОН ──
            if c["side"] in ("long", "short") and c["score"] >= TILT_SCORE:
                is_l = c["side"] == "long"
                bnd = c["hi"] if is_l else c["lo"]
                base_slip = 0.25        # реалистичное, для сеток кроме «проскальзывание»

                def _enter(kk, slip, stop_pct, tp1_pct, tp2_m):
                    px = fine[kk]["c"]
                    d = (bnd - px) / px * 100 if is_l else (px - bnd) / px * 100
                    if d < B.TILT_MIN_DIST_PCT:
                        return None
                    # ВАЖНО: стоп и TP1 бот считает от цены СИГНАЛА, а входит по
                    # фактической (хуже на величину проскальзывания). Если считать
                    # их от факта, бэктест льстит тесным стопам: при стопе 0.5% и
                    # проскальзывании 0.25% реальная дистанция до стопа 0.75%, то есть
                    # риск в полтора раза больше задуманного (живьём на SAND так и вышло:
                    # план $15, факт $21.6). Поэтому уровни — от px, вход — по ent.
                    ent = px * (1 + slip / 100) if is_l else px * (1 - slip / 100)
                    stp = px * (1 - stop_pct / 100) if is_l else px * (1 + stop_pct / 100)
                    t1 = px * (1 + tp1_pct / 100) if is_l else px * (1 - tp1_pct / 100)
                    m = px * tp2_m / 100
                    t2 = (bnd - m) if is_l else (bnd + m)
                    if is_l and t2 <= t1:
                        t2 = t1 * 1.001
                    if not is_l and t2 >= t1:
                        t2 = t1 * 0.999
                    fut = fine[kk + 1:kk + 1 + hold]
                    if len(fut) < 4:
                        return None
                    return _sim(fut, c["side"], ent, stp, t1, t2)

                # вся позиция до одной цели против деления пополам
                px0 = fine[k0]["c"]
                d0 = (bnd - px0) / px0 * 100 if is_l else (px0 - bnd) / px0 * 100
                if d0 >= B.TILT_MIN_DIST_PCT:
                    e0 = px0 * (1 + base_slip / 100) if is_l else px0 * (1 - base_slip / 100)
                    s0 = px0 * (1 - 1.0 / 100) if is_l else px0 * (1 + 1.0 / 100)
                    f0 = fine[k0 + 1:k0 + 1 + hold]
                    if len(f0) >= 4:
                        rsp = _sim(f0, c["side"], e0, s0,
                                   px0 * (1 + 1.0 / 100) if is_l else px0 * (1 - 1.0 / 100),
                                   bnd, split=True)
                        if rsp is not None:
                            res["t_split"].append(rsp)
                        for g in SINGLE_GRID:
                            tg = px0 * (1 + g / 100) if is_l else px0 * (1 - g / 100)
                            r1 = _sim(f0, c["side"], e0, s0, tg, tg, split=False)
                            if r1 is not None:
                                res["t_single"][g].append(r1)

                # объединённая: уклон + ведение через пробой
                if d0 >= B.TILT_MIN_DIST_PCT and len(f0) >= 4:
                    c1 = px0 * (1 + COMBO_TP1 / 100) if is_l else px0 * (1 - COMBO_TP1 / 100)
                    c2 = bnd                                     # граница коридора
                    for g3 in COMBO_TP3:
                        c3 = px0 * (1 + g3 / 100) if is_l else px0 * (1 - g3 / 100)
                        r3 = _sim3(f0, c["side"], e0, s0, c1, c2, c3, trail=True)
                        if r3 is not None:
                            res["combo"][g3].append(r3)
                        r4 = _sim3(f0, c["side"], e0, s0, c1, c2, c3, trail=False)
                        if r4 is not None:
                            res["combo_nobe"][g3].append(r4)

                fired = _enter(k0, base_slip, 1.0, B.TILT_TP1_PCT, B.TILT_TP2_MARGIN)
                if fired is not None:
                    n_tilt += 1
                # стоп × проскальзывание
                for sp in TSTOP_GRID:
                    for slp in SLIP_GRID:
                        r = _enter(k0, slp, sp, B.TILT_TP1_PCT, B.TILT_TP2_MARGIN)
                        if r is not None:
                            res["stop"][(sp, slp)].append(r)
                # цели
                for t1p in TP1_GRID:
                    r = _enter(k0, base_slip, 1.0, t1p, B.TILT_TP2_MARGIN)
                    if r is not None:
                        res["tp1"][t1p].append(r)
                for t2m in TP2M_GRID:
                    r = _enter(k0, base_slip, 1.0, B.TILT_TP1_PCT, t2m)
                    if r is not None:
                        res["tp2m"][t2m].append(r)
                # частота перепроверки входа: ищем первый момент, когда до границы ≥1%
                for rc in RECHECK_MIN:
                    stepn = max(1, int(rc * 60 / step))
                    limit = int(B.WATCH_TTL_HOURS * 3600 / step)
                    for kk in range(k0, min(k0 + limit, len(fine) - hold - 1), stepn):
                        r = _enter(kk, base_slip, 1.0, B.TILT_TP1_PCT, B.TILT_TP2_MARGIN)
                        if r is not None:
                            res["recheck"][rc].append(r)
                            break

            # ── ПРОБОЙ: удалённость точки входа за уровень ──
            for bd in BENTRY_GRID:
                for sd, lvl_ in (("long", c["hi"]), ("short", c["lo"])):
                    trg = lvl_ * (1 + bd / 100) if sd == "long" else lvl_ * (1 - bd / 100)
                    for k in range(k0 + 1, min(k0 + watch, len(fine) - hold - 2)):
                        bar2 = fine[k]
                        if not (bar2["c"] > trg if sd == "long" else bar2["c"] < trg):
                            continue
                        ft = fine[k + 1:k + 1 + hold]
                        if len(ft) < 4:
                            break
                        sg = bar2["c"]
                        en = sg * (1 + 0.25 / 100) if sd == "long" else sg * (1 - 0.25 / 100)
                        st = sg * (1 - 1.0 / 100) if sd == "long" else sg * (1 + 1.0 / 100)
                        a1 = sg * (1 + 1.0 / 100) if sd == "long" else sg * (1 - 1.0 / 100)
                        hgt = c["hi"] - c["lo"]
                        a2 = (c["hi"] + hgt * 0.5) if sd == "long" else (c["lo"] - hgt * 0.5)
                        r = _sim(ft, sd, en, st, a1, a2)
                        if r is not None:
                            res["bentry"][bd].append(r)
                        break

            # ── ПРОБОЙ: только сетка VWAP ──
            hi_t = B.order_trigger(c["hi"], True, c["atr"])
            lo_t = B.order_trigger(c["lo"], False, c["atr"])
            done = False
            for k in range(k0 + 1, min(k0 + watch, len(fine) - 2)):
                bar = fine[k]
                for side, trig in (("long", hi_t), ("short", lo_t)):
                    if not (bar["c"] > trig if side == "long" else bar["c"] < trig):
                        continue
                    fut = fine[k + 1:k + 1 + hold]
                    if len(fut) < 4:
                        continue
                    w = dict(c)
                    for kk_, vv_ in (("created", 0), ("funding", 0.0), ("change_24h", 0.0),
                                     ("oi_win", None), ("oi_15m", None), ("P", B.ALT_P)):
                        w.setdefault(kk_, vv_)
                    sig_px = bar["c"]                      # цена сигнала
                    ent = sig_px * (1 + 0.25 / 100) if side == "long" else sig_px * (1 - 0.25 / 100)
                    bo = B.build_breakout(dict(w), side, sig_px, 0.0, None)   # уровни от сигнала
                    r = _sim(fut, side, ent, bo["stop"], bo["tp1_price"], bo["tp2_price"])
                    if r is None:
                        continue
                    va = _vwap_atr(fine, k, bars_day)
                    for thr in VWAP_GRID:
                        if thr is None or (va is not None and va <= thr):
                            res["vwap"][thr].append(r)
                    # окно отправки по МСК — живой бот вне его не торгует
                    inw = _in_window(_ts(bar))
                    vw_ok = va is None or va <= B.VWAP_MAX_ATR
                    res["bwin"][inw].append(r)
                    rv_b = bar["v"] / vol_fine
                    bo["pace"] = rv_b
                    bo.setdefault("delta", None)
                    bo.setdefault("ext_atr", 0.0)
                    try:
                        bsc = B.analyze_breakout(bo)[0]
                    except Exception:
                        bsc = None
                    if inw and vw_ok:
                        st_b = sig_px * (1 - 1.0 / 100) if side == "long" else sig_px * (1 + 1.0 / 100)
                        t1_b = sig_px * (1 + 1.0 / 100) if side == "long" else sig_px * (1 - 1.0 / 100)
                        hg = c["hi"] - c["lo"]
                        t2_b = (c["hi"] + hg * 0.5) if side == "long" else (c["lo"] - hg * 0.5)
                        rsp = _sim(fut, side, ent, st_b, t1_b, t2_b, split=True)
                        if rsp is not None:
                            res["b_split"].append(rsp)
                        for g in SINGLE_GRID:
                            tg = sig_px * (1 + g / 100) if side == "long" else sig_px * (1 - g / 100)
                            r1 = _sim(fut, side, ent, st_b, tg, tg, split=False)
                            if r1 is not None:
                                res["b_single"][g].append(r1)
                    if inw and vw_ok:
                        # цели пробоя: структурные против фиксированных, как у УКЛОНА
                        for tg in BTGT_GRID:
                            if tg is None:
                                res["btgt"][tg].append(r)
                                continue
                            spc, t1pc = tg
                            if side == "long":
                                st2 = sig_px * (1 - spc / 100)
                                a1 = sig_px * (1 + t1pc / 100)
                                a2 = c["hi"] + (c["hi"] - c["lo"]) * 0.5
                            else:
                                st2 = sig_px * (1 + spc / 100)
                                a1 = sig_px * (1 - t1pc / 100)
                                a2 = c["lo"] - (c["hi"] - c["lo"]) * 0.5
                            r2 = _sim(fut, side, ent, st2, a1, a2)
                            if r2 is not None:
                                res["btgt"][tg].append(r2)
                    if inw and vw_ok:                       # база: окно + VWAP как в боте
                        for rt in BRVOL_GRID:
                            if rv_b >= rt:
                                res["brvol"][rt].append(r)
                        if bsc is not None:
                            for bt_ in BSCORE_GRID:
                                if bsc >= bt_:
                                    res["bscore"][bt_].append(r)
                    n_brk += 1
                    last_ts = _ts(fut[-1])
                    done = True
                    break
                if done:
                    break
        if i % 10 == 0:
            print(f"[LONG] {i}/{len(pairs)} | зарядов {n_ch} | уклон {n_tilt} пробой {n_brk} "
                  f"| {time.time()-t0:.0f}с")

    B.ACC_MIN_SCORE = saved
    took = time.time() - t0

    L = [f"📚 <b>Длинный бэктест</b> (заряд 1h, сделки по {FINE_TF}, ~{cov:.0f} дн, {len(pairs)} пар)",
         f"Комиссия {FEE_PCT}%, время {took/60:.1f} мин",
         f"Зарядов {n_ch} | сделок: УКЛОН {n_tilt}, ПРОБОЙ {n_brk}",
         "<i>✅ плюс уверенно · ❌ минус уверенно · пусто — неотличимо от нуля</i>",
         "", "<b>Порог VWAP (ПРОБОЙ):</b>"]
    for v in VWAP_GRID:
        L.append(_line(res["vwap"][v], f"VWAP {'выкл' if v is None else f'≤{v}'}"))

    L += ["", "<b>ПРОБОЙ: торговать только в окна отправки?</b>",
          f"  <i>окна бота: {B.windows_txt()} МСК</i>"]
    L.append(_line(res["bwin"][True], "в окне"))
    L.append(_line(res["bwin"][False], "вне окна (бот туда не ходит)"))
    L += ["", f"<b>ПРОБОЙ: фильтр объёма</b> (в окне + VWAP ≤{B.VWAP_MAX_ATR})"]
    for v in BRVOL_GRID:
        L.append(_line(res["brvol"][v], f"объём ≥{v}×" + (" (выкл)" if v <= 1.0 else "")))
    L += ["", f"<b>ПРОБОЙ: порог силы</b> (в окне + VWAP ≤{B.VWAP_MAX_ATR})",
          "  <i>в бэктесте сила ниже живой на 2-3 — живой порог ставить выше</i>"]
    for v in BSCORE_GRID:
        L.append(_line(res["bscore"][v], f"сила ≥{v}" + (" (как сейчас — фильтра нет)" if v == 0 else "")))

    L += ["", "<b>ПРОБОЙ: цели структурные или фиксированные, как у УКЛОНА?</b>",
          "  <i>в журнале структурные стопы были 2.3-2.7% — цели далеко, позиция мелкая</i>"]
    for v in BTGT_GRID:
        lbl = "структурные (как сейчас)" if v is None else f"стоп {v[0]}% / TP1 {v[1]}%"
        L.append(_line(res["btgt"][v], lbl))

    L += ["", "<b>ПРОБОЙ: как далеко за уровень входить?</b>",
          "  <i>стоп 1%, TP1 1%; сейчас в боте буфер max(0.3%, 0.15×ATR)</i>"]
    for v in BENTRY_GRID:
        L.append(_line(res["bentry"][v], f"вход за {v}% от уровня"))

    L += ["", "═══ <b>ОБЪЕДИНЁННАЯ: УКЛОН + ПРОБОЙ ОДНОЙ СДЕЛКОЙ</b> ═══",
          f"  <i>вход по уклону внутри коридора, стоп 1%. Три цели по трети позиции:",
          f"   TP1 {COMBO_TP1}% → стоп в безубыток; TP2 на границе коридора → стоп на TP1;",
          f"   TP3 за пробоем. Пробой не отдельная сделка, а продолжение уклона</i>"]
    for v in COMBO_TP3:
        L.append(_line(res["combo"][v], f"TP3 {v}% (стоп подтягиваем)"))
    L.append("  <i>то же, но стоп НЕ двигаем — видно, что даёт подтяжка:</i>")
    for v in COMBO_TP3:
        L.append(_line(res["combo_nobe"][v], f"TP3 {v}% (стоп на месте)"))
    base_t = res.get("t_split") or []
    if base_t:
        bt_e = sum(base_t) / len(base_t)
        best_c = max(res["combo"].items(), key=lambda kv: (sum(kv[1]) / len(kv[1])) if kv[1] else -9)
        if best_c[1]:
            be = sum(best_c[1]) / len(best_c[1])
            L.append(f"  → лучшая объединённая: TP3 {best_c[0]}% = {be:+.3f}R против "
                     f"{bt_e:+.3f}R у нынешнего УКЛОНА ({be - bt_e:+.3f}R)")

    L += ["", "═══ <b>ДЕЛИТЬ ПОЗИЦИЮ ПОПОЛАМ ИЛИ ВЕСТИ ЦЕЛИКОМ?</b> ═══",
          "  <i>при делении самый частый исход «TP1 + безубыток» даёт лишь ПОЛОВИНУ цели,",
          "   а проигрыш всегда полный −1R. Целиком: победа полная, но без подстраховки</i>",
          "", "<b>УКЛОН</b> (стоп 1%)"]
    L.append(_line(res["t_split"], "пополам: TP1 1% + TP2 на границе (как сейчас)"))
    for g in SINGLE_GRID:
        L.append(_line(res["t_single"][g], f"целиком до {g}%"))
    L += ["", "<b>ПРОБОЙ</b> (стоп 1%, в окне + VWAP)"]
    L.append(_line(res["b_split"], "пополам: TP1 1% + TP2 за коридором"))
    for g in SINGLE_GRID:
        L.append(_line(res["b_single"][g], f"целиком до {g}%"))

    L += ["", "<b>УКЛОН: стоп × проскальзывание</b> (живьём было 0.21-0.77%)"]
    for sp in TSTOP_GRID:
        row = []
        for slp in SLIP_GRID:
            a = res["stop"][(sp, slp)]
            row.append(f"{(sum(a)/len(a) if a else 0):+.2f}")
        L.append(f"  стоп {sp:>5}% | " + " | ".join(f"{s} при {sl}%" for s, sl in zip(row, SLIP_GRID)))
    best = max(res["stop"].items(), key=lambda kv: (sum(kv[1]) / len(kv[1])) if kv[1] else -9)
    L.append(f"  → лучшее сочетание: стоп {best[0][0]}% при проскальзывании {best[0][1]}%")
    L.append("  <i>смотри колонку 0.25% — она ближе всего к реальности. Стоп и цели "
             "считаются от цены СИГНАЛА, вход по факту — как в боте, поэтому "
             "проскальзывание реально увеличивает дистанцию до стопа</i>")

    L += ["", "<b>УКЛОН: TP1</b> (стоп 1%, проскальзывание 0.25%)"]
    for v in TP1_GRID:
        L.append(_line(res["tp1"][v], f"TP1 {v}%"))
    L += ["", "<b>УКЛОН: отступ TP2 от границы коридора</b>"]
    for v in TP2M_GRID:
        L.append(_line(res["tp2m"][v], f"TP2 за {v}% до границы" + (" (точно на границе)" if v == 0 else "")))

    L += ["", "<b>УКЛОН: как часто перепроверять вход</b>",
          "  <i>коридор остаётся часовым, меняется только частота проверки «до границы ≥1%»</i>"]
    for v in RECHECK_MIN:
        L.append(_line(res["recheck"][v], f"каждые {v} мин" + (" (как сейчас)" if v == 60 else "")))

    L += ["", "<i>OI за историю не восстановить — порог силы снижен на 2. "
          "В спорной свече считаем стоп. Подглядывания в будущее нет.</i>"]

    msg = "\n".join(L)
    print(msg.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    try:
        B.send_blocks(msg.split("\n"))
    except Exception as e:
        print(f"[LONG] отправка не удалась: {e}")


def main():
    try:
        run()
    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            B.send_telegram(f"⚠️ Длинный бэктест упал: {e}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
