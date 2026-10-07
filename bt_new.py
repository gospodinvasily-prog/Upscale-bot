"""
bt_new.py — три независимых бэктеста дневной торговли.

Запуск: RUN_BACKTEST=new   BT_MODE=funding|oi_div|weekly|all
Env общие: BT_DAYS=1825  BT_OFFSET=0

══════════════════════════════════════════════════════════════════
1. FUNDING FADE (BT_MODE=funding)
   Сигнал: перцентиль funding rate пары за 30 дн > 80% → ШОРТ следующего дня
           перцентиль < 20% → ЛОНГ следующего дня
   Логика: funding mean-reverts; высокий фандинг = лонги перегреты = давление
   Вход: open(D+1), выход: close(D+1); лонг платит фандинг, шорт получает
   Планка (2.64σ): excess ≥ +0.15% значимо, обе половины, fade > momentum

2. OI DIVERGENCE (BT_MODE=oi_div)
   Сигнал: цена за день D выросла > +k%, но OI за тот же день упало > -m%
           (умные деньги закрывают лонги на росте = слабый рост) → ШОРТ
           Зеркально: цена упала, OI упало → реальная ликвидация → ЛОНГ
   Данные: daily candles + hourly contract_stats (агрегируем в дни)
   Планка: та же, excess ≥ +0.15% значимо

3. WEEKLY SEASONALITY (BT_MODE=weekly)
   Сигнал: день недели (0=пн .. 6=вс) на 5 годах
   Метрика: raw ход open→close (лонг) по дням недели, CI кластеризация по дате
   Планка: ≥1 день с NET ≥ +0.10% значимо (2.64σ) обе половины
   Выход: матрица по дням + лучший день

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
BT_MODE   = os.environ.get("BT_MODE", "all").lower()   # funding|oi_div|weekly|all

COST_TK = (0.05 + 0.03) * 2   # 0.16% круг, ликвидные
FUND_D  = 0.03                 # %/день
Z       = 2.64
MSK     = timezone(timedelta(hours=3))
DAY     = 86400
HOUR    = 3600
LIQ10   = ["BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "DOGE", "LINK", "LTC", "AVAX"]

# ── параметры стратегий ──────────────────────────────────────────
FF_PCT_HI  = float(os.environ.get("FF_PCT_HI",  "80"))   # перцентиль для шорта
FF_PCT_LO  = float(os.environ.get("FF_PCT_LO",  "20"))   # перцентиль для лонга
FF_TRAIL   = int(os.environ.get("FF_TRAIL",     "30"))    # окно перцентиля, дней

OI_PRICE_K = float(os.environ.get("OI_PRICE_K", "2.0"))  # мин. ход цены %
OI_OI_M    = float(os.environ.get("OI_OI_M",    "1.0"))  # мин. падение OI %


# ════════════════════════════════════════════════════════════════
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ════════════════════════════════════════════════════════════════

def _fetch_daily_candles(sym, days, offset):
    """Дневные свечи. Возвращает [{t, o, h, l, c, v}]."""
    now = int(time.time()) - offset * DAY
    out, cur = [], now - (days + 45) * DAY
    while cur < now:
        raw = B.api_get("candlesticks", {
            "contract": f"{sym}_USDT", "interval": "1d",
            "from": cur, "to": min(now, cur + 1000 * DAY)
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
            seen.add(c["t"]); u.append(c)
    return u[:-1]


def _fetch_funding_history(sym, days, offset):
    """
    GET /futures/usdt/funding_rate — история выплат фандинга.
    Возвращает {day_int: avg_funding_rate_%} (среднее по выплатам за день).
    Gate.io возвращает поля: t (unix), r (rate как строка).
    Выплаты 3 раза в день (00:00 / 08:00 / 16:00 UTC), агрегируем в дни.
    """
    now = int(time.time()) - offset * DAY
    start = now - (days + 10) * DAY
    out = []
    # Gate.io /funding_rate: limit макс 1000, пагинация по from/to
    cur = start
    while cur < now:
        raw = B.api_get("funding_rate", {
            "contract": f"{sym}_USDT",
            "from": cur,
            "to":   min(now, cur + 200 * DAY),
            "limit": 1000,
        })
        rows = raw if isinstance(raw, list) else []
        if not rows:
            break
        out.extend(rows)
        nxt = int(float(rows[-1].get("t", 0))) + 1
        if nxt <= cur:
            break
        cur = nxt

    by_day = {}
    for r in out:
        ts = int(float(r.get("t", 0)))
        d  = ts // DAY
        try:
            rate = float(r.get("r", 0)) * 100   # в % за один период
        except (ValueError, TypeError):
            continue
        by_day.setdefault(d, []).append(rate)

    return {d: sum(v) / len(v) for d, v in by_day.items()}


def _fetch_oi_daily(sym, days, offset):
    """
    OI из contract_stats по часам, агрегируем в дни (последнее значение дня).
    Возвращает {day_int: oi_float}.
    """
    now = int(time.time()) - offset * DAY
    out, cur = [], now - (days + 5) * DAY
    while cur < now:
        raw = B.api_get("contract_stats", {
            "contract": f"{sym}_USDT", "interval": "1h",
            "from": cur, "to": min(now, cur + 100 * HOUR),
            "limit": 100
        })
        rows = raw if isinstance(raw, list) else []
        if not rows:
            break
        out.extend(rows)
        nxt = int(float(rows[-1].get("time", rows[-1].get("t", 0)))) + HOUR
        if nxt <= cur:
            break
        cur = nxt

    by_day = {}
    for r in out:
        ts = int(float(r.get("time", r.get("t", 0))))
        d  = ts // DAY
        oi = r.get("open_interest", r.get("oi"))
        if oi is not None:
            try:
                by_day[d] = float(oi)   # перезаписываем — берём последнее значение дня
            except (ValueError, TypeError):
                pass
    return by_day


def _day_ci(pairs):
    """[(date_str, value)] → (n, n_days, mean, Z*se) кластеризация по дням."""
    by_d = {}
    for dt, v in pairs:
        by_d.setdefault(dt, []).append(v)
    dm = [sum(v) / len(v) for v in by_d.values()]
    if not dm:
        return None
    m  = sum(dm) / len(dm)
    se = statistics.pstdev(dm) / len(dm) ** 0.5 if len(dm) > 1 else 0.0
    return sum(len(v) for v in by_d.values()), len(dm), m, Z * se


def _halves(pairs):
    """[(date_str, value)] → (mean_h1, mean_h2)."""
    ds = sorted({dt for dt, _ in pairs})
    if len(ds) < 10:
        return None, None
    mid = ds[len(ds) // 2]
    h1 = [v for dt, v in pairs if dt < mid]
    h2 = [v for dt, v in pairs if dt >= mid]
    return (sum(h1) / len(h1) if h1 else 0.0), (sum(h2) / len(h2) if h2 else 0.0)


def _day_mean_market(nd_all_pairs_by_day):
    """
    nd_all_pairs_by_day: {day_int: {sym: nd}} — ВСЕ пары, не только сигнальные.
    Возвращает {day_int: float} — средний ход рынка.
    """
    return {
        d: sum(v.values()) / len(v)
        for d, v in nd_all_pairs_by_day.items() if len(v) >= 5
    }


# ════════════════════════════════════════════════════════════════
# 1. FUNDING FADE
# ════════════════════════════════════════════════════════════════

def run_funding_fade():
    print(f"[FF] FUNDING FADE: {len(LIQ10)} пар × {BT_DAYS} дн (offset {BT_OFFSET})")

    # Собираем по всем парам: day -> {sym: (funding_rate, nd_next)}
    fund_by_day = {}    # day_d -> {sym: fr (% в день)}
    nd_all      = {}    # day_d -> {sym: nd D+1} — ВСЕ пары для market mean

    for sym in LIQ10:
        print(f"[FF]   {sym}...")
        candles  = _fetch_daily_candles(sym, BT_DAYS, BT_OFFSET)
        fund_idx = _fetch_funding_history(sym, BT_DAYS, BT_OFFSET)  # {day_int: avg_fr%}

        for i in range(len(candles) - 1):
            c0, c1 = candles[i], candles[i + 1]
            if c0["c"] <= 0 or c1["o"] <= 0:
                continue
            d  = c0["t"] // DAY
            nd = (c1["c"] / c1["o"] - 1) * 100

            # nd_all собираем всегда (для market mean)
            nd_all.setdefault(d, {})[sym] = nd

            # fund только если есть данные за этот день
            if d in fund_idx:
                fund_by_day.setdefault(d, {})[sym] = fund_idx[d]

    days       = sorted(set(fund_by_day))
    dm_market  = _day_mean_market(nd_all)

    rev, mom = [], []

    for i, d in enumerate(days):
        if d not in dm_market:
            continue
        dm = dm_market[d]
        dt = datetime.fromtimestamp(d * DAY, MSK).strftime("%Y-%m-%d")

        for sym, f_today in fund_by_day[d].items():
            nd = nd_all.get(d, {}).get(sym)
            if nd is None:
                continue

            # rolling percentile за последние FF_TRAIL дней
            history = [
                fund_by_day[pd][sym]
                for pd in days[max(0, i - FF_TRAIL):i]
                if sym in fund_by_day.get(pd, {})
            ]
            if len(history) < FF_TRAIL // 2:
                continue

            pct = sum(1 for x in history if x < f_today) / len(history) * 100

            if pct >= FF_PCT_HI:
                sgn = -1   # перегрет лонгами → ШОРТ
            elif pct <= FF_PCT_LO:
                sgn = 1    # перегрет шортами → ЛОНГ
            else:
                continue

            net = sgn * nd - COST_TK - FUND_D * sgn
            ex  = sgn * (nd - dm)
            rev.append((dt, net, ex))
            mom.append((dt, -sgn * nd - COST_TK + FUND_D * sgn, -ex))

    st  = _day_ci([(r[0], r[1]) for r in rev])
    ste = _day_ci([(r[0], r[2]) for r in rev])
    sm  = _day_ci([(r[0], r[1]) for r in mom])
    h1, h2 = _halves([(r[0], r[1]) for r in rev])

    ex_m  = ste[2] if ste else 0.0
    ex_ci = ste[3] if ste else 0.0
    mom_m = sm[2]  if sm  else 0.0

    ok = (st is not None and st[0] >= 30 and
          ex_m - ex_ci > 0.15 and
          st[2] > 0 and
          h1 is not None and (h1 > 0) == (h2 > 0) and
          st[2] > mom_m)

    mark = "✅" if (st and st[2] - st[3] > 0) else "❌" if (st and st[2] + st[3] < 0) else "  "

    L = [
        f"💰 <b>FUNDING FADE</b>: {len(LIQ10)} пар, ~{BT_DAYS} дн",
        f"<i>шорт когда funding percentile({FF_TRAIL}д) ≥ {FF_PCT_HI:.0f}%, лонг ≤ {FF_PCT_LO:.0f}%",
        f"ход D+1 open→close | издержки {COST_TK:.2f}% | 2.64σ</i>", ""
    ]

    if st:
        n, nd_, m, ci = st
        L.append(f"  {mark} NET: {n} сд / {nd_} дн, <b>{m:+.3f}%</b> (±{ci:.3f})"
                 + (f", половины {h1:+.3f}|{h2:+.3f}" if h1 is not None else ""))
        L.append(f"      EXCESS: {ex_m:+.3f}% (±{ex_ci:.3f}) | "
                 f"momentum: {mom_m:+.3f}% → fade {'>' if st[2] > mom_m else '<'} momentum"
                 + ("  ← ПЛАНКА ✅" if ok else ""))
    else:
        L.append("  ⚠️ недостаточно данных (проверь доступность /funding_rate для этих пар)")

    by_year = {}
    for dt, net, _ in rev:
        by_year.setdefault(dt[:4], []).append(net)
    if len(by_year) >= 3:
        L.append("")
        L.append("<b>NET по годам</b>: " +
                 " | ".join(f"{y}: {sum(v)/len(v):+.2f}% ({len(v)})"
                            for y, v in sorted(by_year.items())))

    L.append("")
    L.append("<i>Вывод: " + ("ПЛАНКА ПРОЙДЕНА — следующий шаг: BT_OFFSET=365 → форвард"
              if ok else
              "Планка не пройдена. Фандинг как сигнал дневного разворота не подтверждён") + "</i>")

    return "\n".join(L)


# ════════════════════════════════════════════════════════════════
# 2. OI DIVERGENCE
# ════════════════════════════════════════════════════════════════

def run_oi_divergence():
    print(f"[OI] OI DIVERGENCE: {len(LIQ10)} пар × {BT_DAYS} дн (offset {BT_OFFSET})")

    nd_all     = {}   # day_d -> {sym: nd D+1} — ВСЕ пары
    sig_by_day = {}   # day_d -> {sym: direction}

    for sym in LIQ10:
        print(f"[OI]   {sym}...")
        candles = _fetch_daily_candles(sym, BT_DAYS, BT_OFFSET)
        oi_idx  = _fetch_oi_daily(sym, BT_DAYS, BT_OFFSET)  # {day_int: oi}

        for i in range(1, len(candles) - 1):
            c_prev, c_cur, c_next = candles[i - 1], candles[i], candles[i + 1]
            if c_prev["c"] <= 0 or c_cur["c"] <= 0 or c_next["o"] <= 0:
                continue
            d      = c_cur["t"] // DAY
            d_prev = c_prev["t"] // DAY

            nd = (c_next["c"] / c_next["o"] - 1) * 100
            nd_all.setdefault(d, {})[sym] = nd

            oi_cur  = oi_idx.get(d)
            oi_prev = oi_idx.get(d_prev)
            if oi_cur is None or oi_prev is None or oi_prev <= 0:
                continue

            price_ret = (c_cur["c"] / c_prev["c"] - 1) * 100
            oi_ret    = (oi_cur / oi_prev - 1) * 100

            direction = None
            if price_ret >= OI_PRICE_K and oi_ret <= -OI_OI_M:
                direction = -1   # цена выросла + OI упал → ШОРТ
            elif price_ret <= -OI_PRICE_K and oi_ret <= -OI_OI_M:
                direction = 1    # цена упала  + OI упал → ЛОНГ

            if direction is not None:
                sig_by_day.setdefault(d, {})[sym] = direction

    days      = sorted(nd_all)
    dm_market = _day_mean_market(nd_all)

    rev_long, rev_short, mom = [], [], []

    for d in days:
        if d not in sig_by_day or d not in dm_market:
            continue
        dm = dm_market[d]
        dt = datetime.fromtimestamp(d * DAY, MSK).strftime("%Y-%m-%d")

        for sym, sgn in sig_by_day[d].items():
            nd = nd_all.get(d, {}).get(sym)
            if nd is None:
                continue
            net = sgn * nd - COST_TK - FUND_D * sgn
            ex  = sgn * (nd - dm)
            row = (dt, net, ex)
            (rev_long if sgn == 1 else rev_short).append(row)
            mom.append((dt, -sgn * nd - COST_TK + FUND_D * sgn, -ex))

    rev_all = rev_long + rev_short
    st  = _day_ci([(r[0], r[1]) for r in rev_all])
    ste = _day_ci([(r[0], r[2]) for r in rev_all])
    sm  = _day_ci([(r[0], r[1]) for r in mom])
    h1, h2 = _halves([(r[0], r[1]) for r in rev_all])
    stl = _day_ci([(r[0], r[1]) for r in rev_long])
    sts = _day_ci([(r[0], r[1]) for r in rev_short])

    ex_m  = ste[2] if ste else 0.0
    ex_ci = ste[3] if ste else 0.0
    mom_m = sm[2]  if sm  else 0.0

    ok = (st is not None and st[0] >= 30 and
          ex_m - ex_ci > 0.15 and
          st[2] > 0 and
          h1 is not None and (h1 > 0) == (h2 > 0) and
          st[2] > mom_m)

    mark = "✅" if (st and st[2] - st[3] > 0) else "❌" if (st and st[2] + st[3] < 0) else "  "

    L = [
        f"📊 <b>OI DIVERGENCE</b>: {len(LIQ10)} пар, ~{BT_DAYS} дн",
        f"<i>шорт: цена ≥+{OI_PRICE_K:.1f}% & OI ≤-{OI_OI_M:.1f}% (умные уходят на росте)",
        f"лонг: цена ≤-{OI_PRICE_K:.1f}% & OI ≤-{OI_OI_M:.1f}% (ликвидация, дно)",
        f"ход D+1 open→close | издержки {COST_TK:.2f}% | 2.64σ</i>", ""
    ]

    if st:
        n, nd_, m, ci = st
        L.append(f"  {mark} ВСЕГО: {n} сд / {nd_} дн, <b>{m:+.3f}%</b> (±{ci:.3f})"
                 + (f", половины {h1:+.3f}|{h2:+.3f}" if h1 is not None else ""))
        L.append(f"      EXCESS: {ex_m:+.3f}% (±{ex_ci:.3f}) | "
                 f"momentum: {mom_m:+.3f}%"
                 + ("  ← ПЛАНКА ✅" if ok else ""))
        if stl and stl[0] >= 20:
            L.append(f"      лонг-нога (цена↓ + OI↓): {stl[0]} сд, NET {stl[2]:+.3f}% (±{stl[3]:.3f})")
        if sts and sts[0] >= 20:
            L.append(f"      шорт-нога (цена↑ + OI↓): {sts[0]} сд, NET {sts[2]:+.3f}% (±{sts[3]:.3f})")
    else:
        L.append("  ⚠️ недостаточно данных (OI из hourly contract_stats — проверь покрытие)")

    by_year = {}
    for dt, net, _ in rev_all:
        by_year.setdefault(dt[:4], []).append(net)
    if len(by_year) >= 3:
        L.append("")
        L.append("<b>NET по годам</b>: " +
                 " | ".join(f"{y}: {sum(v)/len(v):+.2f}% ({len(v)})"
                            for y, v in sorted(by_year.items())))

    L.append("")
    L.append("<i>Вывод: " + ("ПЛАНКА ПРОЙДЕНА"
              if ok else
              "Планка не пройдена. OI-дивергенция на дневном горизонте не монетизируется") + "</i>")

    return "\n".join(L)


# ════════════════════════════════════════════════════════════════
# 3. WEEKLY SEASONALITY
# ════════════════════════════════════════════════════════════════

DOW_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

def run_weekly_seasonality():
    print(f"[WS] WEEKLY SEASONALITY: {len(LIQ10)} пар × {BT_DAYS} дн (offset {BT_OFFSET})")

    # Собираем (date_str, dow, nd) для каждой пары-дня
    # NET = nd - COST_TK (лонг каждый день)
    # excess = nd - mean_nd_ALL_pairs_ALL_days (убираем дрейф рынка)
    all_nd = []   # (date_str, dow, nd)
    nd_by_day = {}

    for sym in LIQ10:
        print(f"[WS]   {sym}...")
        candles = _fetch_daily_candles(sym, BT_DAYS, BT_OFFSET)
        for i in range(len(candles) - 1):
            c0, c1 = candles[i], candles[i + 1]
            if c0["c"] <= 0 or c1["o"] <= 0:
                continue
            d   = c0["t"] // DAY
            nd  = (c1["c"] / c1["o"] - 1) * 100
            dt  = datetime.fromtimestamp(d * DAY, MSK).strftime("%Y-%m-%d")
            dow = datetime.fromtimestamp(d * DAY, MSK).weekday()
            all_nd.append((dt, dow, nd))
            nd_by_day.setdefault(d, {})[sym] = nd

    if not all_nd:
        return "📅 <b>WEEKLY SEASONALITY</b>: нет данных"

    # Глобальное среднее дневного хода (дрейф рынка) — для excess
    global_mean = sum(x[2] for x in all_nd) / len(all_nd)

    # dm_market по дням — для внутридневной нейтрализации (excess над рынком конкретного дня)
    dm_market = _day_mean_market(nd_by_day)

    # by_dow[dow] = [(date_str, net, excess)]
    by_dow = {i: [] for i in range(7)}
    for dt, dow, nd in all_nd:
        d_int  = int(datetime.strptime(dt, "%Y-%m-%d").replace(tzinfo=MSK).timestamp()) // DAY
        dm_day = dm_market.get(d_int, global_mean)
        net    = nd - COST_TK
        ex     = nd - dm_day   # excess над рынком конкретного дня (межпарное сравнение)
        by_dow[dow].append((dt, net, ex))

    L = [
        f"📅 <b>WEEKLY SEASONALITY</b>: {len(LIQ10)} пар, ~{BT_DAYS} дн",
        f"<i>лонг каждый день → рыночная ставка (global mean {global_mean:+.3f}%/д).",
        f"NET = ход open→close − {COST_TK:.2f}% издержки.",
        f"Excess = ход пары − avg_рынок того дня (нейтрализует общий рыночный ход).",
        f"Планка 2.64σ: NET > 0 значимо + обе половины</i>", ""
    ]

    best_day, best_net = None, -999
    passed = []

    for dow in range(7):
        tr = by_dow[dow]
        if not tr:
            continue
        st_net = _day_ci([(r[0], r[1]) for r in tr])
        st_ex  = _day_ci([(r[0], r[2]) for r in tr])
        if not st_net or st_net[0] < 20:
            L.append(f"  {DOW_NAMES[dow]}: мало данных")
            continue
        n, nd_, m_net, ci_net = st_net
        m_ex  = st_ex[2]  if st_ex  else 0.0
        ci_ex = st_ex[3]  if st_ex  else 0.0

        h1, h2 = _halves([(r[0], r[1]) for r in tr])
        mark = "✅" if m_net - ci_net > 0 else "❌" if m_net + ci_net < 0 else "  "
        ok_day = (m_net - ci_net > 0.10 and h1 is not None and (h1 > 0) == (h2 > 0))
        if ok_day:
            passed.append(DOW_NAMES[dow])
        if m_net > best_net:
            best_net, best_day = m_net, dow

        L.append(f"  {mark} {DOW_NAMES[dow]}: {n} пар-дней | "
                 f"NET <b>{m_net:+.3f}%</b> (±{ci_net:.3f})"
                 + (f" | excess {m_ex:+.3f}% (±{ci_ex:.3f})" if st_ex else "")
                 + (f" | половины {h1:+.3f}|{h2:+.3f}" if h1 is not None else "")
                 + ("  ← значимо ✅" if ok_day else ""))

    L.append("")

    if best_day is not None:
        best_tr = by_dow[best_day]
        h1b, h2b = _halves([(r[0], r[1]) for r in best_tr])
        L.append(f"<b>Лучший день</b>: {DOW_NAMES[best_day]} (NET {best_net:+.3f}%)"
                 + (f", половины {h1b:+.3f}|{h2b:+.3f}" if h1b is not None else ""))

        by_year = {}
        for dt, net, _ in best_tr:
            by_year.setdefault(dt[:4], []).append(net)
        if len(by_year) >= 3:
            L.append("<b>NET по годам (" + DOW_NAMES[best_day] + ")</b>: " +
                     " | ".join(f"{y}: {sum(v)/len(v):+.2f}% ({len(v)})"
                                for y, v in sorted(by_year.items())))

    L.append("")
    if passed:
        L.append(f"<b>Значимые дни</b>: {', '.join(passed)} → форвард: торговать только эти дни")
    else:
        L.append("<i>Ни один день недели не показал значимого NET > 0 на данном горизонте</i>")

    return "\n".join(L)


# ════════════════════════════════════════════════════════════════
# ДИСПЕТЧЕР
# ════════════════════════════════════════════════════════════════

def run():
    results = []
    if BT_MODE in ("all", "funding"):
        try:
            results.append(run_funding_fade())
        except Exception:
            results.append(f"⚠️ FUNDING FADE упал:\n{traceback.format_exc()[-400:]}")

    if BT_MODE in ("all", "oi_div"):
        try:
            results.append(run_oi_divergence())
        except Exception:
            results.append(f"⚠️ OI DIVERGENCE упал:\n{traceback.format_exc()[-400:]}")

    if BT_MODE in ("all", "weekly"):
        try:
            results.append(run_weekly_seasonality())
        except Exception:
            results.append(f"⚠️ WEEKLY SEASONALITY упал:\n{traceback.format_exc()[-400:]}")

    full = "\n\n══════════════════════════\n\n".join(results)
    print(full.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    try:
        B.send_blocks(full.split("\n"))
    except Exception as e:
        print(f"[BT_NEW] отправка: {e}")


def main():
    try:
        run()
    except Exception:
        traceback.print_exc()
        try:
            B.send_telegram(f"⚠️ bt_new упал: {traceback.format_exc()[-400:]}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
