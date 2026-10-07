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
   Данные: daily candles + daily contract_stats (open_interest)
   Планка: та же, excess ≥ +0.15% значимо

3. WEEKLY SEASONALITY (BT_MODE=weekly)
   Сигнал: день недели (0=пн .. 6=вс) на 5 годах
   Метрика: средний избыточный ход каждого дня недели (excess над рынком)
   Планка: для торговли нужен хотя бы 1 день с excess > 0.15% значимо (2.64σ)
   Выход: матрица по дням + лучший/худший день

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
    """Дневные свечи для пары. Возвращает список {t, o, h, l, c, v}."""
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


def _fetch_stats(sym, days, offset):
    """contract_stats: funding_rate и open_interest по дням."""
    now = int(time.time()) - offset * DAY
    out, cur = [], now - (days + 45) * DAY
    while cur < now:
        raw = B.api_get("contract_stats", {
            "contract": f"{sym}_USDT", "type": "funding_rate",
            "interval": "1d", "from": cur, "to": min(now, cur + 500 * DAY)
        })
        part = raw if isinstance(raw, list) else (raw or [])
        if not part:
            break
        out.extend(part)
        nxt = int(part[-1].get("t", part[-1].get("time", 0))) + DAY
        if nxt <= cur:
            break
        cur = nxt
    seen, u = set(), []
    for r in sorted(out, key=lambda x: int(x.get("t", x.get("time", 0)))):
        ts = int(r.get("t", r.get("time", 0)))
        if ts not in seen:
            seen.add(ts); u.append(r)
    return u


def _fetch_oi(sym, days, offset):
    """contract_stats open_interest."""
    now = int(time.time()) - offset * DAY
    out, cur = [], now - (days + 45) * DAY
    while cur < now:
        raw = B.api_get("contract_stats", {
            "contract": f"{sym}_USDT", "type": "open_interest",
            "interval": "1d", "from": cur, "to": min(now, cur + 500 * DAY)
        })
        part = raw if isinstance(raw, list) else (raw or [])
        if not part:
            break
        out.extend(part)
        nxt = int(part[-1].get("t", part[-1].get("time", 0))) + DAY
        if nxt <= cur:
            break
        cur = nxt
    seen, u = set(), []
    for r in sorted(out, key=lambda x: int(x.get("t", x.get("time", 0)))):
        ts = int(r.get("t", r.get("time", 0)))
        if ts not in seen:
            seen.add(ts); u.append(r)
    return u


def _day_ci(pairs):
    """[(date, value)] → (n_trades, n_days, mean, Z*se) кластеризация по дням."""
    by_d = {}
    for dt, v in pairs:
        by_d.setdefault(dt, []).append(v)
    dm = [sum(v) / len(v) for v in by_d.values()]
    if not dm:
        return None
    m = sum(dm) / len(dm)
    se = statistics.pstdev(dm) / len(dm) ** 0.5 if len(dm) > 1 else 0.0
    return sum(len(v) for v in by_d.values()), len(dm), m, Z * se


def _halves(pairs):
    """[(date, value)] → (mean_h1, mean_h2) разбивка по времени."""
    ds = sorted({dt for dt, _ in pairs})
    if len(ds) < 10:
        return None, None
    mid = ds[len(ds) // 2]
    h1 = [v for dt, v in pairs if dt < mid]
    h2 = [v for dt, v in pairs if dt >= mid]
    return (sum(h1) / len(h1) if h1 else 0.0), (sum(h2) / len(h2) if h2 else 0.0)


def _day_mean_market(sig):
    """sig: {day_int: {sym: nd}} → {day_int: float} средний ход рынка."""
    return {
        d: sum(v.values()) / len(v)
        for d, v in sig.items() if len(v) >= 5
    }


# ════════════════════════════════════════════════════════════════
# 1. FUNDING FADE
# ════════════════════════════════════════════════════════════════

def run_funding_fade():
    print(f"[FF] FUNDING FADE: {len(LIQ10)} пар × {BT_DAYS} дн (offset {BT_OFFSET})")

    # Собираем: для каждого дня D по каждой паре — funding_rate дня D и nd дня D+1
    # Структура: day_d -> {sym: (funding, nd, dm_next)}
    fund_by_day = {}   # day_d -> {sym: funding_rate}
    nd_by_day   = {}   # day_d -> {sym: open→close D+1}

    for sym in LIQ10:
        candles = _fetch_daily_candles(sym, BT_DAYS, BT_OFFSET)
        stats   = _fetch_stats(sym, BT_DAYS, BT_OFFSET)

        # funding indexed by day
        fund_idx = {}
        for r in stats:
            ts = int(r.get("t", r.get("time", 0)))
            val = r.get("funding_rate", r.get("r", None))
            if val is not None:
                try:
                    fund_idx[ts // DAY] = float(val) * 100  # в %
                except (ValueError, TypeError):
                    pass

        for i in range(1, len(candles) - 1):
            c0, c1 = candles[i], candles[i + 1]
            if c1["o"] <= 0:
                continue
            d = c0["t"] // DAY
            if d not in fund_idx:
                continue
            nd = (c1["c"] / c1["o"] - 1) * 100
            fund_by_day.setdefault(d, {})[sym] = fund_idx[d]
            nd_by_day.setdefault(d, {})[sym]   = nd

    days = sorted(set(fund_by_day) & set(nd_by_day))
    dm_market = _day_mean_market(nd_by_day)

    # Для каждой пары строим перцентиль funding за TRAIL дней
    # Собираем все (dt, net, ex, direction) для разных ног
    rev, mom = [], []

    for i, d in enumerate(days):
        if d not in dm_market:
            continue
        dm = dm_market[d]
        dt = datetime.fromtimestamp(d * DAY, MSK).strftime("%Y-%m-%d")

        for sym in fund_by_day.get(d, {}):
            f_today = fund_by_day[d][sym]
            nd = nd_by_day[d].get(sym)
            if nd is None:
                continue

            # перцентиль фандинга за последние TRAIL дней
            history = [
                fund_by_day[pd][sym]
                for pd in days[max(0, i - FF_TRAIL):i]
                if sym in fund_by_day.get(pd, {})
            ]
            if len(history) < FF_TRAIL // 2:
                continue

            pct = sum(1 for x in history if x < f_today) / len(history) * 100

            # Сигнал
            if pct >= FF_PCT_HI:
                # перегрет лонгами → ШОРТ
                sgn = -1
            elif pct <= FF_PCT_LO:
                # перегрет шортами → ЛОНГ
                sgn = 1
            else:
                continue

            net = sgn * nd - COST_TK - FUND_D * sgn
            ex  = sgn * (nd - dm)
            rev.append((dt, net, ex))
            # momentum-контроль: зеркальный отбор
            mom.append((dt, -sgn * nd - COST_TK + FUND_D * sgn, -ex))

    # Статистика
    st  = _day_ci([(r[0], r[1]) for r in rev])
    ste = _day_ci([(r[0], r[2]) for r in rev])
    sm  = _day_ci([(r[0], r[1]) for r in mom])
    h1, h2 = _halves([(r[0], r[1]) for r in rev])

    ex_m  = ste[2] if ste else 0.0
    ex_ci = ste[3] if ste else 0.0
    mom_m = sm[2] if sm else 0.0

    ok = (st is not None and st[0] >= 30 and
          ex_m - ex_ci > 0.15 and
          (st[2] > 0) and
          h1 is not None and (h1 > 0) == (h2 > 0) and
          st[2] > mom_m)

    mark = "✅" if (st and st[2] - st[3] > 0) else "❌" if (st and st[2] + st[3] < 0) else "  "

    L = [
        f"💰 <b>FUNDING FADE</b>: {len(LIQ10)} пар, ~{BT_DAYS} дн",
        f"<i>шорт когда funding percentile(30д) ≥ {FF_PCT_HI:.0f}%, лонг ≤ {FF_PCT_LO:.0f}%",
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
        L.append("  ⚠️ недостаточно данных")

    # Годовой срез
    by_year = {}
    for dt, net, _ in rev:
        by_year.setdefault(dt[:4], []).append(net)
    if len(by_year) >= 3:
        L.append("")
        L.append("<b>NET по годам</b>: " +
                 " | ".join(f"{y}: {sum(v)/len(v):+.2f}% ({len(v)})"
                            for y, v in sorted(by_year.items())))

    # Квинтили фандинга
    L.append("")
    L.append("<i>Вывод: " + ("ПЛАНКА ПРОЙДЕНА — следующий шаг: RV_OFFSET=365 → форвард"
              if ok else
              "Планка не пройдена. Фандинг как сигнал дневного разворота не подтверждён") + "</i>")

    return "\n".join(L)


# ════════════════════════════════════════════════════════════════
# 2. OI DIVERGENCE
# ════════════════════════════════════════════════════════════════

def run_oi_divergence():
    print(f"[OI] OI DIVERGENCE: {len(LIQ10)} пар × {BT_DAYS} дн (offset {BT_OFFSET})")

    nd_by_day  = {}   # day_d -> {sym: nd D+1}
    sig_by_day = {}   # day_d -> {sym: direction (+1 лонг / -1 шорт)}

    for sym in LIQ10:
        candles = _fetch_daily_candles(sym, BT_DAYS, BT_OFFSET)
        oi_data = _fetch_oi(sym, BT_DAYS, BT_OFFSET)

        oi_idx = {}
        for r in oi_data:
            ts  = int(r.get("t", r.get("time", 0)))
            val = r.get("open_interest", r.get("v", None))
            if val is not None:
                try:
                    oi_idx[ts // DAY] = float(val)
                except (ValueError, TypeError):
                    pass

        for i in range(1, len(candles) - 1):
            c_prev, c_cur, c_next = candles[i - 1], candles[i], candles[i + 1]
            if c_prev["c"] <= 0 or c_cur["c"] <= 0 or c_next["o"] <= 0:
                continue
            d = c_cur["t"] // DAY
            d_prev = c_prev["t"] // DAY

            if d not in oi_idx or d_prev not in oi_idx:
                continue

            price_ret = (c_cur["c"] / c_prev["c"] - 1) * 100
            oi_ret    = (oi_idx[d] / oi_idx[d_prev] - 1) * 100 if oi_idx[d_prev] > 0 else 0.0
            nd        = (c_next["c"] / c_next["o"] - 1) * 100

            direction = None
            # Цена растёт сильно, но OI падает → слабый рост, умные уходят → ШОРТ
            if price_ret >= OI_PRICE_K and oi_ret <= -OI_OI_M:
                direction = -1
            # Цена падает сильно, но OI падает (закрытие лонгов/шортов) → дно, отскок → ЛОНГ
            elif price_ret <= -OI_PRICE_K and oi_ret <= -OI_OI_M:
                direction = 1

            if direction is None:
                continue

            nd_by_day.setdefault(d, {})[sym]  = nd
            sig_by_day.setdefault(d, {})[sym] = direction

    days = sorted(set(sig_by_day) & set(nd_by_day))
    dm_market = _day_mean_market(nd_by_day)

    rev_long, rev_short, mom = [], [], []

    for d in days:
        if d not in dm_market:
            continue
        dm = dm_market[d]
        dt = datetime.fromtimestamp(d * DAY, MSK).strftime("%Y-%m-%d")

        for sym, sgn in sig_by_day[d].items():
            nd = nd_by_day[d].get(sym)
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
    mom_m = sm[2] if sm else 0.0

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
            L.append(f"      лонг-нога (OI падает на минусе): {stl[0]} сд, NET {stl[2]:+.3f}% (±{stl[3]:.3f})")
        if sts and sts[0] >= 20:
            L.append(f"      шорт-нога (OI падает на плюсе): {sts[0]} сд, NET {sts[2]:+.3f}% (±{sts[3]:.3f})")
    else:
        L.append("  ⚠️ недостаточно данных")

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

    nd_by_day = {}   # day_d -> {sym: nd}

    for sym in LIQ10:
        candles = _fetch_daily_candles(sym, BT_DAYS, BT_OFFSET)
        for i in range(len(candles) - 1):
            c0, c1 = candles[i], candles[i + 1]
            if c1["o"] <= 0 or c0["c"] <= 0:
                continue
            d  = c0["t"] // DAY
            nd = (c1["c"] / c1["o"] - 1) * 100
            nd_by_day.setdefault(d, {})[sym] = nd

    days = sorted(nd_by_day)
    dm_market = _day_mean_market(nd_by_day)

    # excess[dow][date] = средний excess по парам того дня недели
    by_dow = {i: [] for i in range(7)}   # dow -> [(date, excess)]

    for d in days:
        if d not in dm_market or len(nd_by_day[d]) < 5:
            continue
        dm  = dm_market[d]
        dow = datetime.fromtimestamp(d * DAY, MSK).weekday()
        dt  = datetime.fromtimestamp(d * DAY, MSK).strftime("%Y-%m-%d")

        # Средний excess всех пар в этот день (лонг каждый день = рыночная ставка)
        excess_day = sum(nd - dm for nd in nd_by_day[d].values()) / len(nd_by_day[d])
        by_dow[dow].append((dt, excess_day))

    L = [
        f"📅 <b>WEEKLY SEASONALITY</b>: {len(LIQ10)} пар, ~{BT_DAYS} дн",
        f"<i>средний избыточный ход (excess над рынком дня) по дню недели.",
        f"Лонг каждый день → рыночная ставка; нас интересует аномалия конкретного дня.",
        f"Планка 2.64σ: excess ≥ +0.15% значимо на ≥1 дне недели</i>", ""
    ]

    best_day, best_ex = None, -999
    passed = []

    for dow in range(7):
        tr = by_dow[dow]
        st = _day_ci(tr)
        if not st or st[0] < 20:
            L.append(f"  {DOW_NAMES[dow]}: мало данных")
            continue
        n, nd_, m, ci = st
        mark = "✅" if m - ci > 0 else "❌" if m + ci < 0 else "  "
        ok_day = (m - ci > 0.15)
        if ok_day:
            passed.append(DOW_NAMES[dow])
        if m > best_ex:
            best_ex, best_day = m, dow
        L.append(f"  {mark} {DOW_NAMES[dow]}: {n} пар-дней, excess <b>{m:+.3f}%</b> (±{ci:.3f})"
                 + ("  ← значимо ✅" if ok_day else ""))

    L.append("")

    # Нейтральная стратегия: лонг в лучший день, шорт в худший
    if best_day is not None:
        best_tr = by_dow[best_day]
        h1, h2  = _halves(best_tr)
        L.append(f"<b>Лучший день для лонга</b>: {DOW_NAMES[best_day]} ({best_ex:+.3f}%)"
                 + (f", половины {h1:+.3f}|{h2:+.3f}" if h1 is not None else ""))

    # Годовой срез лучшего дня
    if best_day is not None:
        by_year = {}
        for dt, ex in by_dow[best_day]:
            by_year.setdefault(dt[:4], []).append(ex)
        if len(by_year) >= 3:
            L.append("<b>Excess по годам (" + DOW_NAMES[best_day] + ")</b>: " +
                     " | ".join(f"{y}: {sum(v)/len(v):+.2f}% ({len(v)})"
                                for y, v in sorted(by_year.items())))

    L.append("")
    if passed:
        L.append(f"<b>Значимые дни</b>: {', '.join(passed)} → форвард: торговать только эти дни")
    else:
        L.append("<i>Ни один день недели не показал значимой сезонности на данном горизонте</i>")

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
            results.append(f"⚠️ FUNDING FADE упал:\n{traceback.format_exc()[-300:]}")

    if BT_MODE in ("all", "oi_div"):
        try:
            results.append(run_oi_divergence())
        except Exception:
            results.append(f"⚠️ OI DIVERGENCE упал:\n{traceback.format_exc()[-300:]}")

    if BT_MODE in ("all", "weekly"):
        try:
            results.append(run_weekly_seasonality())
        except Exception:
            results.append(f"⚠️ WEEKLY SEASONALITY упал:\n{traceback.format_exc()[-300:]}")

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
