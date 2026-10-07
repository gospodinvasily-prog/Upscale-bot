"""
bt_signals.py — три новых бэктеста дневной торговли.

Запуск: RUN_BACKTEST=signals   BT_MODE=liq|xmom|vol|all
Env общие: BT_DAYS=1825  BT_OFFSET=0

══════════════════════════════════════════════════════════════════
1. LIQUIDATION CASCADE (BT_MODE=liq)
   Гипотеза: после дня с экстремальным объёмом + сильным движением (каскад
   ликвидаций) рынок перегружен и на следующий день откатывается.
   Сигнал: |ret(D)| > VOL_K × σ_30d И volume(D) > VVOL_K × avg_vol_30d
   Направление: против хода D (fade)
   Вход: open(D+1), выход: close(D+1); ШОРТ если D был сильно вверх, ЛОНГ если вниз
   Планка: excess ≥ +0.15% значимо (2.64σ), обе половины, fade > momentum

2. CROSS-ASSET MOMENTUM (BT_MODE=xmom)
   Гипотеза: когда BTC растёт несколько дней подряд, альты следуют с лагом.
   Сигнал: сумма дневных ретёрнов BTC за XMOM_N дней > XMOM_THR% → ЛОНГ альтов
           (и зеркально: sum < -XMOM_THR% → ШОРТ альтов)
   Вход: open(D+1), выход: close(D+1) для не-BTC пар
   Планка: та же, excess над рынком (включая BTC)

3. VOLUME ANOMALY (BT_MODE=vol)
   Гипотеза: день с аномально высоким объёмом при слабом движении = аккумуляция/
   распределение → следующий день покажет реальное направление.
   Сигнал: volume(D) > VANOM_K × avg_vol_30d И |ret(D)| < VANOM_MOVE% (тихий объём)
   Направление: по направлению дня D (momentum в продолжение «тихого» объёма)
   Планка: та же

Все три: кластеризация CI по дням, поправка на скрининг 2.64σ,
         momentum-контроль, разбивка на половины, годовой срез.
══════════════════════════════════════════════════════════════════
"""
import os, time, statistics, traceback
from datetime import datetime, timezone, timedelta

import bot as B

# ── общие параметры ──────────────────────────────────────────────
BT_DAYS   = int(os.environ.get("BT_DAYS",   "1825"))
BT_OFFSET = int(os.environ.get("BT_OFFSET", "0"))
BT_MODE   = os.environ.get("BT_MODE", "all").lower()   # liq|xmom|vol|all

COST_TK = (0.05 + 0.03) * 2   # 0.16% круг, ликвидные
FUND_D  = 0.03                 # %/день, лонг платит
Z       = 2.64
MSK     = timezone(timedelta(hours=3))
DAY     = 86400
LIQ10   = ["BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "DOGE", "LINK", "LTC", "AVAX"]

# ── параметры стратегий ──────────────────────────────────────────
# LIQUIDATION CASCADE
LIQ_RET_K  = float(os.environ.get("LIQ_RET_K",  "2.0"))   # σ движения
LIQ_VOL_K  = float(os.environ.get("LIQ_VOL_K",  "2.0"))   # × средний объём
LIQ_WIN    = int(os.environ.get("LIQ_WIN",      "30"))     # окно σ и avg

# CROSS-ASSET MOMENTUM
XMOM_N    = int(os.environ.get("XMOM_N",    "3"))     # дней BTC-momentum
XMOM_THR  = float(os.environ.get("XMOM_THR", "3.0"))  # % суммарный порог

# VOLUME ANOMALY
VANOM_K    = float(os.environ.get("VANOM_K",    "2.5"))   # × avg vol
VANOM_MOVE = float(os.environ.get("VANOM_MOVE", "1.0"))   # макс |ret| %
VANOM_WIN  = int(os.environ.get("VANOM_WIN",   "30"))     # окно avg vol


# ════════════════════════════════════════════════════════════════
# УТИЛИТЫ
# ════════════════════════════════════════════════════════════════

def _fetch_daily(sym):
    """Возвращает список [{t,o,h,l,c,v}] за BT_DAYS+45 дней до сейчас−OFFSET."""
    now = int(time.time()) - BT_OFFSET * DAY
    out, cur = [], now - (BT_DAYS + 45) * DAY
    while cur < now:
        raw = B.api_get("candlesticks", {
            "contract": f"{sym}_USDT",
            "interval": "1d",
            "from": cur,
            "to": min(now, cur + 1000 * DAY),
        })
        part = B.parse_candles(raw) if raw else []
        if not part:
            break
        out.extend(part)
        nxt = part[-1].get("t", 0) + DAY
        if nxt <= cur:
            break
        cur = nxt
    seen, u = set(), []
    for c in sorted(out, key=lambda x: x.get("t", 0)):
        if c.get("t", 0) not in seen:
            seen.add(c["t"])
            u.append(c)
    return u[:-1]   # последняя свеча незакрытая


def _day_ci(pairs_list):
    """[(date_str, value)] → (n, n_days, mean, Z*se). Кластеризация по дате."""
    by_d = {}
    for dt, v in pairs_list:
        by_d.setdefault(dt, []).append(v)
    dm = [sum(v) / len(v) for v in by_d.values()]
    if not dm:
        return None
    m = sum(dm) / len(dm)
    se = statistics.pstdev(dm) / len(dm) ** 0.5 if len(dm) > 1 else 0.0
    return sum(len(v) for v in by_d.values()), len(dm), m, Z * se


def _split_halves(pairs_list):
    """Разбить по медиане дат → (first_half_ci, second_half_ci)."""
    dates = sorted(set(dt for dt, _ in pairs_list))
    if len(dates) < 4:
        return None, None
    mid = dates[len(dates) // 2]
    h1 = [(dt, v) for dt, v in pairs_list if dt < mid]
    h2 = [(dt, v) for dt, v in pairs_list if dt >= mid]
    return _day_ci(h1), _day_ci(h2)


def _year_breakdown(pairs_list):
    """→ {year_str: (n, mean)}"""
    by_y = {}
    for dt, v in pairs_list:
        y = dt[:4]
        by_y.setdefault(y, []).append(v)
    return {y: (len(v), sum(v) / len(v)) for y, v in by_y.items()}


def _fmt_ci(st):
    if not st:
        return "нет данных"
    n, nd, m, ci = st
    return f"NET {m:+.3f}% (±{ci:.3f}), {n} сд / {nd} дн"


def _day_mean_market(nd_all_by_day):
    """{day_int: {sym: nd}} → {day_int: float} — среднее по всем парам за день."""
    out = {}
    for d, smap in nd_all_by_day.items():
        if len(smap) >= 3:
            out[d] = sum(smap.values()) / len(smap)
    return out


# ════════════════════════════════════════════════════════════════
# 1. LIQUIDATION CASCADE
# ════════════════════════════════════════════════════════════════

def run_liq_cascade():
    print("[LIQ] загрузка данных…")
    data = {sym: _fetch_daily(sym) for sym in LIQ10}

    signal_nd = []     # (date_str, nd%)  для сигнальных дней, направление применено
    signal_raw = []    # то же, без издержек (momentum)
    nd_all_by_day = {} # {day_int: {sym: nd}} для dm_market (ВСЕ пары)

    for sym, candles in data.items():
        if len(candles) < LIQ_WIN + 5:
            continue
        for i in range(LIQ_WIN, len(candles) - 1):
            D  = candles[i]
            X  = candles[i + 1]
            o, cl_x = X.get("o", 0), X.get("c", 0)
            if o <= 0 or cl_x <= 0:
                continue

            # nd рынка (следующий день)
            nd = (cl_x / o - 1) * 100
            day_int = X["t"] // DAY
            dt_str  = datetime.fromtimestamp(X["t"], MSK).strftime("%Y-%m-%d")

            # всегда регистрируем для dm_market
            nd_all_by_day.setdefault(day_int, {})[sym] = nd

            # параметры дня D (для вычисления σ и avg_vol)
            window = candles[i - LIQ_WIN: i]
            rets   = [(c["c"] / c["o"] - 1) * 100 for c in window
                      if c.get("o", 0) > 0 and c.get("c", 0) > 0]
            vols   = [c.get("v", 0) for c in window]
            if len(rets) < LIQ_WIN // 2 or not vols:
                continue

            sigma_r   = statistics.pstdev(rets)
            avg_vol   = sum(vols) / len(vols)
            ret_D     = (D.get("c", 0) / D.get("o", 1) - 1) * 100 if D.get("o", 0) > 0 else 0.0
            vol_D     = D.get("v", 0)

            if sigma_r <= 0 or avg_vol <= 0:
                continue

            # Сигнал: экстремальный ход + экстремальный объём
            if abs(ret_D) < LIQ_RET_K * sigma_r:
                continue
            if vol_D < LIQ_VOL_K * avg_vol:
                continue

            # Fade: против направления дня D
            direction = -1 if ret_D > 0 else +1
            nd_dir    = nd * direction
            net       = nd_dir - COST_TK - FUND_D
            raw       = nd_dir  # без издержек (momentum-контроль)

            signal_nd.append((dt_str, net))
            signal_raw.append((dt_str, raw))

    dm_market = _day_mean_market(nd_all_by_day)

    # excess = nd − avg_market того же дня
    excess_nd = []
    for dt, v in signal_nd:
        day_int = int(datetime.strptime(dt, "%Y-%m-%d")
                      .replace(tzinfo=MSK).timestamp()) // DAY
        if day_int in dm_market:
            excess_nd.append((dt, v - dm_market[day_int]))

    st_net    = _day_ci(signal_nd)
    st_excess = _day_ci(excess_nd)
    st_raw    = _day_ci(signal_raw)
    h1_net, h2_net = _split_halves(signal_nd)
    yb = _year_breakdown(signal_nd)

    L = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🌊 <b>LIQUIDATION CASCADE</b>",
        f"   параметры: ret >{LIQ_RET_K:.1f}σ, vol >{LIQ_VOL_K:.1f}× avg | окно {LIQ_WIN} дн",
        f"   NET:     {_fmt_ci(st_net)}",
        f"   EXCESS:  {_fmt_ci(st_excess)}",
        f"   raw(мом):{_fmt_ci(st_raw)}",
        f"   ½/½:     {_fmt_ci(h1_net)} | {_fmt_ci(h2_net)}",
    ]
    if yb:
        y_str = " | ".join(f"{y}: {m:+.2f}%({n})" for y, (n, m) in sorted(yb.items()))
        L.append(f"   годы:   {y_str}")
    # критерий
    ok = (st_excess and st_excess[2] - st_excess[3] > 0.15
          and h1_net and h2_net
          and h1_net[2] > 0 and h2_net[2] > 0
          and st_excess[2] > (st_raw[2] if st_raw else 0))
    L.append(f"   → {'✅ прошла планку' if ok else '❌ не прошла планку'}")
    return "\n".join(L)


# ════════════════════════════════════════════════════════════════
# 2. CROSS-ASSET MOMENTUM
# ════════════════════════════════════════════════════════════════

def run_xmom():
    print("[XMOM] загрузка данных…")
    data = {sym: _fetch_daily(sym) for sym in LIQ10}
    btc  = data.get("BTC", [])
    if len(btc) < XMOM_N + 5:
        print("[XMOM] нет данных BTC")
        return "❌ XMOM: нет BTC-истории"

    # BTC rolling sum за XMOM_N дней → сигнал для следующего дня
    btc_sig = {}  # day_int_X → direction (+1/-1/0)
    for i in range(XMOM_N, len(btc) - 1):
        window = btc[i - XMOM_N: i]
        s = sum((c["c"] / c["o"] - 1) * 100 for c in window
                if c.get("o", 0) > 0 and c.get("c", 0) > 0)
        X_day = btc[i + 1]["t"] // DAY
        if s > XMOM_THR:
            btc_sig[X_day] = +1
        elif s < -XMOM_THR:
            btc_sig[X_day] = -1

    signal_nd  = []
    signal_raw = []
    nd_all_by_day = {}

    for sym, candles in data.items():
        if sym == "BTC":
            continue
        if len(candles) < XMOM_N + 5:
            continue
        for i in range(1, len(candles) - 1):
            X  = candles[i + 1] if i + 1 < len(candles) else None
            if X is None:
                continue
            o, cl_x = X.get("o", 0), X.get("c", 0)
            if o <= 0 or cl_x <= 0:
                continue

            nd      = (cl_x / o - 1) * 100
            day_int = X["t"] // DAY
            dt_str  = datetime.fromtimestamp(X["t"], MSK).strftime("%Y-%m-%d")

            nd_all_by_day.setdefault(day_int, {})[sym] = nd

            direction = btc_sig.get(day_int, 0)
            if direction == 0:
                continue

            nd_dir = nd * direction
            net    = nd_dir - COST_TK - FUND_D
            raw    = nd_dir

            signal_nd.append((dt_str, net))
            signal_raw.append((dt_str, raw))

    dm_market = _day_mean_market(nd_all_by_day)
    excess_nd = []
    for dt, v in signal_nd:
        day_int = int(datetime.strptime(dt, "%Y-%m-%d")
                      .replace(tzinfo=MSK).timestamp()) // DAY
        if day_int in dm_market:
            excess_nd.append((dt, v - dm_market[day_int]))

    st_net    = _day_ci(signal_nd)
    st_excess = _day_ci(excess_nd)
    st_raw    = _day_ci(signal_raw)
    h1_net, h2_net = _split_halves(signal_nd)
    yb = _year_breakdown(signal_nd)

    L = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📈 <b>CROSS-ASSET MOMENTUM</b>",
        f"   параметры: BTC sum({XMOM_N}d) > ±{XMOM_THR:.1f}% → альты D+1",
        f"   NET:     {_fmt_ci(st_net)}",
        f"   EXCESS:  {_fmt_ci(st_excess)}",
        f"   raw(мом):{_fmt_ci(st_raw)}",
        f"   ½/½:     {_fmt_ci(h1_net)} | {_fmt_ci(h2_net)}",
    ]
    if yb:
        y_str = " | ".join(f"{y}: {m:+.2f}%({n})" for y, (n, m) in sorted(yb.items()))
        L.append(f"   годы:   {y_str}")
    ok = (st_excess and st_excess[2] - st_excess[3] > 0.15
          and h1_net and h2_net
          and h1_net[2] > 0 and h2_net[2] > 0
          and st_excess[2] > (st_raw[2] if st_raw else 0))
    L.append(f"   → {'✅ прошла планку' if ok else '❌ не прошла планку'}")
    return "\n".join(L)


# ════════════════════════════════════════════════════════════════
# 3. VOLUME ANOMALY (тихий объём → momentum)
# ════════════════════════════════════════════════════════════════

def run_vol_anomaly():
    print("[VOL] загрузка данных…")
    data = {sym: _fetch_daily(sym) for sym in LIQ10}

    signal_nd  = []
    signal_raw = []
    nd_all_by_day = {}

    for sym, candles in data.items():
        if len(candles) < VANOM_WIN + 5:
            continue
        for i in range(VANOM_WIN, len(candles) - 1):
            D  = candles[i]
            X  = candles[i + 1]
            o, cl_x = X.get("o", 0), X.get("c", 0)
            if o <= 0 or cl_x <= 0:
                continue

            nd      = (cl_x / o - 1) * 100
            day_int = X["t"] // DAY
            dt_str  = datetime.fromtimestamp(X["t"], MSK).strftime("%Y-%m-%d")

            nd_all_by_day.setdefault(day_int, {})[sym] = nd

            window  = candles[i - VANOM_WIN: i]
            vols    = [c.get("v", 0) for c in window]
            avg_vol = sum(vols) / len(vols) if vols else 0
            vol_D   = D.get("v", 0)
            ret_D   = (D.get("c", 0) / D.get("o", 1) - 1) * 100 if D.get("o", 0) > 0 else 0.0

            if avg_vol <= 0:
                continue

            # Сигнал: высокий объём + слабое движение
            if vol_D < VANOM_K * avg_vol:
                continue
            if abs(ret_D) > VANOM_MOVE:
                continue

            # Momentum: по направлению дня D (если D был слабо вверх → ЛОНГ, слабо вниз → ШОРТ)
            direction = +1 if ret_D >= 0 else -1
            nd_dir    = nd * direction
            net       = nd_dir - COST_TK - FUND_D
            raw       = nd_dir

            signal_nd.append((dt_str, net))
            signal_raw.append((dt_str, raw))

    dm_market = _day_mean_market(nd_all_by_day)
    excess_nd = []
    for dt, v in signal_nd:
        day_int = int(datetime.strptime(dt, "%Y-%m-%d")
                      .replace(tzinfo=MSK).timestamp()) // DAY
        if day_int in dm_market:
            excess_nd.append((dt, v - dm_market[day_int]))

    st_net    = _day_ci(signal_nd)
    st_excess = _day_ci(excess_nd)
    st_raw    = _day_ci(signal_raw)
    h1_net, h2_net = _split_halves(signal_nd)
    yb = _year_breakdown(signal_nd)

    L = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🔕 <b>VOLUME ANOMALY</b> (тихий объём)",
        f"   параметры: vol >{VANOM_K:.1f}× avg, |ret| <{VANOM_MOVE:.1f}% | окно {VANOM_WIN} дн",
        f"   NET:     {_fmt_ci(st_net)}",
        f"   EXCESS:  {_fmt_ci(st_excess)}",
        f"   raw(мом):{_fmt_ci(st_raw)}",
        f"   ½/½:     {_fmt_ci(h1_net)} | {_fmt_ci(h2_net)}",
    ]
    if yb:
        y_str = " | ".join(f"{y}: {m:+.2f}%({n})" for y, (n, m) in sorted(yb.items()))
        L.append(f"   годы:   {y_str}")
    ok = (st_excess and st_excess[2] - st_excess[3] > 0.15
          and h1_net and h2_net
          and h1_net[2] > 0 and h2_net[2] > 0
          and st_excess[2] > (st_raw[2] if st_raw else 0))
    L.append(f"   → {'✅ прошла планку' if ok else '❌ не прошла планку'}")
    return "\n".join(L)


# ════════════════════════════════════════════════════════════════
# MAIN
# ════════════════════════════════════════════════════════════════

def run():
    now_str = datetime.now(MSK).strftime("%Y-%m-%d %H:%M")
    header  = (f"🔬 <b>bt_signals.py — новые сигналы</b>\n"
               f"{len(LIQ10)} пар, {BT_DAYS} дн, offset {BT_OFFSET} | {now_str} МСК\n"
               f"Планка: excess ≥ +0.15% значимо (2.64σ), обе ½ > 0, excess > raw\n"
               f"Издержки: COST_TK={COST_TK:.2f}% + FUND_D={FUND_D:.2f}%/день\n"
               f"BT_MODE={BT_MODE}")

    results = [header, ""]

    if BT_MODE in ("liq", "all"):
        try:
            results.append(run_liq_cascade())
        except Exception:
            results.append(f"❌ LIQ ERROR:\n{traceback.format_exc()[-400:]}")

    if BT_MODE in ("xmom", "all"):
        try:
            results.append(run_xmom())
        except Exception:
            results.append(f"❌ XMOM ERROR:\n{traceback.format_exc()[-400:]}")

    if BT_MODE in ("vol", "all"):
        try:
            results.append(run_vol_anomaly())
        except Exception:
            results.append(f"❌ VOL ERROR:\n{traceback.format_exc()[-400:]}")

    msg = "\n".join(results)
    print(msg.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    try:
        B.send_blocks(msg.split("\n"))
    except Exception as e:
        print(f"[SIG] отправка: {e}")


def main():
    try:
        run()
    except Exception:
        traceback.print_exc()
        try:
            B.send_telegram(f"⚠️ bt_signals упал: {traceback.format_exc()[-400:]}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
