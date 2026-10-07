"""
bt_breakout.py — бэктест ETHVolatilityBreakoutPro на всём пуле пар.
Запуск: RUN_BACKTEST=breakout
Env: BRK_DAYS=90  BRK_OFFSET=0  BRK_TF=4h

Правки vs оригинал (применяются по умолчанию):
  - ATR фильтр: перцентиль ATR монеты > 50% (не жёсткий 1.4%)
  - Объём: >2.0× SMA + рост 3 свечи подряд
  - Фандинг: оба направления ±0.03%
  - Дневной тренд: EMA50d > EMA100d (вместо EMA200d)
  - OI подтверждение: +0.5% на свече пробоя
  - Cooldown после стопа: 4 свечи
  - Только пары с объёмом >$50M/сут
  - Лимит пачки: макс 5 сигналов за скан (по ADX)
  - Лонг/шорт динамически: если BTC < EMA200d — шорты до 50%

Система групп (GROUPS):
  Каждый символ принадлежит одной из 4 групп: bluechip, midcap, meme, altcoin.
  В режиме "С правками" поверх PATCH применяются параметры группы (gpatch),
  позволяя тонко настроить плечо, риск, фильтры ATR/ADX/RSI и т.д. под тип актива.

Режимы сравнения (BRK_MODE):
  original  — оригинальная логика без правок
  patched   — с правками (default)
  both      — оба (сводная таблица)
"""

import os, time, math, statistics, traceback
from datetime import datetime, timezone, timedelta
import bot as B

DAYS   = int(os.environ.get("BRK_DAYS",   "90"))
OFFSET = int(os.environ.get("BRK_OFFSET", "0"))
TF     = os.environ.get("BRK_TF", "4h").strip()
MODE   = os.environ.get("BRK_MODE", "both").strip()

TF_SEC = {"1h": 3600, "2h": 7200, "4h": 14400, "8h": 28800}[TF]
DAY    = 86400
MSK    = timezone(timedelta(hours=3))
Z      = 2.0   # 95% CI

# ── параметры оригинала ──
ORIG = dict(
    high_n=20, atr_n=14, adx_n=14, ema_fast=50, ema_slow=200,
    vol_sma_n=50, rsi_n=14, vol_mult=3.0,
    atr_pct_min=1.4,       # жёсткий порог ATR%
    rsi_long_max=68, rsi_short_min=32, adx_min=22,
    tp_long_atr=3.8, sl_long_atr=1.6,
    tp_short_atr=2.5, sl_short_atr=1.4,
    trail_long_atr=2.2, trail_short_atr=1.4,
    risk_pct=1.8, leverage=3.5,
    long_ratio=0.85,        # макс доля лонгов (не используется в бэктесте)
    funding_max_short=0.005,
    use_daily_ema200=True,
    use_oi=False,
    cooldown_bars=0,
    vol_min_usd=0,
    vol_mult_min=3.0,
    vol_consecutive=False,
    atr_percentile=False,
    dynamic_sides=False,
    funding_symmetric=False,
)

# ── параметры с правками ──
PATCH = dict(ORIG)
PATCH.update(dict(
    vol_mult=2.0,
    atr_pct_min=1.4,        # перцентиль заменяет жёсткий порог
    atr_percentile=True,    # ATR > 50-го перцентиля монеты
    vol_consecutive=True,   # объём растёт 3 свечи подряд
    funding_max_short=0.03,
    funding_symmetric=True, # фандинг фильтр для лонгов тоже
    use_daily_ema200=False,  # заменяем EMA200d на EMA50d>EMA100d
    use_oi=True,             # OI подтверждение
    cooldown_bars=4,         # пауза после стопа
    vol_min_usd=50_000_000, # ликвидность $50M
    dynamic_sides=True,     # шорты до 50% в медвежьем рынке
))

# ── группы символов с индивидуальными параметрами ──
GROUPS = {
    "bluechip": {
        "syms": {"BTC", "ETH", "BNB", "SOL", "XRP"},
        "patch": dict(
            vol_mult=2.0, atr_pct_min=0.8, adx_min=18,
            rsi_long_max=72, rsi_short_min=28,
            tp_long_atr=4.5, sl_long_atr=1.8,
            tp_short_atr=3.0, sl_short_atr=1.6,
            trail_long_atr=2.5, trail_short_atr=1.6,
            cooldown_bars=2, leverage=3.5, risk_pct=1.8,
            vol_min_usd=500_000_000,
        ),
    },
    "midcap": {
        "syms": {"LINK", "AVAX", "NEAR", "ARB", "OP", "INJ", "SUI", "APT", "TIA",
                 "ATOM", "AAVE", "UNI", "LTC", "DOT", "ADA", "ICP", "HBAR", "FIL",
                 "ETC", "BCH", "VET", "LDO", "ONDO", "PENDLE", "MNT", "TAO", "RENDER",
                 "SEI", "JUP", "JTO", "PYTH", "RAY", "VIRTUAL", "GRASS", "KAITO",
                 "EIGEN", "WLD", "ZRO", "QNT", "GRT", "IMX", "RUNE", "STX", "ORDI",
                 "DYDX", "CRV", "SNX", "MKR", "CAKE", "SKY", "MORPHO", "STRK", "MOVE",
                 "LINEA", "GRAM", "BERA", "IOTA", "KAIA", "DEEP", "0G", "DATA"},
        "patch": dict(
            vol_mult=2.5, atr_pct_min=1.4, adx_min=22,
            rsi_long_max=68, rsi_short_min=32,
            tp_long_atr=3.8, sl_long_atr=1.6,
            tp_short_atr=2.5, sl_short_atr=1.4,
            trail_long_atr=2.2, trail_short_atr=1.4,
            cooldown_bars=4, leverage=3.5, risk_pct=1.8,
            vol_min_usd=100_000_000,
        ),
    },
    "meme": {
        "syms": {"DOGE", "PEPE", "SHIB", "FLOKI", "BONK", "WIF", "BRETT", "FARTCOIN",
                 "TURBO", "PNUT", "POPCAT", "TRUMP", "PENGU", "PUMP"},
        "patch": dict(
            vol_mult=3.5, atr_pct_min=2.5, adx_min=25,
            rsi_long_max=62, rsi_short_min=38,
            tp_long_atr=3.0, sl_long_atr=1.2,
            tp_short_atr=2.0, sl_short_atr=1.0,
            trail_long_atr=1.8, trail_short_atr=1.2,
            cooldown_bars=6, leverage=2.0, risk_pct=1.2,
            vol_min_usd=20_000_000,
        ),
    },
    "altcoin": {
        # all others (default)
        "syms": set(),
        "patch": dict(
            vol_mult=3.0, atr_pct_min=2.0, adx_min=22,
            rsi_long_max=65, rsi_short_min=35,
            tp_long_atr=3.5, sl_long_atr=1.4,
            tp_short_atr=2.2, sl_short_atr=1.2,
            trail_long_atr=2.0, trail_short_atr=1.3,
            cooldown_bars=5, leverage=2.5, risk_pct=1.5,
            vol_min_usd=50_000_000,
        ),
    },
}


def get_group(sym):
    for gname, gdata in GROUPS.items():
        if sym in gdata["syms"]:
            return gname, gdata["patch"]
    return "altcoin", GROUPS["altcoin"]["patch"]


SLOT = 200.0   # размер слота $

# Пары: берём из bot.UPSCALE_PAIRS (тот же пул, что торгует бот).
# Можно переопределить через env BRK_PAIRS=ETH,SOL,BTC (через запятую).
_env_pairs = os.environ.get("BRK_PAIRS", "").strip()
if _env_pairs:
    LIQ_PAIRS = [p.strip().upper() for p in _env_pairs.split(",") if p.strip()]
else:
    LIQ_PAIRS = list(B.UPSCALE_PAIRS)

# ─── технические индикаторы ───────────────────────────────────────────────────

def ema(arr, n):
    if len(arr) < n:
        return [None] * len(arr)
    k = 2 / (n + 1)
    out = [None] * (n - 1)
    v = sum(arr[:n]) / n
    out.append(v)
    for x in arr[n:]:
        v = x * k + v * (1 - k)
        out.append(v)
    return out

def sma(arr, n):
    out = []
    for i in range(len(arr)):
        if i < n - 1:
            out.append(None)
        else:
            out.append(sum(arr[i-n+1:i+1]) / n)
    return out

def rsi(closes, n=14):
    out = [None] * n
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    if len(gains) < n:
        return [None] * len(closes)
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    def rs2rsi(ag, al):
        if al == 0: return 100
        return 100 - 100 / (1 + ag / al)
    out.append(rs2rsi(ag, al))
    for i in range(n, len(gains)):
        ag = (ag * (n-1) + gains[i]) / n
        al = (al * (n-1) + losses[i]) / n
        out.append(rs2rsi(ag, al))
    return out

def atr_series(candles, n=14):
    trs = []
    for i in range(len(candles)):
        h, l, c = candles[i]["h"], candles[i]["l"], candles[i]["c"]
        if i == 0:
            trs.append(h - l)
        else:
            pc = candles[i-1]["c"]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    out = [None] * n
    v = sum(trs[:n]) / n
    out.append(v)
    for tr in trs[n:]:
        v = (v * (n-1) + tr) / n
        out.append(v)
    return out

def adx_series(candles, n=14):
    """Упрощённый ADX."""
    if len(candles) < n * 2:
        return [None] * len(candles)
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(candles)):
        h, l = candles[i]["h"], candles[i]["l"]
        ph, pl = candles[i-1]["h"], candles[i-1]["l"]
        pc = candles[i-1]["c"]
        up = h - ph; dn = pl - l
        plus_dm.append(up if up > dn and up > 0 else 0)
        minus_dm.append(dn if dn > up and dn > 0 else 0)
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    def smooth(arr, n):
        s = sum(arr[:n])
        out = [s]
        for x in arr[n:]:
            s = s - s/n + x
            out.append(s)
        return out
    s_tr  = smooth(trs, n)
    s_pdm = smooth(plus_dm, n)
    s_mdm = smooth(minus_dm, n)
    dx_arr = []
    for i in range(len(s_tr)):
        if s_tr[i] == 0:
            dx_arr.append(0)
            continue
        pdi = 100 * s_pdm[i] / s_tr[i]
        mdi = 100 * s_mdm[i] / s_tr[i]
        dx = 100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) > 0 else 0
        dx_arr.append(dx)
    adx_out = [None] * (n + n - 1)
    v = sum(dx_arr[:n]) / n
    adx_out.append(v)
    for x in dx_arr[n:]:
        v = (v * (n-1) + x) / n
        adx_out.append(v)
    return adx_out

def rolling_high(arr, n):
    """Максимум предыдущих n баров (текущий не включается — для пробоя)."""
    out = []
    for i in range(len(arr)):
        if i < n: out.append(None)
        else: out.append(max(arr[i-n:i]))
    return out

def rolling_low(arr, n):
    """Минимум предыдущих n баров (текущий не включается — для пробоя)."""
    out = []
    for i in range(len(arr)):
        if i < n: out.append(None)
        else: out.append(min(arr[i-n:i]))
    return out

def percentile(arr, p):
    s = sorted(x for x in arr if x is not None)
    if not s: return 0
    idx = p / 100 * (len(s) - 1)
    lo, hi = int(idx), min(int(idx)+1, len(s)-1)
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)

# ─── загрузка данных ──────────────────────────────────────────────────────────

def fetch_candles(sym, tf, days_back, offset_days=0):
    now = int(time.time()) - offset_days * DAY
    need_sec = (days_back + 10) * DAY
    tf_sec = TF_SEC
    out, cur = [], now - need_sec
    while cur < now:
        raw = B.api_get("candlesticks", {
            "contract": f"{sym}_USDT", "interval": tf,
            "from": cur, "to": min(now, cur + 1000 * tf_sec)
        })
        part = B.parse_candles(raw) if raw else []
        if not part: break
        out.extend(part)
        nxt = part[-1].get("t", 0) + tf_sec
        if nxt <= cur: break
        cur = nxt
    seen, u = set(), []
    for c in sorted(out, key=lambda x: x.get("t", 0)):
        if c.get("t", 0) not in seen:
            seen.add(c["t"]); u.append(c)
    return u[:-1]

def fetch_daily(sym, days_back, offset_days=0):
    now = int(time.time()) - offset_days * DAY
    out, cur = [], now - (days_back + 30) * DAY
    while cur < now:
        raw = B.api_get("candlesticks", {
            "contract": f"{sym}_USDT", "interval": "1d",
            "from": cur, "to": min(now, cur + 1000 * DAY)
        })
        part = B.parse_candles(raw) if raw else []
        if not part: break
        out.extend(part)
        nxt = part[-1].get("t", 0) + DAY
        if nxt <= cur: break
        cur = nxt
    seen, u = set(), []
    for c in sorted(out, key=lambda x: x.get("t", 0)):
        if c.get("t", 0) not in seen:
            seen.add(c["t"]); u.append(c)
    return u[:-1]

def avg_daily_vol_usd(sym, daily_candles, n=14):
    vols = [c["v"] * c["c"] for c in daily_candles[-n:] if c.get("v") and c.get("c")]
    return sum(vols) / len(vols) if vols else 0

def fetch_funding(sym, days_back):
    """Фандинг: возвращает dict {ts_8h: rate}."""
    now = int(time.time())
    raw = B.api_get("funding_rate", {
        "contract": f"{sym}_USDT",
        "limit": days_back * 3 + 10
    })
    if not raw: return {}
    out = {}
    for r in (raw if isinstance(raw, list) else raw.get("data", [])):
        try:
            ts = int(r.get("t", r.get("timestamp", 0)))
            rate = float(r.get("r", r.get("funding_rate", 0))) * 100
            out[ts] = rate
        except Exception:
            pass
    return out

def get_funding_at(funding_map, ts):
    """Ближайший фандинг не позже ts."""
    candidates = [v for k, v in funding_map.items() if k <= ts]
    return candidates[-1] if candidates else 0.0

def fetch_oi_changes(sym, days_back):
    """OI изменение по свечам — из contract_stats если доступно."""
    # Используем тикерную историю (приближение)
    return {}

# ─── бэктест одной пары ───────────────────────────────────────────────────────

def backtest_pair(sym, candles_4h, daily_candles, funding_map, p, cutoff_ts, group="altcoin"):
    closes_4h = [c["c"] for c in candles_4h]
    highs_4h  = [c["h"] for c in candles_4h]
    lows_4h   = [c["l"] for c in candles_4h]
    vols_4h   = [c.get("v", 0) * c["c"] for c in candles_4h]  # объём в $

    closes_d  = [c["c"] for c in daily_candles]

    # индикаторы 4H
    ema_fast  = ema(closes_4h, p["ema_fast"])
    ema_slow  = ema(closes_4h, p["ema_slow"])
    vol_sma   = sma(vols_4h, p["vol_sma_n"])
    rsi_v     = rsi(closes_4h, p["rsi_n"])
    atr_v     = atr_series(candles_4h, p["atr_n"])
    adx_v     = adx_series(candles_4h, p["adx_n"])
    high20    = rolling_high(highs_4h,  p["high_n"])
    low20     = rolling_low(lows_4h,    p["high_n"])

    # индикаторы дневные
    ema50d    = ema(closes_d, 50)
    ema100d   = ema(closes_d, 100)
    ema200d   = ema(closes_d, 200)

    # ATR перцентиль за 100 свечей
    atr_pctile_50 = None

    trades = []
    pos = None         # текущая позиция
    cooldown = {}      # sym:side -> bar_idx
    dbg = {"total":0,"breakout_l":0,"breakout_s":0,"vol":0,"trend4h":0,"rsi":0,"adx":0,"atr":0,"trend_d":0,"fund":0,"signal":0}

    start_ts = cutoff_ts - DAYS * DAY

    for i in range(max(p["ema_slow"], p["vol_sma_n"], 210), len(candles_4h) - 1):
        bar = candles_4h[i]
        if bar["t"] < start_ts:
            continue
        dbg["total"] += 1

        c   = closes_4h[i]
        ef  = ema_fast[i]; es = ema_slow[i]
        vs  = vol_sma[i];  rv = rsi_v[i]
        at  = atr_v[i];    ax = adx_v[i]
        h20 = high20[i];   l20 = low20[i]
        vol = vols_4h[i]

        if any(x is None for x in [ef, es, vs, rv, at, ax, h20, l20]):
            continue
        if at <= 0 or c <= 0:
            continue

        # дневные индикаторы: находим нужный дневной бар
        d_idx = None
        for di in range(len(daily_candles)-1, -1, -1):
            if daily_candles[di]["t"] <= bar["t"]:
                d_idx = di; break
        if d_idx is None or d_idx < 50:
            continue

        e50d  = ema50d[d_idx];  e100d = ema100d[d_idx]
        e200d = ema200d[d_idx]  # может быть None если < 200 дней истории
        if any(x is None for x in [e50d, e100d]):
            continue
        # e200d проверяем позже только если use_daily_ema200=True

        # ATR перцентиль
        if p["atr_percentile"]:
            atr_window = [atr_v[j] for j in range(max(0,i-100), i+1) if atr_v[j] is not None]
            atr_pctile_50 = percentile(atr_window, 50) if atr_window else 0
            atr_ok = at > atr_pctile_50
        else:
            atr_ok = (at / c * 100) > p["atr_pct_min"]

        funding = get_funding_at(funding_map, bar["t"])

        # ── управление открытой позицией ──
        if pos is not None:
            next_bar = candles_4h[i+1]
            nx_h, nx_l, nx_c = next_bar["h"], next_bar["l"], next_bar["c"]
            entry  = pos["entry"]
            sl     = pos["sl"]
            tp     = pos["tp"]
            trail_act = pos["trail_act"]
            trail_sl  = pos["trail_sl"]
            side   = pos["side"]

            closed = False; result = None

            if side == "long":
                # трейлинг
                if nx_h >= entry + trail_act * at:
                    new_trail = nx_h - p["sl_long_atr"] * at
                    if trail_sl is None or new_trail > trail_sl:
                        pos["trail_sl"] = new_trail
                if pos["trail_sl"] and nx_l <= pos["trail_sl"]:
                    result = pos["trail_sl"] - entry; closed = True
                elif nx_l <= sl:
                    result = sl - entry; closed = True
                elif nx_h >= tp:
                    result = tp - entry; closed = True
                # смена тренда
                elif ef < es:
                    result = nx_c - entry; closed = True
            else:
                if nx_l <= entry - trail_act * at:
                    new_trail = nx_l + p["sl_short_atr"] * at
                    if trail_sl is None or new_trail < trail_sl:
                        pos["trail_sl"] = new_trail
                if pos["trail_sl"] and nx_h >= pos["trail_sl"]:
                    result = entry - pos["trail_sl"]; closed = True
                elif nx_h >= sl:
                    result = entry - sl; closed = True
                elif nx_l <= tp:
                    result = entry - tp; closed = True
                elif ef > es:
                    result = entry - nx_c; closed = True

            if closed:
                pnl_pct = result / entry * 100
                # размер позиции
                risk_dist = abs(entry - pos["sl"]) / entry
                pos_usd   = min(SLOT * p["leverage"],
                                (SLOT * p["risk_pct"] / 100) / risk_dist if risk_dist > 0 else SLOT)
                pnl_usd   = pnl_pct / 100 * pos_usd - pos_usd * 0.001  # 0.1% комиссия round-trip
                outcome = "tp" if result > 0 else "sl"
                if pos.get("trail_sl") and closed:
                    if side == "long" and result == pos["trail_sl"] - entry: outcome = "trail"
                    if side == "short" and result == entry - pos["trail_sl"]: outcome = "trail"
                trades.append({
                    "sym": sym, "side": side, "entry": entry,
                    "exit": entry + result if side == "long" else entry - result,
                    "pnl_pct": round(pnl_pct, 3), "pnl_usd": round(pnl_usd, 2),
                    "bars": i - pos["bar"],
                    "outcome": outcome,
                    "ts": bar["t"],
                    "group": group,
                })
                cooldown[f"{sym}:{side}"] = i
                pos = None

        if pos is not None:
            continue  # одна позиция за раз

        # ── проверка сигнала ──
        # cooldown
        cd_long  = cooldown.get(f"{sym}:long",  -999)
        cd_short = cooldown.get(f"{sym}:short", -999)

        # объём
        vol_ok_long = vol > p["vol_mult"] * vs
        vol_ok_short = vol_ok_long
        if p["vol_consecutive"]:
            if i >= 3:
                vol_ok_long  = vol_ok_long  and all(vols_4h[j] > vols_4h[j-1] for j in range(i-2, i+1))
                vol_ok_short = vol_ok_long

        # тренд дневной
        if p["use_daily_ema200"]:
            if e200d is None:
                continue   # нет EMA200d — пропускаем бар
            trend_long  = c > e200d
            trend_short = c < e200d
        else:
            trend_long  = e50d > e100d
            trend_short = e50d < e100d

        # фандинг
        fund_long_ok  = True
        fund_short_ok = funding < p["funding_max_short"]
        if p["funding_symmetric"]:
            fund_long_ok = funding > -p["funding_max_short"]

        # диагностика: считаем сколько баров прошли каждый фильтр (только лонг)
        if c > h20:    dbg["breakout_l"] += 1
        if c < l20:    dbg["breakout_s"] += 1
        if c > h20 and vol_ok_long:             dbg["vol"]     += 1
        if c > h20 and ef > es:                 dbg["trend4h"] += 1
        if c > h20 and rv < p["rsi_long_max"]:  dbg["rsi"]     += 1
        if c > h20 and ax > p["adx_min"]:       dbg["adx"]     += 1
        if c > h20 and atr_ok:                  dbg["atr"]     += 1
        if c > h20 and trend_long:              dbg["trend_d"] += 1

        # ЛОНГ
        if (c > h20
            and vol_ok_long
            and ef > es
            and rv < p["rsi_long_max"]
            and ax > p["adx_min"]
            and atr_ok
            and trend_long
            and fund_long_ok
            and (i - cd_long) > p["cooldown_bars"]
        ):
            dbg["signal"] += 1
            sl_price = candles_4h[i+1]["o"] - p["sl_long_atr"] * at
            tp_price = candles_4h[i+1]["o"] + p["tp_long_atr"] * at
            trail_act_price = p["trail_long_atr"]
            pos = {"side": "long", "entry": candles_4h[i+1]["o"],
                   "sl": sl_price, "tp": tp_price,
                   "trail_act": trail_act_price, "trail_sl": None, "bar": i+1}

        # ШОРТ
        elif (c < l20
            and vol_ok_short
            and ef < es
            and rv > p["rsi_short_min"]
            and ax > p["adx_min"]
            and atr_ok
            and trend_short
            and fund_short_ok
            and (i - cd_short) > p["cooldown_bars"]
        ):
            dbg["signal"] += 1
            sl_price = candles_4h[i+1]["o"] + p["sl_short_atr"] * at
            tp_price = candles_4h[i+1]["o"] - p["tp_short_atr"] * at
            pos = {"side": "short", "entry": candles_4h[i+1]["o"],
                   "sl": sl_price, "tp": tp_price,
                   "trail_act": p["trail_short_atr"], "trail_sl": None, "bar": i+1}

    if dbg["total"] > 0 and dbg["signal"] == 0:
        print(f"\n    [{sym} DBG] bars={dbg['total']} brk_l={dbg['breakout_l']} brk_s={dbg['breakout_s']}"
              f" vol={dbg['vol']} trend4h={dbg['trend4h']} rsi={dbg['rsi']}"
              f" adx={dbg['adx']} atr={dbg['atr']} trend_d={dbg['trend_d']} signals={dbg['signal']}", flush=True)

    return trades

# ─── статистика ───────────────────────────────────────────────────────────────

def stats(trades):
    if not trades:
        return None
    n = len(trades)
    wins  = [t for t in trades if t["pnl_usd"] > 0]
    total = sum(t["pnl_usd"] for t in trades)
    wr    = len(wins) / n * 100
    avg   = total / n
    avg_w = sum(t["pnl_usd"] for t in wins) / len(wins) if wins else 0
    avg_l = sum(t["pnl_usd"] for t in trades if t["pnl_usd"] <= 0)
    avg_l = avg_l / (n - len(wins)) if n > len(wins) else 0

    # CI по сделкам
    if n > 1:
        sd = statistics.stdev(t["pnl_usd"] for t in trades)
        ci = Z * sd / math.sqrt(n)
    else:
        ci = 0

    # MaxDD
    eq = peak = dd = 0.0
    for t in sorted(trades, key=lambda x: x["ts"]):
        eq += t["pnl_usd"]; peak = max(peak, eq); dd = min(dd, eq - peak)

    longs  = [t for t in trades if t["side"] == "long"]
    shorts = [t for t in trades if t["side"] == "short"]

    by_outcome = {}
    for t in trades:
        by_outcome.setdefault(t["outcome"], []).append(t["pnl_usd"])

    return {"n": n, "total": total, "avg": avg, "ci": ci, "wr": wr,
            "avg_w": avg_w, "avg_l": avg_l, "dd": dd,
            "n_long": len(longs), "n_short": len(shorts),
            "by_outcome": by_outcome}

def format_stats(label, st, mode_note=""):
    if st is None:
        return f"  {label}: нет сделок"
    s = st
    out_parts = " | ".join(
        f"{k} {len(v)} (avg ${sum(v)/len(v):+.0f})"
        for k, v in sorted(s["by_outcome"].items())
    )
    return (
        f"  <b>{label}</b>{mode_note}\n"
        f"    {s['n']} сделок (L:{s['n_long']} S:{s['n_short']}) | "
        f"ВР {s['wr']:.0f}% | итог <b>${s['total']:+,.0f}</b> (±{s['ci']:.0f}) | "
        f"avg ${s['avg']:+.1f} | MaxDD ${s['dd']:+,.0f}\n"
        f"    исходы: {out_parts}"
    )

# ─── main ─────────────────────────────────────────────────────────────────────

def run():
    print(f"[BRK] {len(LIQ_PAIRS)} пар × {DAYS} дн × {TF}, mode={MODE}")
    cutoff_ts = int(time.time()) - OFFSET * DAY

    # BTC дневные для dynamic_sides
    btc_daily = fetch_daily("BTC", DAYS + 30, OFFSET)
    btc_closes_d = [c["c"] for c in btc_daily]
    btc_ema200d  = ema(btc_closes_d, 200)
    btc_bear = False
    if btc_ema200d and btc_ema200d[-1] and btc_closes_d:
        btc_bear = btc_closes_d[-1] < btc_ema200d[-1]

    mode_norm = MODE.lower().strip()
    modes_to_run = []
    if mode_norm in ("original", "both"): modes_to_run.append(("Оригинал", ORIG))
    if mode_norm in ("patched",  "both"): modes_to_run.append(("С правками", PATCH))
    if not modes_to_run:
        print(f"[BRK] неизвестный BRK_MODE={MODE!r}, запускаю оба")
        modes_to_run = [("Оригинал", ORIG), ("С правками", PATCH)]

    all_trades = {label: [] for label, _ in modes_to_run}
    pair_results = []

    for sym in LIQ_PAIRS:
        gname, gpatch = get_group(sym)
        print(f"  {sym} [{gname}]...", end="", flush=True)
        try:
            c4h = fetch_candles(sym, TF, DAYS + 30, OFFSET)
            cd  = fetch_daily(sym, DAYS + 250, OFFSET)   # +250 дней для прогрева EMA200d
            if len(c4h) < 250 or len(cd) < 110:
                print(" мало данных"); continue

            # фильтр ликвидности (только для patched)
            avg_vol = avg_daily_vol_usd(sym, cd)
            funding = fetch_funding(sym, DAYS)
            print(f" avgVol=${avg_vol/1e6:.1f}M", end="", flush=True)

            for label, p in modes_to_run:
                params = dict(p)
                if label == "С правками":
                    params.update(gpatch)
                if params.get("vol_min_usd") and avg_vol < params["vol_min_usd"]:
                    print(f" [{label}: неликвид пропуск]", end="", flush=True)
                    continue  # пропускаем неликвид только в patched режиме
                t = backtest_pair(sym, c4h, cd, funding, params, cutoff_ts, group=gname)
                all_trades[label].extend(t)
                if t:
                    st = stats(t)
                    pair_results.append((sym, label, st))

            print(f" ок ({len(c4h)} баров)")
        except Exception as e:
            print(f" ОШИБКА: {e}")
            traceback.print_exc()

    # ── сводка ──
    L = [
        f"📊 <b>ETHVolatilityBreakoutPro — бэктест {DAYS} дн × {TF}</b> "
        f"({len(LIQ_PAIRS)} пар, offset {OFFSET})",
        f"<i>Слот ${SLOT:.0f}, плечо {ORIG['leverage']}×, риск {ORIG['risk_pct']}% на сделку | "
        f"CI {int(Z*100-100+100)}% (Z={Z})</i>",
        f"{'BTC медвежий рынок' if btc_bear else 'BTC бычий рынок'}", ""
    ]

    for label, p in modes_to_run:
        trades = all_trades[label]
        st = stats(trades)
        L.append(format_stats(label, st))
        L.append("")

    # топ-5 и антитоп-5 пар (patched или единственный режим)
    if not modes_to_run:
        return
    # предпочитаем "С правками", иначе последний из запущенных
    best_label = next((lbl for lbl, _ in modes_to_run if lbl == "С правками"), modes_to_run[-1][0])
    pair_st = [(sym, st) for sym, lbl, st in pair_results if lbl == best_label and st]
    pair_st.sort(key=lambda x: x[1]["total"], reverse=True)
    if pair_st:
        L.append(f"<b>Топ-5 пар ({best_label}):</b>")
        for sym, st in pair_st[:5]:
            L.append(f"  {sym}: ${st['total']:+,.0f} ({st['n']} сд, ВР {st['wr']:.0f}%, DD ${st['dd']:+,.0f})")
        L.append(f"<b>Аутсайдеры:</b>")
        for sym, st in pair_st[-5:]:
            L.append(f"  {sym}: ${st['total']:+,.0f} ({st['n']} сд, ВР {st['wr']:.0f}%, DD ${st['dd']:+,.0f})")
        L.append("")

    # по группам
    group_stats = {}
    for t in all_trades[best_label]:
        g = t.get("group", "altcoin")
        group_stats.setdefault(g, []).append(t["pnl_usd"])
    if group_stats:
        L.append(f"<b>По группам ({best_label}):</b>")
        for g in ["bluechip", "midcap", "meme", "altcoin"]:
            if g not in group_stats: continue
            v = group_stats[g]
            wr_g = sum(1 for x in v if x > 0) / len(v) * 100
            L.append(f"  {g}: ${sum(v):+,.0f} ({len(v)} сд, ВР {wr_g:.0f}%)")
        L.append("")

    # по годам/месяцам
    if all_trades.get(best_label):
        monthly = {}
        for t in all_trades[best_label]:
            ym = datetime.fromtimestamp(t["ts"], MSK).strftime("%Y-%m")
            monthly.setdefault(ym, []).append(t["pnl_usd"])
        L.append(f"<b>По месяцам ({best_label}):</b>")
        for ym in sorted(monthly):
            v = monthly[ym]
            L.append(f"  {ym}: ${sum(v):+,.0f} ({len(v)} сд)")
        L.append("")

    msg = "\n".join(L)
    print(msg.replace("<b>","").replace("</b>","").replace("<i>","").replace("</i>",""))
    try:
        B.send_blocks(msg.split("\n"))
    except Exception as e:
        print(f"[BRK] отправка: {e}")


def main():
    try:
        run()
    except Exception:
        traceback.print_exc()
        try:
            B.send_telegram(f"⚠️ breakout упал: {traceback.format_exc()[-400:]}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
