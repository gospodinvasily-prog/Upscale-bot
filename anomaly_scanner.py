# -*- coding: utf-8 -*-
"""
anomaly_scanner.py — Anomaly Scanner V17b (6 аномалий, Simple TP, без BTC)
==========================================================================
Производственный модуль для авто-торговли. V17b = V16 + убран BTC.

6 аномалий:
  1. Laggard — Z-score return пары vs рынок
  2. ZScoreDev — Z-score price vs SMA20
  3. Divergence — расхождение цены и volume flow
  4. LiqMagnet — цена около 48h high/low
  5. Exhaustion — 5+ одноцветных свечей
  6. TrendAlignment — Stacked MA + ADX растёт (новая в V16)

Логика V17b:
  - Скан в :50 каждого часа, 04:00-22:00 МСК (пропуск 07:00, 15:00)
  - Вход при ≥2 аномалиях в одну сторону (MIN=2, комбо-подтверждение)
  - SKIP_2_WEAK: combo из 2 слабых (Divergence+Exhaustion) → skip
  - BLOCK_LONG_IN_BEAR: LONG в BEAR-режиме запрещён полностью
  - Simple TP: trend=1.5×ATR, counter=1.0×ATR (без Multi-TP)
  - SL=2.0×ATR, SL всегда первый
  - 24ч кулдаун по паре после любой сделки
  - 48ч blacklist после стопа
  - MAX_OPEN = 2
  - 94 пары (без BTC + без 9 худших)
  - Без confirmation (вход сразу)

Бэктест на 94 парах за 90 дней: PF 1.59, WR 69.3%, +$3918/90д (~$1306/мес)

Команды:
  /anomaly — статус
  /anomaly_on — включить
  /anomaly_off — выключить
  /anomaly_close — закрыть позиции
  /anomaly_clean — cleaner зависших
  /anomaly_alerts — вкл/выкл уведомления
"""

import os, sys, json, time, threading, statistics, datetime as dt, requests
from collections import defaultdict

try:
    import bot as B
except Exception:
    B = None
try:
    import upscale_exec as UE
except Exception:
    UE = None

STRATEGY_TAG = "anomaly_v17b"
GATE_API = "https://api.gateio.ws/api/v4/spot"
MSK = dt.timezone(dt.timedelta(hours=3))

PAIRS = [
    "ETH","BNB","XRP","SOL","AAVE","ADA","AERO","ALGO","APT","ARB",
    "ASTER","ATOM","AVAX","BCH","BERA","BONK","BRETT","BSV",
    "CAKE","CHZ","CRO","CRV","DASH","DATA","DEEP","DEXE","DOGE",
    "DYDX","EIGEN","ENA","ENS","ETC","FARTCOIN","FET","FIL","FLOKI",
    "GALA","GRAM","GRT","HBAR","HYPE","ICP","IMX","INJ","IOTA",
    "JASMY","JTO","JUP","KAIA","KAITO","LDO","LINEA","LTC",
    "MANA","MNT","MOVE","NEAR","ONDO","ORDI","PENDLE",
    "PENGU","PEPE","PNUT","POL","POPCAT","PUMP","PYTH","QNT","RAY",
    "RENDER","RUNE","S","SAND","SEI","SHIB","SKY","STX","SUI",
    "TIA","TRUMP","TRX","TURBO","UNI","VET","VIRTUAL","WAL",
    "WIF","WLD","XLM","XMR","XTZ","ZEC","ZRO","0G"
]
# V17b (2026-10-24): BTC УБРАН — на бэктесте 90д дал -$575 убытка (худшая пара).
# BTC слишком "тяжёлый" для reversal-аномалий: большие объёмы, институциональные потоки
# ломают логику Laggard/ZScoreDev. Без BTC: PF 1.45 → 1.59 (+10%), PnL +$3052 → +$3918 (+28%)
# BTC 1D по-прежнему используется для _btc_regime (BULL/BEAR/NEUTRAL определение).
# V14 (Task ID 78): убраны 9 общих худших пар (доказано per-pair анализом 385 дней):
# MORPHO (Stop-loser, -$1071 в обеих стратегиях)
# GRASS (Low-n-anom, -$504)
# STRK (Stop-loser, -$357)
# LINK (Long-loser, -$351)
# OP (Short-loser, -$310)
# KAS (Mixed-loser, -$292)
# AXS (Short-loser, -$255)
# TAO (Mixed-loser, -$233)
# DOT (Long-loser, -$213)
# Суммарно эти 9 пар дали -$3582 потерь на обеих стратегиях

# === ПАРАМЕТРЫ V17b (захардкожены) ===
MAX_OPEN = 2
SL_MULT = 2.0
# V17b: Simple TP — один TP без частичных закрытий
# Бэктест на 95 парах: Simple TP PF 1.59 (+$3918) > Multi-TP PF 1.45 (+$3052)
TP_TREND = 1.5       # V17b: TP по тренду (быстрый)
TP_COUNTER = 1.0     # V17b: TP против тренда (очень быстрый)
MAX_HOLD_SEC = 48 * 3600
MIN_ANOMALIES = 2     # V16: комбо-подтверждение (2+ аномалии)
RISK_USD = 100
LEVERAGE = 5
MAX_MARGIN = 3000.0
SCAN_MINUTE = 50  # скан в :50 каждого часа
SYMBOL_COOLDOWN_SEC = 24 * 3600
SIDE_DAILY_STOP_USD = 300.0
BEAR_AWARE_ENABLED = True

# V16: SKIP_2_WEAK — combo из 2 слабых (Divergence+Exhaustion) → skip
USE_SKIP_2_WEAK = True
# V16: добавили TrendAlignment в STRONG (6 аномалий вместо 5)
STRONG_ANOMALIES = {"Laggard", "ZScoreDev", "LiqMagnet", "TrendAlignment"}  # PF > 1.4
WEAK_ANOMALIES = {"Divergence", "Exhaustion"}                              # PF < 1.4
# Коррелированные пары (если combo = ровно эти 2 → skip)
CORRELATION_PAIRS = []

# V16: запретить LONG в BEAR-режиме
# По бэктесту: LONG в BEAR = PF 0.76, -$718; LONG в BULL = PF 1.42, +$1490
# SHORT работает в обоих режимах (PF 2.14 / 2.21)
BLOCK_LONG_IN_BEAR = True

# Реальные комиссии Upscale
COMMISSION_PCT = 0.008  # 0.008% за операцию
SPREAD_PCT = 0.03       # рыночный спред

_data_dir = os.environ.get("BOT_DATA_DIR", "/tmp")
try: os.makedirs(_data_dir, exist_ok=True)
except: pass

STATE_FILE = f"{_data_dir}/anomaly_state.json"
TRACKED_FILE = f"{_data_dir}/anomaly_tracked.json"

_session = requests.Session()
_tracked_lock = threading.Lock()

_state = {
    "scan_mode": "off",
    "last_scan_ts": 0,
    "trades_today": 0,
    "trades_today_date": "",
    "long_cooldown_until": 0,
    "short_cooldown_until": 0,
    "symbol_blacklist": {},
    "symbol_cooldown": {},
    "long_today_pnl": 0.0,
    "short_today_pnl": 0.0,
    "pnl_date": "",
}
_stats = {"scans": 0, "signals": 0, "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0}

_scanner_thread = None
_watchdog_thread = None
_scanner_running = False

# === УТИЛИТЫ ===

def _now(): return time.time()
def _today(): return dt.datetime.now(MSK).strftime("%Y-%m-%d")
def _save():
    try:
        with open(STATE_FILE, "w") as f: json.dump(_state, f)
    except: pass

def _load():
    global _state
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                loaded = json.load(f)
                _state.update(loaded)
    except: pass

def _load_tracked():
    try:
        if os.path.exists(TRACKED_FILE): return json.load(open(TRACKED_FILE))
    except: pass
    return {}

def _save_tracked(d):
    try:
        tmp = f"{TRACKED_FILE}.tmp"
        with open(tmp, "w") as f: json.dump(d, f)
        os.replace(tmp, TRACKED_FILE)
    except: pass

def _reset_daily_pnl_if_needed():
    today = _today()
    if _state.get("pnl_date") != today:
        _state["pnl_date"] = today
        _state["long_today_pnl"] = 0.0
        _state["short_today_pnl"] = 0.0
        _save()

def _side_blocked(direction):
    _reset_daily_pnl_if_needed()
    if direction in ("LONG", "long"):
        return _state.get("long_today_pnl", 0.0) <= -SIDE_DAILY_STOP_USD
    else:
        return _state.get("short_today_pnl", 0.0) <= -SIDE_DAILY_STOP_USD

def _update_side_pnl(direction, pnl):
    _reset_daily_pnl_if_needed()
    if direction in ("LONG", "long"):
        _state["long_today_pnl"] = _state.get("long_today_pnl", 0.0) + pnl
    else:
        _state["short_today_pnl"] = _state.get("short_today_pnl", 0.0) + pnl
    _save()

# === ИНДИКАТОРЫ ===

def _sma(v, p):
    return statistics.mean(v[-p:]) if len(v) >= p else None

def _atr(c, p=14):
    if len(c) < 2: return 0
    trs = [max(c[i]["high"]-c[i]["low"], abs(c[i]["high"]-c[i-1]["close"]), abs(c[i]["low"]-c[i-1]["close"])) for i in range(1, len(c))]
    return statistics.mean(trs[-p:]) if len(trs) >= p else statistics.mean(trs)

def _adx(c, p=14):
    """V16: ADX (Average Directional Index) + Plus DI + Minus DI.
    Возвращает (adx, plus_di, minus_di). При недостатке данных — (0, 0, 0).
    Та же реализация, что в prophet_V15.py — для 6-й аномалии TrendAlignment.
    """
    if len(c) < p*2: return 0, 0, 0
    pdm, mdm, trs = [], [], []
    for i in range(1, len(c)):
        h, l = c[i]["high"], c[i]["low"]
        ph, pl = c[i-1]["high"], c[i-1]["low"]
        u, d = h-ph, pl-l
        pdm.append(u if (u>d and u>0) else 0)
        mdm.append(d if (d>u and d>0) else 0)
        pc = c[i-1]["close"]
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    av = statistics.mean(trs[:p]); ps = statistics.mean(pdm[:p]); ms = statistics.mean(mdm[:p])
    dxs = []
    for i in range(p, len(trs)):
        av = (av*(p-1)+trs[i])/p; ps = (ps*(p-1)+pdm[i])/p; ms = (ms*(p-1)+mdm[i])/p
        if av > 0:
            pdi = 100*ps/av; mdi = 100*ms/av
            s = pdi+mdi
            dxs.append(100*abs(pdi-mdi)/s if s > 0 else 0)
    if len(dxs) < p: return 0, 0, 0
    ax = statistics.mean(dxs[:p])
    for dx in dxs[p:]: ax = (ax*(p-1)+dx)/p
    return ax, 100*ps/av if av > 0 else 0, 100*ms/av if av > 0 else 0

# === FETCH ===

def _fetch(sym, iv, start_ts, end_ts):
    all_c = []; cur = start_ts
    ivs = {"4h":14400,"1h":3600,"15m":900,"1d":86400}[iv]
    while cur < end_ts:
        to = min(cur + 999*ivs, end_ts)
        try:
            r = _session.get(f"{GATE_API}/candlesticks", params={"currency_pair":f"{sym}_USDT","interval":iv,"from":str(cur),"to":str(to)}, timeout=15)
            if not r.ok: break
            batch = r.json()
            if not isinstance(batch, list) or not batch: cur = to+1; continue
            for c in batch:
                if len(c) < 6: continue
                try: all_c.append({"ts":int(c[0]),"vol":float(c[1]),"close":float(c[2]),"high":float(c[3]),"low":float(c[4]),"open":float(c[5])})
                except: pass
            lt = int(batch[-1][0])
            cur = (lt+1) if lt > cur else (to+1)
            time.sleep(0.08)
        except: break
    all_c.sort(key=lambda x: x["ts"])
    return all_c

def _btc_regime(at_ts):
    btc = _fetch("BTC", "1d", at_ts - 90*86400, at_ts + 86400)
    bc = [c for c in btc if c["ts"] <= at_ts]
    if len(bc) < 50: return 0
    sma = _sma([c["close"] for c in bc], 50)
    if not sma: return 0
    return +1 if bc[-1]["close"] > sma else -1

def _fetch_current_price(sym):
    try:
        r = _session.get(f"{GATE_API}/ticker", params={"currency_pair": f"{sym}_USDT"}, timeout=10)
        if r.ok:
            d = r.json()
            return float(d.get("last", 0))
    except: pass
    return None

# === DETECT ANOMALIES ===

def detect_anomalies(sym, scan_ts, c4h, market_returns, btc_reg):
    """Возвращает список: [(anomaly_name, direction, details)]"""
    anomalies = []
    # Только ЗАКРЫТЫЕ 4H свечи (ts + 14400 <= scan_ts)
    c4 = [c for c in c4h if c["ts"] + 14400 <= scan_ts][-100:]
    if len(c4) < 30:
        return anomalies

    last = c4[-1]
    price = last["close"]
    atr = _atr(c4, 14)
    atr_pct = (atr / price) * 100 if price > 0 else 0
    if atr_pct < 0.5 or atr_pct > 10:
        return anomalies

    # 1. LAGGARD
    if market_returns:
        market_avg = statistics.mean(market_returns)
        market_std = statistics.stdev(market_returns) if len(market_returns) > 1 else 1
        if market_std > 0 and len(c4) >= 7:
            ret_24h = (price - c4[-7]["close"]) / c4[-7]["close"] * 100
            z_score = (ret_24h - market_avg) / market_std
            if z_score <= -1.5:
                anomalies.append(("Laggard", "LONG", f"Z={z_score:+.2f}"))
            elif z_score >= 1.5:
                anomalies.append(("Laggard", "SHORT", f"Z={z_score:+.2f}"))

    # 2. ZSCOREDEV (V13.6 audit fix: убран look-ahead bias)
    # Раньше: c4[-1].close входил в sma20/std_20 → микро-смещение Z на ~5%
    # Теперь: stats считаются по c4[-21:-1] (без последней свечи), затем z от текущей price
    if len(c4) >= 21:
        prev_closes = [c["close"] for c in c4[-21:-1]]  # 20 свечей ДО последней
        sma20 = statistics.mean(prev_closes)
        std_20 = statistics.stdev(prev_closes) if len(prev_closes) > 1 else 0
        if std_20 > 0:
            z = (price - sma20) / std_20
            if z <= -2.0:
                anomalies.append(("ZScoreDev", "LONG", f"Z={z:+.2f}"))
            elif z >= 2.0:
                anomalies.append(("ZScoreDev", "SHORT", f"Z={z:+.2f}"))

    # 3. DIVERGENCE (V13.6 audit fix: нормализация объёмов + sign-based logic)
    # Раньше: баг знаков — если obv_past отрицательный, формула obv_now < obv_past*0.7
    #         работала неправильно (ловила false positives при конвергенции)
    #         + магические 0.7/1.3 без обоснования + ненормализованные объёмы
    # Теперь: volume flow в [-1, +1] (нормализованный), сравнение по знакам
    if len(c4) >= 10:
        recent = c4[-5:]
        older = c4[-10:-5]
        price_up = recent[-1]["close"] > recent[0]["close"]
        
        def vol_flow(window):
            """Нормализованный volume flow в [-1, +1]."""
            if not window or len(window) < 2: return 0.0
            total_vol = sum(abs(c["vol"]) for c in window)
            if total_vol <= 0: return 0.0
            signed = 0.0
            for i in range(1, len(window)):
                if window[i]["close"] > window[i-1]["close"]:
                    signed += window[i]["vol"]
                elif window[i]["close"] < window[i-1]["close"]:
                    signed -= window[i]["vol"]
            return signed / total_vol  # [-1, +1]
        
        vf_now = vol_flow(recent)    # [-1, +1]
        vf_past = vol_flow(older)    # [-1, +1]
        
        # Истинная дивергенция: price direction ≠ volume flow direction
        # SHORT: price↑ но volume flow отрицательный (продажи доминируют) → SHORT
        # LONG:  price↓ но volume flow положительный (покупки доминируют) → LONG
        if price_up and vf_now < -0.1 and vf_now < vf_past - 0.2:
            anomalies.append(("Divergence", "SHORT", f"цена↑ VF={vf_now:+.2f}"))
        elif not price_up and vf_now > 0.1 and vf_now > vf_past + 0.2:
            anomalies.append(("Divergence", "LONG", f"цена↓ VF={vf_now:+.2f}"))

    # 4. LIQUIDATION MAGNET (V13.5 audit fix #3: ATR-нормализация порога)
    # Раньше: фиксированный порог 1.5% (магическое число)
    # Теперь: порог = 1.0 × ATR_pct (волатильно-адаптивный)
    # На волатильных монетах (ATR=3%) порог = 3%, на спокойных (ATR=0.5%) порог = 0.5%
    # Дополнительно: добавлен breakout-confirmation — цена должна быть CLOSE к экстремуму,
    # а не просто wick (close ≥ prev_close или close > prev 4h high для LONG)
    if len(c4) >= 12:
        recent_48h = c4[-12:]
        high_48h = max(c["high"] for c in recent_48h)
        low_48h = min(c["low"] for c in recent_48h)
        dist_to_high = (high_48h - price) / price * 100
        dist_to_low = (price - low_48h) / price * 100
        # ATR-нормализованный порог (1.0×ATR_pct, не менее 0.5%, не более 3%)
        liq_threshold = max(0.5, min(3.0, atr_pct * 1.0))
        # Breakout-confirmation: последняя свеча должна быть в направлении сигнала
        last_candle = c4[-1]
        last_bullish = last_candle["close"] >= last_candle["open"]
        if dist_to_high <= liq_threshold and dist_to_high >= 0 and last_bullish:
            anomalies.append(("LiqMagnet", "LONG", f"около 48h high (+{dist_to_high:.2f}%, thr={liq_threshold:.2f}%)"))
        elif dist_to_low <= liq_threshold and dist_to_low >= 0 and not last_bullish:
            anomalies.append(("LiqMagnet", "SHORT", f"около 48h low (-{dist_to_low:.2f}%, thr={liq_threshold:.2f}%)"))

    # 5. EXHAUSTION
    if len(c4) >= 7:
        last_color = None
        consecutive = 0
        for c in reversed(c4[-7:]):
            color = "green" if c["close"] >= c["open"] else "red"
            if last_color is None:
                last_color = color; consecutive = 1
            elif color == last_color:
                consecutive += 1
            else:
                break
        if consecutive >= 5 and last_color == "green":
            anomalies.append(("Exhaustion", "SHORT", f"{consecutive} зелёных подряд"))
        elif consecutive >= 5 and last_color == "red":
            anomalies.append(("Exhaustion", "LONG", f"{consecutive} красных подряд"))

    # === АНОМАЛИЯ 6: TrendAlignment — Stacked MA + ADX растёт ===
    # V16: добавлена 6-я аномалия (как в prophet_V15.py)
    if len(c4) >= 89:
        sma13 = _sma(closes, 13)
        sma34 = _sma(closes, 34)
        sma89 = _sma(closes, 89)
        if sma13 and sma34 and sma89:
            adx_now, pdi, mdi = _adx(c4, 14)
            adx_prev = 0
            if len(c4) > 30:
                adx_prev, _, _ = _adx(c4[:-1], 14)
            last_close = c4[-1]["close"]
            if sma13 > sma34 > sma89 and last_close > sma13 and adx_now > 20 and adx_now > adx_prev:
                anomalies.append(("TrendAlignment", "LONG", f"ADX={adx_now:.0f}↑ stack✓"))
            if sma13 < sma34 < sma89 and last_close < sma13 and adx_now > 20 and adx_now > adx_prev:
                anomalies.append(("TrendAlignment", "SHORT", f"ADX={adx_now:.0f}↑ stack✓"))

    return anomalies

def _calc_market_returns(scan_ts, all_c4h):
    returns = []
    for sym in PAIRS:
        c4h = all_c4h.get(sym, [])
        c4 = [c for c in c4h if c["ts"] + 14400 <= scan_ts][-7:]
        if len(c4) >= 7:
            ret = (c4[-1]["close"] - c4[0]["close"]) / c4[0]["close"] * 100
            returns.append(ret)
    return returns

def _get_tp_mult(direction, btc_reg):
    if btc_reg == +1:
        return TP_TREND if direction == "LONG" else TP_COUNTER
    elif btc_reg == -1:
        return TP_TREND if direction == "SHORT" else TP_COUNTER
    return TP_TREND


# === V13.6: SKIP_2_WEAK логика комбо ===

def _is_2_weak_combo(anom_list):
    """Проверяет: combo состоит ТОЛЬКО из 2 weak коррелированных аномалий.
    Возвращает True если нужно ПРОПУСТИТЬ сигнал.
    
    Пропускаем если:
    - Ровно 2 аномалии в одну сторону
    - Обе слабые (Divergence, Exhaustion) → слабый сигнал
    - ИЛИ обе из корреляционной пары → дублирование
    """
    if len(anom_list) != 2: return False
    names = {n for n, _ in anom_list}
    # Случай 1: обе слабые
    if names.issubset(WEAK_ANOMALIES):
        return True
    # Случай 2: коррелированная пара
    for a, b in CORRELATION_PAIRS:
        if names == {a, b}:
            return True
    return False


def _has_strong_anomaly(anom_list):
    """Проверяет есть ли STRONG аномалия (PF > 1.4) в списке."""
    return any(n in STRONG_ANOMALIES for n, _ in anom_list)


def _count_uncorrelated(anom_list):
    """Считает количество 'uncorrelated' аномалий для BEAR фильтра.
    Коррелированные пары считаются как 1, не как 2."""
    if not anom_list: return 0
    names = {n for n, _ in anom_list}
    count = 0
    counted = set()
    for n in names:
        if n in counted: continue
        # Проверяем есть ли корреляционный партнер
        partner_found = False
        for a, b in CORRELATION_PAIRS:
            if n == a and b in names:
                count += 1
                counted.add(a); counted.add(b)
                partner_found = True
                break
            elif n == b and a in names and a not in counted:
                continue  # посчитаем когда дойдём до a
        if not partner_found:
            count += 1
            counted.add(n)
    return count



# === ПРОВЕРКА ЗАНЯТОСТИ ПАРЫ PROPHET ===

def _is_symbol_busy_prophet(sym):
    """Проверяет нет ли открытой позиции Prophet по этой паре."""
    try:
        if B and hasattr(B, "EXECUTOR"):
            # Проверяем tracked Prophet
            prophet_tracked_file = f"{_data_dir}/prophet_tracked.json"
            if os.path.exists(prophet_tracked_file):
                pt = json.load(open(prophet_tracked_file))
                for pid, rec in pt.items():
                    if rec.get("status") == "open" and rec.get("sym") == sym:
                        return True
    except: pass
    return False

# === DO SCAN ===

def do_scan(scan_ts=None):
    if not B: return None
    if scan_ts is None: scan_ts = int(_now())

    btc_reg = _btc_regime(scan_ts)
    if btc_reg == 0: return None  # NEUTRAL

    # Clean expired
    now_ts = int(_now())
    bl = _state.get("symbol_blacklist", {})
    expired = [s for s, t in bl.items() if t < now_ts]
    for s in expired: bl.pop(s, None)
    sc = _state.get("symbol_cooldown", {})
    expired_cd = [s for s, t in sc.items() if t < now_ts]
    for s in expired_cd: sc.pop(s, None)
    if expired or expired_cd:
        _state["symbol_blacklist"] = bl
        _state["symbol_cooldown"] = sc
        _save()

    # Предзагрузка
    all_c4h = {}
    for sym in PAIRS:
        c4h = _fetch(sym, "4h", scan_ts - 30*86400, scan_ts + 3600)
        if c4h: all_c4h[sym] = c4h

    market_returns = _calc_market_returns(scan_ts, all_c4h)

    tracked = _load_tracked()
    open_syms = {v.get("sym") for v in tracked.values() if v.get("status") == "open"}

    results = []
    for sym in PAIRS:
        if sym in open_syms: continue
        if sym in bl: continue
        if sym in sc: continue
        # Проверка занятости Prophet
        if _is_symbol_busy_prophet(sym): continue

        c4h = all_c4h.get(sym, [])
        if not c4h: continue

        all_anom = detect_anomalies(sym, scan_ts, c4h, market_returns, btc_reg)
        if not all_anom: continue

        long_anom = [(n, d) for n, d, _ in all_anom if d == "LONG"]
        short_anom = [(n, d) for n, d, _ in all_anom if d == "SHORT"]

        direction = None
        active = []
        
        if USE_SKIP_2_WEAK:
            # V13.6: SKIP_2_WEAK логика комбо
            # Skip если combo состоит только из 2 weak коррелированных аномалий
            if _is_2_weak_combo(long_anom) or _is_2_weak_combo(short_anom):
                continue
            
            # V17b: BLOCK_LONG_IN_BEAR — запрет LONG в BEAR-режиме (бэктест PF 0.76)
            # Если BTC в BEAR → полностью пропускаем LONG сигналы
            if len(long_anom) >= MIN_ANOMALIES and not (BLOCK_LONG_IN_BEAR and btc_reg == -1):
                # BEAR фильтр: 3+ uncorrelated LONG в BEAR → skip
                if btc_reg == -1 and _count_uncorrelated(long_anom) >= 3:
                    continue
                direction = "LONG"
                active = long_anom
            elif len(short_anom) >= MIN_ANOMALIES:
                if btc_reg == -1 and _count_uncorrelated(short_anom) >= 3:
                    continue
                direction = "SHORT"
                active = short_anom
        else:
            # Старая логика: ≥2 аномалии в одну сторону
            # V17b: BLOCK_LONG_IN_BEAR — запрет LONG в BEAR-режиме
            if len(long_anom) >= MIN_ANOMALIES and not (BLOCK_LONG_IN_BEAR and btc_reg == -1):
                # BEAR фильтр: 3+ аномалии в BEAR → skip
                if btc_reg == -1 and len(long_anom) >= 3:
                    continue
                direction = "LONG"
                active = long_anom
            elif len(short_anom) >= MIN_ANOMALIES:
                if btc_reg == -1 and len(short_anom) >= 3:
                    continue
                direction = "SHORT"
                active = short_anom

        if not direction: continue

        # Cooldown
        if direction == "LONG":
            if now_ts < _state.get("long_cooldown_until", 0): continue
        else:
            if now_ts < _state.get("short_cooldown_until", 0): continue

        # Per-side daily stop
        if _side_blocked(direction): continue

        # Считаем TP/SL
        c4 = [cc for cc in c4h if cc["ts"] + 14400 <= scan_ts][-100:]
        if len(c4) < 14: continue
        last_c = c4[-1]
        price = last_c["close"]
        atr = _atr(c4, 14)
        atr_pct = (atr / price) * 100 if price > 0 else 0
        if atr_pct < 0.5 or atr_pct > 10: continue

        tp_mult = _get_tp_mult(direction, btc_reg)
        sp = SL_MULT * atr_pct
        tp = tp_mult * atr_pct
        is_long = direction == "LONG"

        # Slippage
        if is_long:
            entry_price = price * (1 + SPREAD_PCT / 100)
        else:
            entry_price = price * (1 - SPREAD_PCT / 100)

        results.append({
            "symbol": sym,
            "direction": direction,
            "entry": entry_price,
            "score": len(active),
            "tp_mult": tp_mult,
            "stop_pct": sp,
            "tp_pct": tp,
            "atr_pct": atr_pct,
            "btc_reg": btc_reg,
            "anomalies": " | ".join([f"{n}({d})" for n, d in active]),
            "anomaly_names": [n for n, _ in active],
            "n_anomalies": len(active),
            "has_strong": _has_strong_anomaly(active),
            "is_2_weak_skipped": _is_2_weak_combo(active),  # всегда False если дошли сюда
            "stop_price": entry_price*(1-sp/100) if is_long else entry_price*(1+sp/100),
            "tp_price": entry_price*(1+tp/100) if is_long else entry_price*(1-tp/100),
        })

    if not results: return None
    # V13.6: приоритет — STRONG аномалии сначала, потом по n_anomalies
    results.sort(key=lambda x: (-int(x.get("has_strong", False)), -x["n_anomalies"]))
    return results[0]

# === EXECUTE TRADE ===

# V13.8: SLIPPAGE PROTECTION (как в Donchian 4H)
# После входа market ордером проверяем реальный entryPrice с Upscale.
# Если slippage большой — пересчитываем TP/SL под реальную цену.
# Если slippage критичный (>5%) — закрываем позицию.
SLIPPAGE_OK_PCT = 1.0       # ≤1% — ок, ничего не делаем
SLIPPAGE_ADJUST_PCT = 5.0   # 1-5% — пересчитываем TP/SL
SLIPPAGE_CLOSE_PCT = 5.0    # >5% — закрываем позицию

def _fetch_upscale_entry_price(executor, sym: str):
    """Получает реальный entryPrice позиции с Upscale API.
    Возвращает float или None."""
    try:
        if not executor or not executor.client or not executor.account_id:
            return None
        executor._ensure_account()
        if not executor.account_id: return None
        # Подгружаем markets чтобы найти market_id символа
        try: executor._refresh_markets()
        except: pass
        live = UE._as_list(executor.client.positions(executor.account_id))
        if not live: return None
        # Находим позицию по символу
        for p in live:
            if not isinstance(p, dict): continue
            lp_mid = UE._pos_market(p)
            if executor._mk:
                for s, m in executor._mk.items():
                    if str(m.get("id","")) == lp_mid and s == sym:
                        # Нашли позицию символа
                        for k in ("entryPrice", "avgEntry", "openPrice", "entry"):
                            v = p.get(k)
                            if v is None: continue
                            try:
                                t = str(v).strip()
                                if t and t not in ("None","null","0"):
                                    # from_fp9 для fixed-point
                                    try:
                                        if str(v).isdigit() and len(str(v)) > 9:
                                            return float(UE.from_fp9(t))
                                        return float(t)
                                    except: continue
                            except: continue
        return None
    except Exception as e:
        print(f"[anomaly V13.8] fetch_upscale_entry_price {sym}: {e}")
        return None


def _apply_slippage_protection(executor, sym: str, side: str,
                                 planned_entry: float, planned_stop: float,
                                 planned_tp: float, sp_pct: float, tp_pct: float,
                                 is_long: bool) -> dict:
    """V13.8: Проверяет реальный entryPrice с Upscale и применяет slippage protection.
    
    Возвращает dict с результатом:
    {
        "action": "ok"|"adjusted"|"closed"|"skipped",
        "up_entry": float,
        "slippage_pct": float,
        "new_stop": float,
        "new_tp": float,
    }
    """
    result = {"action": "skipped", "up_entry": None, "slippage_pct": 0,
              "new_stop": planned_stop, "new_tp": planned_tp}
    
    if not planned_entry or planned_entry <= 0:
        return result
    
    # Ждём 2 секунды чтобы позиция появилась в API
    time.sleep(2)
    
    up_entry = _fetch_upscale_entry_price(executor, sym)
    if not up_entry or up_entry <= 0:
        # Не смогли получить цену — пропускаем коррекцию
        print(f"[anomaly V13.8] {sym}: не удалось получить up_entry, пропускаем slippage protection")
        return result
    
    # Считаем slippage (всегда положительный, без знака направления)
    slippage_pct = abs(up_entry - planned_entry) / planned_entry * 100
    result["up_entry"] = up_entry
    result["slippage_pct"] = slippage_pct
    
    print(f"[anomaly V13.8] {sym} {side}: planned={planned_entry:.6g}, upscale={up_entry:.6g}, slippage={slippage_pct:.2f}%")
    
    if slippage_pct <= SLIPPAGE_OK_PCT:
        # OK — slippage в норме, ничего не делаем
        result["action"] = "ok"
        return result
    
    if slippage_pct > SLIPPAGE_CLOSE_PCT:
        # КРИТИЧНО — slippage >5%, закрываем позицию
        print(f"[anomaly V13.8] {sym}: SLIPPAGE {slippage_pct:.2f}% > {SLIPPAGE_CLOSE_PCT}% — ЗАКРЫВАЕМ позицию!")
        try:
            # Найти позицию и закрыть
            live = UE._as_list(executor.client.positions(executor.account_id))
            for p in live:
                if not isinstance(p, dict): continue
                lp_mid = UE._pos_market(p)
                if executor._mk:
                    for s, m in executor._mk.items():
                        if str(m.get("id","")) == lp_mid and s == sym:
                            pid = UE._pos_id(p)
                            mid = lp_mid
                            dir_field = "short" if side == "short" else "long"
                            info = {"id": pid, "mid": mid, "dir": dir_field}
                            try:
                                res = executor.close_position(info, reason=f"SLIPPAGE_PROTECTION_{slippage_pct:.1f}%")
                                result["action"] = "closed"
                                if B:
                                    B.send_telegram(
                                        f"🚨 <b>SLIPPAGE PROTECTION: {sym} ЗАКРЫТА</b>\n"
                                        f"Плановый вход: ${planned_entry:.6g}\n"
                                        f"Реальный вход: ${up_entry:.6g}\n"
                                        f"Slippage: {slippage_pct:.2f}% (>{SLIPPAGE_CLOSE_PCT}%)\n"
                                        f"Действие: позиция закрыта для защиты от искажённого RR\n"
                                        f"Результат: {res}"
                                    )
                                return result
                            except Exception as e:
                                print(f"[anomaly V13.8] {sym}: close error: {e}")
                                break
        except Exception as e:
            print(f"[anomaly V13.8] {sym}: slippage close error: {e}")
        return result
    
    # 1% < slippage ≤ 5% — пересчитываем TP/SL под реальную цену
    # Как в Donchian: new_stop = planned_stop * (up_entry / planned_entry)
    correction = up_entry / planned_entry
    if not (0.95 < correction < 1.05):
        # Защита — если коррекция слишком большая, закрываем
        print(f"[anomaly V13.8] {sym}: correction {correction} вне диапазона — пропускаем")
        return result
    
    new_stop = planned_stop * correction
    new_tp = planned_tp * correction
    
    # Применяем новые уровни через move_stop (если поддерживается executor'ом)
    try:
        live = UE._as_list(executor.client.positions(executor.account_id))
        for p in live:
            if not isinstance(p, dict): continue
            lp_mid = UE._pos_market(p)
            if executor._mk:
                for s, m in executor._mk.items():
                    if str(m.get("id","")) == lp_mid and s == sym:
                        pid = UE._pos_id(p)
                        dir_field = "short" if side == "short" else "long"
                        info = {"id": pid, "mid": lp_mid, "dir": dir_field}
                        # Перемещаем стоп
                        try:
                            if hasattr(executor, "move_stop"):
                                executor.move_stop(info, new_stop)
                        except: pass
                        # Перемещаем TP (если поддерживается)
                        try:
                            if hasattr(executor, "move_tp"):
                                executor.move_tp(info, new_tp)
                            elif hasattr(executor, "set_tp"):
                                executor.set_tp(info, new_tp)
                        except: pass
                        result["action"] = "adjusted"
                        result["new_stop"] = new_stop
                        result["new_tp"] = new_tp
                        if B:
                            B.send_telegram(
                                f"📐 <b>SLIPPAGE ADJUST: {sym} {side.upper()}</b>\n"
                                f"Плановый вход: ${planned_entry:.6g}\n"
                                f"Реальный вход: ${up_entry:.6g}\n"
                                f"Slippage: +{slippage_pct:.2f}%\n"
                                f"Стоп: ${planned_stop:.6g} → ${new_stop:.6g}\n"
                                f"TP: ${planned_tp:.6g} → ${new_tp:.6g}\n"
                                f"Коррекция: ×{correction:.4f}"
                            )
                        return result
    except Exception as e:
        print(f"[anomaly V13.8] {sym}: adjust error: {e}")
    
    return result




def execute_trade(setup):
    if not B or not hasattr(B, "EXECUTOR"): return
    executor = B.EXECUTOR
    if not executor: return

    sym = setup["symbol"]
    direction = setup["direction"]
    side = "long" if direction == "LONG" else "short"
    entry = setup["entry"]
    stop = setup["stop_price"]
    tp = setup["tp_price"]
    sp = setup["stop_pct"]

    pos_size = RISK_USD / sp * 100
    margin = pos_size / LEVERAGE
    if margin > MAX_MARGIN:
        margin = MAX_MARGIN
        pos_size = margin * LEVERAGE

    sig = {
        "symbol": sym, "side": side, "price": entry,
        "stop": stop, "stop_pct": sp, "tp1_price": tp,
        "exit_mode": "tp_ladder", "risk_usd": RISK_USD,
        "max_pos_usd": pos_size, "max_same_side": 1,
        "strategy": STRATEGY_TAG, "leverage_override": LEVERAGE, "ext_atr": 0.0,
    }

    try:
        score = int(setup["n_anomalies"] * 10)
        executor.on_signal(sig, score)
        emoji_dir = "🟢 LONG" if side == "long" else "🔴 SHORT"
        score_label = "🔥 СИЛЬНЫЙ" if setup["n_anomalies"] >= 4 else ("⭐ Приоритет" if setup["n_anomalies"] >= 3 else "обычный")
        # V13.6: показываем STRONG статус
        strong_str = " | STRONG✓" if setup.get("has_strong") else ""
        if B:
            B.send_telegram(
                f"🚨 <b>ANOMALY A+: {sym} {emoji_dir} открыт</b>\n"
                f"Вход: ${entry:.6g}\n"
                f"Стоп: ${stop:.6g} ({sp:.1f}%)\n"
                f"Тейк: ${tp:.6g} ({setup['tp_pct']:.1f}%)\n"
                f"TP×ATR: {setup['tp_mult']:.1f}\n"
                f"Маржа: ${margin:.0f} × {LEVERAGE}x\n"
                f"📊 Аномалий: {setup['n_anomalies']}/5 ({score_label}){strong_str}\n"
                f"📋 {setup['anomalies']}"
            )
        _state["trades_today"] += 1
        _stats["trades"] += 1
        _save()
        with _tracked_lock:
            tr = _load_tracked()
            tr[f"pending_{sym}_{int(_now())}"] = {
                "sym": sym, "side": side, "dir": side, "entry": entry,
                "stop": stop, "tp": tp, "opened_ts": _now(),
                "strategy": STRATEGY_TAG, "status": "open",
                "direction": direction,
                "n_anomalies": setup["n_anomalies"],
                "anomalies": setup["anomalies"],
            }
            _save_tracked(tr)
        
        # V13.8: SLIPPAGE PROTECTION — проверяем реальный entryPrice с Upscale
        # и корректируем TP/SL или закрываем позицию если slippage критичный
        try:
            slippage_result = _apply_slippage_protection(
                executor=executor, sym=sym, side=side,
                planned_entry=entry, planned_stop=stop, planned_tp=tp,
                sp_pct=sp, tp_pct=setup["tp_pct"],
                is_long=(direction == "LONG")
            )
            # Если скорректировали TP/SL — обновляем tracked
            if slippage_result.get("action") == "adjusted":
                with _tracked_lock:
                    tr = _load_tracked()
                    # Находим нашу pending_ запись и обновляем stop/tp
                    for k, v in tr.items():
                        if k.startswith("pending_") and v.get("sym") == sym and v.get("status") == "open":
                            v["stop"] = slippage_result["new_stop"]
                            v["tp"] = slippage_result["new_tp"]
                            v["entry"] = slippage_result.get("up_entry", entry)
                            v["slippage_pct"] = slippage_result["slippage_pct"]
                            v["slippage_adjusted"] = True
                            break
                    _save_tracked(tr)
            # Если закрыли позицию из-за slippage — удаляем pending_
            elif slippage_result.get("action") == "closed":
                with _tracked_lock:
                    tr = _load_tracked()
                    to_del = [k for k, v in tr.items() 
                             if k.startswith("pending_") and v.get("sym") == sym and v.get("status") == "open"]
                    for k in to_del:
                        tr[k]["status"] = "closed"
                        tr[k]["close_reason"] = "SLIPPAGE_PROTECTION"
                    _save_tracked(tr)
        except Exception as e:
            print(f"[anomaly V13.8] {sym}: slippage protection error: {e}")
    except Exception as e:
        if B: B.send_telegram(f"⚠️ Anomaly {sym}: ошибка — {e}")

# === POSITION MANAGEMENT ===

def _fetch_realized_pnl(executor, rec, pid):
    """V16: правильно ищет realized PnL позиции в orders_history.

    Что исправлено vs старого кода:
    1. from_fp9 — Upscale хранит PnL как fixed-point9 ("24300000000" = $24.30).
       float() давал 24e9, нужно UE.from_fp9().
    2. Retry до 3 раз — API часто возвращает пустой список с первого раза.
    3. Пробуем ОБА варианта asset: market_id И тикер (sym).
    4. Суммируем PnL ВСЕХ ордеров после opened_ts (TP1, TP2, финал).

    Реальный кейс 2026-10-24: SUI SHORT — TP в 03:47 (+$24.30) и закрытие
    в 14:28 (+$43.37) = $67.67. Старый код возвращал $0.00.
    """
    if not executor or not executor.client or not executor.account_id:
        return 0.0

    sym = rec.get("sym", "")
    opened_ts = rec.get("opened_ts", 0)
    if not sym or not opened_ts:
        return 0.0

    # Готовим варианты asset: market_id и тикер
    assets_to_try = []
    try:
        if executor._mk:
            m = executor._mk.get(sym)
            if m:
                mid = str(m.get("id", "")).strip()
                if mid:
                    assets_to_try.append(mid)
    except: pass
    if sym and sym not in assets_to_try:
        assets_to_try.append(sym)

    if not assets_to_try:
        return 0.0

    # Helper: парсинг PnL (как _money в upscale_exec.py history)
    def _parse_pnl(pv):
        if pv is None: return None
        t = str(pv).strip()
        if not t or t in ("None", "null", "0"):
            # "0" — это входной ордер (entry), PnL = 0, пропускаем
            return None
        try:
            # Если число с точкой или экспонентой — это уже float
            if "." in t or "e" in t.lower():
                return float(t)
            # Иначе это fixed-point9 — конвертируем
            return float(UE.from_fp9(t))
        except:
            return None

    # Helper: парсинг ts (как _when в upscale_exec.py)
    def _parse_ts(o):
        for k in ("closedAt", "filledAt", "updatedAt", "createdAt"):
            v = o.get(k)
            if not v: continue
            try:
                if isinstance(v, str) and "T" in v:
                    return dt.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
                fv = float(v)
                return fv / 1000 if fv > 1e11 else fv
            except: continue
        return 0

    total_pnl = 0.0
    found_any = False

    for asset in assets_to_try:
        data = None
        # V16: 3 попытки с backoff
        for attempt in range(3):
            try:
                data = executor.client.orders_history(executor.account_id, asset, 100)
                break
            except Exception as e:
                if attempt == 2:
                    print(f"[anomaly V17b] orders_history({asset}) failed 3x: {e}")
                else:
                    time.sleep(0.5 * (attempt + 1))

        if data is None:
            continue

        orders = UE._as_list(data)
        if not orders:
            continue

        # V16: суммируем все ордера после opened_ts - 60
        for o in orders:
            if not isinstance(o, dict):
                continue
            ts = _parse_ts(o)
            if not ts:
                continue
            if ts < opened_ts - 60:
                continue
            pv = _parse_pnl(o.get("realizedPnl"))
            if pv is None:
                continue
            total_pnl += pv
            found_any = True
            o_pid = str(o.get("positionId", "") or o.get("id", "") or "")
            print(f"[anomaly V17b] {sym} asset={asset}: order ts={ts:.0f} "
                  f"pid={o_pid[:8] if o_pid else '-'} pnl=${pv:+.2f} "
                  f"running=${total_pnl:+.2f}")

        if found_any:
            break

    return total_pnl


def manage_positions():
    if not B or not hasattr(B, "EXECUTOR"): return
    executor = B.EXECUTOR
    if not executor or not executor.client: return

    with _tracked_lock:
        tracked = _load_tracked()
    if not tracked: return

    try:
        executor._ensure_account()
        if not executor.account_id: return
        # V13.3: ВАЖНО — подгружаем markets, иначе pending_ конвертация не сработает
        # после рестарта (executor._mk пустой до первого execute())
        try: executor._refresh_markets()
        except: pass
        live = UE._as_list(executor.client.positions(executor.account_id))
        live_ids = {UE._pos_id(p) for p in live if isinstance(p, dict)}
    except: return

    now = _now(); changed = False

    for pid in list(tracked.keys()):
        rec = tracked[pid]
        if rec.get("status") != "open": continue

        if pid.startswith("pending_"):
            sym_m = rec.get("sym")
            found = False
            for lp in live:
                if not isinstance(lp, dict): continue
                lp_mid = UE._pos_market(lp)
                lp_sym = None
                if executor._mk:
                    for s, m in executor._mk.items():
                        if str(m.get("id","")) == lp_mid: lp_sym = s; break
                if lp_sym == sym_m:
                    rid = UE._pos_id(lp)
                    tracked[rid] = rec; del tracked[pid]
                    executor._registry_set(rid, STRATEGY_TAG)
                    changed = True; found = True; break
            if not found and now - rec.get("opened_ts", now) > 30:
                # V13.3: НЕ удаляем pending_ если markets не загружены —
                # иначе теряем реально открытые позиции после рестарта
                if not executor._mk:
                    print(f"[anomaly V17b] WARNING: markets не загружены, сохраняем pending_ {sym_m}")
                else:
                    # markets загружены, но символ не найден в live → реально нет позиции
                    del tracked[pid]; changed = True
            continue

        if pid not in live_ids:
            # V17b: правильный поиск realized PnL через _fetch_realized_pnl
            # Старый код парсил realizedPnl как float() — на Upscale PnL хранится как
            # fixed-point9 ("24300000000" = $24.30), float() давал 24e9.
            # Также: один вызов orders_history без retry часто возвращает пустой список.
            # Также: TP/SL — это разные ордера, их PnL надо суммировать.
            pnl = _fetch_realized_pnl(executor, rec, pid)
            debug_pnl_source = pnl  # для лога ниже

            # V13.5 FIX (audit #2): Корректное вычитание комиссии
            # Биржа уже включила комиссии в realizedPnl, но мы добавляем спред (не учтённый биржей).
            # Спред = SPREAD_PCT% × позиции = (entry × leverage × SPREAD_PCT / 100)
            try:
                pos_size_usd = rec.get("entry", 0) * LEVERAGE  # позиция в $
                # спред + биржа (вход+выход) — учитываем как % от позиции
                cost_usd = pos_size_usd * (COMMISSION_PCT * 2 + SPREAD_PCT) / 100.0
                pnl -= cost_usd
            except Exception as _e:
                print(f"[anomaly V17b] commission calc error: {_e}")

            _stats["pnl"] += pnl
            direction_field = rec.get("direction", rec.get("dir", "long"))
            _update_side_pnl(direction_field, pnl)

            # V13.5 FIX (audit #2): blacklist только при РЕАЛЬНОМ убытке (не микро-<-$0.05)
            # и только при reason=STOP (как в бэктесте)
            # если pnl близко к нулю (±$0.10) → считаем "no-trade" и не blacklist'им
            BLACKLIST_THRESHOLD_USD = -0.50  # убыток > $0.50 = реальный стоп
            if pnl < BLACKLIST_THRESHOLD_USD:
                btc_reg_close = _btc_regime(int(_now()))
                cd_hours = 48 if (BEAR_AWARE_ENABLED and btc_reg_close == -1) else 24
                if direction_field in ("LONG", "long"):
                    _state["long_cooldown_until"] = int(_now()) + cd_hours * 3600
                else:
                    _state["short_cooldown_until"] = int(_now()) + cd_hours * 3600
                bl = _state.get("symbol_blacklist", {})
                # V14 (Task ID 78): blacklist 48 часов вместо 7 дней
                # Обоснование: V3 REAL (Task 64) с 48ч blacklist дал $+26K за 218 дней
                # V13.6 с 7-дневным blacklist дал только $+82 за 385 дней
                # Альткоины меняют regime за 3-7 дней, через неделю символ в другом состоянии
                bl[rec["sym"]] = int(_now()) + 48*3600  # V14: 48ч вместо 7*24*3600
                _state["symbol_blacklist"] = bl
                # 24ч кулдаун по паре после стопа
                sc = _state.get("symbol_cooldown", {})
                sc[rec["sym"]] = int(_now()) + SYMBOL_COOLDOWN_SEC
                _state["symbol_cooldown"] = sc
                _save()
            else:
                # V14 (Task ID 78): УБРАН 24ч кулдаун после прибыльной сделки
                # Обоснование: V3 REAL ставил cooldown только после стопа → 1012 сделок
                # V13.6 ставит после любой → 430 сделок (в 2.4× меньше!)
                # После TP пара может дать новый сигнал сразу — не блокируем
                pass

            side_pnl = _state.get("long_today_pnl" if direction_field in ("LONG","long") else "short_today_pnl", 0.0)
            if side_pnl <= -SIDE_DAILY_STOP_USD:
                side_name = "LONG" if direction_field in ("LONG","long") else "SHORT"
                if B: B.send_telegram(
                    f"🚫 <b>Anomaly: {side_name} заблокирован до завтра</b>\n"
                    f"PnL по {side_name}: ${side_pnl:+.0f}\n"
                    f"Лимит: -${SIDE_DAILY_STOP_USD:.0f}"
                )
            if pnl > 0: _stats["wins"] += 1
            else: _stats["losses"] += 1
            del tracked[pid]; changed = True
        else:
            hold = now - rec.get("opened_ts", now)
            if hold >= MAX_HOLD_SEC:
                pos = next((p for p in live if UE._pos_id(p) == pid), None)
                if pos:
                    mid = UE._pos_market(pos)
                    if mid:
                        dir_field = rec.get("dir", rec.get("direction", "long"))
                        if dir_field in ("SHORT", "short"): dir_field = "short"
                        else: dir_field = "long"
                        info = {"id": pid, "mid": mid, "dir": dir_field}
                        try:
                            executor.close_position(info, reason=f"ANOMALY_TIMEOUT_{MAX_HOLD_SEC//3600}h")
                            if B: B.send_telegram(f"⏱️ Anomaly {rec['sym']}: safety-net {MAX_HOLD_SEC//3600}ч")
                        except: pass

    if changed:
        with _tracked_lock: _save_tracked(tracked)

# === SCANNER LOOP ===

def _scanner_loop():
    global _scanner_running
    _scanner_running = True
    print(f"[anomaly V17b] Scanner запущен. Скан в :{SCAN_MINUTE:02d} каждого часа")

    while True:
        try:
            if not _scanner_running:
                print(f"[anomaly V17b] Scanner: флаг _scanner_running=False, останавливаюсь")
                return

            mode = _state.get("scan_mode", "off")
            if mode == "off":
                time.sleep(60); continue

            now_dt = dt.datetime.now(MSK)
            now_ts = int(_now())

            # Скан в :45-:55 (окно :50)
            if not (45 <= now_dt.minute <= 55):
                time.sleep(30); continue

            # Дедупликация
            last_scan = _state.get("last_scan_ts", 0)
            if _now() - last_scan < 90:
                time.sleep(30); continue
            last_scan_dt = dt.datetime.fromtimestamp(last_scan, tz=MSK)
            if (last_scan_dt.year == now_dt.year and last_scan_dt.month == now_dt.month
                and last_scan_dt.day == now_dt.day and last_scan_dt.hour == now_dt.hour):
                time.sleep(60); continue

            _state["last_scan_ts"] = int(_now())
            _save()

            if _state.get("trades_today_date") != _today():
                _state["trades_today"] = 0
                _state["trades_today_date"] = _today()
                _save()

            # Manage positions
            try: manage_positions()
            except Exception as e:
                print(f"[anomaly V17b] manage_positions error: {e}")

            # Auto-sync orphans
            try:
                if B and hasattr(B, "EXECUTOR") and B.EXECUTOR and B.EXECUTOR.client:
                    _auto_sync_orphans()
            except: pass

            # Скан
            scan_dt_str = now_dt.strftime("%H:%M")
            # V13.9: показываем скан ВСЕГДА, даже если MAX_OPEN достигнут
            tracked_now = _load_tracked()
            open_now = sum(1 for v in tracked_now.values() if v.get("status") == "open")
            skip_reason = ""
            if open_now >= MAX_OPEN:
                skip_reason = f" ⏸️ MAX_OPEN достигнут ({open_now}/{MAX_OPEN}), скан пропущен"
            scan_msg = f"🔍 Anomaly V17b скан в {scan_dt_str} МСК (открыто={open_now}/{MAX_OPEN}){skip_reason}"
            print(f"[anomaly V17b] {scan_msg}")
            if B:
                try: B.send_telegram(scan_msg)
                except: pass

            tracked = _load_tracked()
            open_count = sum(1 for v in tracked.values() if v.get("status") == "open")
            if open_count >= MAX_OPEN:
                time.sleep(60); continue

            try:
                setup = do_scan(now_ts)
            except Exception as e:
                print(f"[anomaly V17b] do_scan error: {e}")
                setup = None

            _stats["scans"] += 1
            _save()

            if setup:
                direction = setup["direction"]
                emoji_dir = "🟢 LONG" if direction == "LONG" else "🔴 SHORT"
                score_label = "🔥 СИЛЬНЫЙ" if setup["n_anomalies"] >= 4 else ("⭐ Приоритет" if setup["n_anomalies"] >= 3 else "обычный")
                strong_str = " | STRONG✓" if setup.get("has_strong") else ""
                print(f"[anomaly V17b] сигнал: {setup['symbol']} {direction} ({setup['n_anomalies']}/5 {score_label})")
                _stats["signals"] += 1

                if B:
                    B.send_telegram(
                        f"🚨 <b>ANOMALY A+ — сигнал найден</b>\n"
                        f"🎯 {setup['symbol']} {emoji_dir} ({setup['n_anomalies']}/5 {score_label}{strong_str})\n"
                        f"📋 {setup['anomalies']}\n"
                        f"⏱️ Вхожу по рынку..."
                    )
                execute_trade(setup)
            else:
                if B:
                    B.send_telegram(
                        f"🔍 Anomaly V17b скан {scan_dt_str}\n"
                        f"❌ Аномалий не найдено\n"
                        f"Открыто: {open_count}/{MAX_OPEN}"
                    )

            time.sleep(60)
        except Exception as e:
            print(f"[anomaly V17b] loop error: {e}")
            time.sleep(60)

# === AUTO-SYNC ORPHANS ===

def _auto_sync_orphans(verbose_telegram=False):
    if not B or not hasattr(B, "EXECUTOR"):
        return "⚠️ Executor недоступен"
    executor = B.EXECUTOR
    if not executor or not executor.client:
        return "⚠️ Нет клиента"

    try:
        executor._ensure_account()
        if not executor.account_id: return "⚠️ Нет account_id"
        # V13.3: подгружаем markets (без них pending_ cleanup пропускается)
        try: executor._refresh_markets()
        except: pass
        live = UE._as_list(executor.client.positions(executor.account_id))
        live_ids = {UE._pos_id(p) for p in live if isinstance(p, dict)}
    except Exception as e:
        return f"⚠️ {e}"

    with _tracked_lock:
        tracked = _load_tracked()

    now_ts = int(_now())
    cleaned = 0

    # Pending orphans
    markets_ok = bool(executor._mk)
    if markets_ok:
        live_syms = {}
        if executor._mk:
            for lp in live:
                if not isinstance(lp, dict): continue
                lp_mid = UE._pos_market(lp)
                for s, m in executor._mk.items():
                    if str(m.get("id","")) == lp_mid:
                        live_syms[s] = UE._pos_id(lp); break

        for pid in list(tracked.keys()):
            rec = tracked[pid]
            if rec.get("status") != "open": continue
            if not pid.startswith("pending_"): continue
            sym = rec.get("sym", "?")
            opened_ts = rec.get("opened_ts", 0)
            age_sec = now_ts - opened_ts if opened_ts else 0
            if age_sec > 300 and sym not in live_syms:
                with _tracked_lock:
                    tr = _load_tracked()
                    if pid in tr:
                        tr[pid]["status"] = "closed"
                        tr[pid]["close_reason"] = "AUTO_SYNC_PENDING"
                        tr[pid]["closed_ts"] = now_ts
                        _save_tracked(tr)
                cleaned += 1
            elif sym in live_syms:
                real_id = live_syms[sym]
                with _tracked_lock:
                    tr = _load_tracked()
                    if pid in tr:
                        if real_id not in tr:
                            tr[real_id] = dict(tr[pid])
                            tr[real_id]["close_reason"] = ""
                        # V13.1: всегда удаляем pending_ (даже если real_id уже был)
                        del tr[pid]
                        _save_tracked(tr)
                try: executor._registry_set(real_id, STRATEGY_TAG)
                except: pass

    # Real orphans
    with _tracked_lock:
        tracked = _load_tracked()
    open_recs = [(pid, v) for pid, v in tracked.items()
                 if v.get("status") == "open" and not pid.startswith("pending_")]
    for pid, rec in open_recs:
        if pid not in live_ids:
            with _tracked_lock:
                tr = _load_tracked()
                if pid in tr:
                    tr[pid]["status"] = "closed"
                    tr[pid]["close_reason"] = "AUTO_SYNC_ORPHAN"
                    tr[pid]["closed_ts"] = now_ts
                    _save_tracked(tr)
            cleaned += 1

    if cleaned > 0:
        msg = f"🔄 Anomaly SYNC: убрано {cleaned} orphan"
        if verbose_telegram and B: B.send_telegram(msg)
        print(f"[anomaly V17b] {msg}")
        return msg
    return "✅ Anomaly SYNC: чисто"

# === CLEANER ===

def run_cleaner(reason="AUTO_CLEAN"):
    if not B or not hasattr(B, "EXECUTOR"):
        return 0, "⚠️ Executor недоступен"
    executor = B.EXECUTOR
    if not executor or not executor.client:
        return 0, "⚠️ Нет клиента"

    with _tracked_lock:
        tracked = _load_tracked()
    open_recs = [(pid, v) for pid, v in tracked.items()
                 if v.get("status") == "open" and not pid.startswith("pending_")]
    if not open_recs:
        return 0, "🧹 Нет открытых Anomaly позиций"

    try:
        executor._ensure_account()
        if not executor.account_id: return 0, "⚠️ Нет account_id"
        live = UE._as_list(executor.client.positions(executor.account_id))
        live_ids = {UE._pos_id(p) for p in live if isinstance(p, dict)}
    except Exception as e:
        return 0, f"⚠️ {e}"

    now = _now()
    max_hold = MAX_HOLD_SEC + 2 * 3600
    closed_count = 0

    for pid, rec in open_recs:
        opened_ts = rec.get("opened_ts", 0)
        too_old = opened_ts and (now - opened_ts > max_hold)
        is_orphan = pid not in live_ids
        if too_old or is_orphan:
            try:
                pos = next((p for p in live if UE._pos_id(p) == pid), None)
                if pos:
                    mid = UE._pos_market(pos)
                    if mid:
                        dir_field = rec.get("dir", rec.get("direction", "long"))
                        if dir_field in ("SHORT", "short"): dir_field = "short"
                        else: dir_field = "long"
                        info = {"id": pid, "mid": mid, "dir": dir_field}
                        executor.close_position(info, reason=reason)
                        closed_count += 1
                with _tracked_lock:
                    tr = _load_tracked()
                    if pid in tr:
                        tr[pid]["status"] = "closed"
                        tr[pid]["close_reason"] = reason
                        tr[pid]["closed_ts"] = now
                        _save_tracked(tr)
            except Exception as e:
                print(f"[anomaly] cleaner error {rec.get('sym')}: {e}")

    if closed_count > 0:
        msg = f"🧹 Anomaly Cleaner ({reason}): закрыто {closed_count}"
        if B: B.send_telegram(msg)
        return closed_count, msg
    return 0, f"🧹 Anomaly Cleaner: все позиции в норме"

# === WATCHDOG ===

def _scanner_watchdog():
    HEARTBEAT_TIMEOUT_SEC = 2 * 3600
    while True:
        try:
            time.sleep(120)
            mode = _state.get("scan_mode", "off")
            if mode == "off": continue
            global _scanner_thread

            if _scanner_thread is None or not _scanner_thread.is_alive():
                print(f"[anomaly V17b] WATCHDOG: scanner умер, перезапускаю...")
                if B:
                    try: B.send_telegram("🔄 Anomaly: scanner перезапускается (умер)")
                    except: pass
                _scanner_thread = threading.Thread(target=_scanner_loop, daemon=True, name="anomaly-scanner-restart")
                _scanner_thread.start()
                continue

            now_msk = dt.datetime.now(MSK)
            now_ts = int(_now())
            last_scan = _state.get("last_scan_ts", 0)
            age_sec = now_ts - last_scan if last_scan else 999999

            if age_sec > HEARTBEAT_TIMEOUT_SEC and 5 <= now_msk.hour <= 23:
                last_scan_str = dt.datetime.fromtimestamp(last_scan, tz=MSK).strftime("%Y-%m-%d %H:%M") if last_scan else "никогда"
                age_hours = age_sec / 3600
                print(f"[anomaly V17b] WATCHDOG: ЗАВИС! Последний скан: {last_scan_str} ({age_hours:.1f}ч)")
                if B:
                    try: B.send_telegram(f"🚨 Anomaly: scanner ЗАВИС! Последний скан: {last_scan_str} ({age_hours:.1f}ч). Перезапуск.")
                    except: pass
                _scanner_running = False
                time.sleep(5)
                _scanner_thread = threading.Thread(target=_scanner_loop, daemon=True, name="anomaly-scanner-heartbeat")
                _scanner_thread.start()
                _state["last_scan_ts"] = int(_now())
                _save()
        except Exception as e:
            print(f"[anomaly V17b] watchdog error: {e}")

# === COMMANDS ===

def cmd_status():
    try: _auto_sync_orphans(verbose_telegram=False)
    except: pass

    mode = _state.get("scan_mode", "off")
    mode_str = "🤖 Авто (вкл)" if mode == "auto" else "🔴 Выкл"
    lines = ["", "━━━ 🚨 Anomaly V17b ━━━"]
    lines.append(f"Режим: {mode_str}")
    lines.append(f"Сделок сегодня: {_state.get('trades_today', 0)}")
    tracked = _load_tracked()
    # Считаем ВСЕ открытые (включая pending_ — они тоже реальные позиции)
    open_n = sum(1 for v in tracked.values() if v.get("status") == "open")
    lines.append(f"Открыто: {open_n}/{MAX_OPEN}")
    lines.append(f"Сканирований: {_stats['scans']}")
    lines.append(f"Сигналов: {_stats['signals']}")
    if _stats["trades"] > 0:
        wr = _stats["wins"]/_stats["trades"]*100
        lines.append(f"Сделок: {_stats['trades']} (WR {wr:.0f}%)")
        lines.append(f"PnL: ${_stats['pnl']:+.2f}")
    lines.append(f"\n📅 Скан: :{SCAN_MINUTE:02d}, 04:00-22:00 МСК (без 07:00, 15:00)")
    lines.append(f"👥 Пар: {len(PAIRS)} (без BTC) | MAX_OPEN={MAX_OPEN}")
    lines.append(f"🎯 Аномалии: 6 шт (Laggard, ZScoreDev, Divergence, LiqMagnet, Exhaustion, TrendAlignment)")
    lines.append(f"⚡ Вход: MIN={MIN_ANOMALIES} (комбо-подтверждение)")
    lines.append(f"🛡️ SKIP_2_WEAK: combo из 2 слабых → skip")
    lines.append(f"🚫 LONG в BEAR: запрещён полностью")
    lines.append(f"🎯 Simple TP: trend={TP_TREND}×ATR, counter={TP_COUNTER}×ATR | SL={SL_MULT}×ATR")
    lines.append(f"🛡️ Per-side daily stop: -${SIDE_DAILY_STOP_USD:.0f}")
    lines.append(f"⏱️ Кулдаун по паре: {SYMBOL_COOLDOWN_SEC//3600}ч")
    lines.append(f"⛔ Blacklist после стопа: 48ч")
    lines.append(f"🏷️ Версия: V17b (без BTC + 6 аномалий + Simple TP)")
    if USE_SKIP_2_WEAK:
        lines.append(f"✅ SKIP_2_WEAK: фильтрует 2 weak коррелированных")
    lines.append(f"💪 STRONG аномалии (PF>1.4): Laggard, ZScoreDev, LiqMagnet, TrendAlignment")
    _reset_daily_pnl_if_needed()
    long_pnl = _state.get("long_today_pnl", 0.0)
    short_pnl = _state.get("short_today_pnl", 0.0)
    lines.append(f"\n📊 Сегодня PnL:")
    lines.append(f"  🟢 LONG:  ${long_pnl:+.0f} / -${SIDE_DAILY_STOP_USD:.0f}")
    lines.append(f"  🔴 SHORT: ${short_pnl:+.0f} / -${SIDE_DAILY_STOP_USD:.0f}")
    now_ts = int(_now())
    bl = _state.get("symbol_blacklist", {})
    active_bl = {s: t for s, t in bl.items() if t > now_ts}
    if active_bl:
        lines.append(f"⛔ Blacklist: {len(active_bl)} пар")
    sc = _state.get("symbol_cooldown", {})
    active_sc = {s: t for s, t in sc.items() if t > now_ts}
    if active_sc:
        lines.append(f"⏸️ Cooldown: {len(active_sc)} пар")
    lines.append(f"\nКоманды:")
    lines.append(f"  /anomaly_on — включить")
    lines.append(f"  /anomaly_off — выключить")
    lines.append(f"  /anomaly_close — закрыть позиции")
    lines.append(f"  /anomaly_clean — cleaner зависших")
    lines.append(f"  /anomaly_alerts — вкл/выкл уведомления")
    return "\n".join(lines)

def cmd_on():
    _state["scan_mode"] = "auto"
    _save()
    # V13.9: проверяем что scanner thread жив, если нет — перезапускаем
    global _scanner_thread, _scanner_running
    if _scanner_thread is None or not _scanner_thread.is_alive():
        print(f"[anomaly V17b] cmd_on: scanner thread мёртв, перезапускаю")
        _scanner_running = True
        _scanner_thread = threading.Thread(target=_scanner_loop, daemon=True, name="anomaly-scanner-cmd-on")
        _scanner_thread.start()
    return (
        f"🚨 <b>Anomaly V17b включён</b>\n\n"
        f"📊 <b>Настройки:</b>\n"
        f"👥 Пар: {len(PAIRS)} (без BTC) | MAX_OPEN={MAX_OPEN}\n"
        f"⏰ Скан: :{SCAN_MINUTE:02d}, 04:00-22:00 МСК (без 07:00, 15:00)\n"
        f"🎯 6 аномалий: Laggard, ZScoreDev, Divergence, LiqMagnet, Exhaustion, TrendAlignment\n"
        f"⚡ Вход: MIN={MIN_ANOMALIES} (комбо-подтверждение)\n"
        f"🛡️ SKIP_2_WEAK: combo из 2 слабых → skip\n"
        f"🚫 LONG в BEAR: запрещён полностью\n"
        f"🎯 Simple TP: trend={TP_TREND}×ATR, counter={TP_COUNTER}×ATR | SL={SL_MULT}×ATR\n"
        f"⏱️ Кулдаун: {SYMBOL_COOLDOWN_SEC//3600}ч | Blacklist: 48ч\n"
        f"🚀 Стратегия запущена"
    )

def cmd_off():
    _state["scan_mode"] = "off"
    _save()
    return "🔴 Anomaly V17b выключен. Открытые позиции продолжают работать."

def cmd_close():
    if not B or not hasattr(B, "EXECUTOR"): return "⚠️ Executor недоступен"
    executor = B.EXECUTOR
    if not executor or not executor.client: return "⚠️ Нет клиента"
    tracked = _load_tracked()
    open_recs = [(pid, v) for pid, v in tracked.items() if v.get("status") == "open" and not pid.startswith("pending_")]
    if not open_recs: return "📭 Нет открытых Anomaly позиций"
    closed = 0
    for pid, rec in open_recs:
        try:
            executor._ensure_account()
            live = UE._as_list(executor.client.positions(executor.account_id))
            pos = next((p for p in live if UE._pos_id(p) == pid), None)
            if pos:
                mid = UE._pos_market(pos)
                if mid:
                    dir_field = rec.get("dir", rec.get("direction", "long"))
                    if dir_field in ("SHORT", "short"): dir_field = "short"
                    else: dir_field = "long"
                    info = {"id": pid, "mid": mid, "dir": dir_field}
                    res = executor.close_position(info, reason="ANOMALY_MANUAL")
                    with _tracked_lock:
                        tr = _load_tracked()
                        if pid in tr: tr[pid]["status"] = "closed"; _save_tracked(tr)
                    closed += 1
                    if B: B.send_telegram(f"🛑 Anomaly {rec['sym']}: закрыто ({res})")
        except Exception as e:
            print(f"[anomaly] close error: {e}")
    return f"🛑 Закрыто: {closed}"

def cmd_clean():
    closed, msg = run_cleaner(reason="MANUAL_CLEAN")
    return msg

# === START ===

def start():
    _load()
    _reset_daily_pnl_if_needed()
    global _scanner_thread
    if _scanner_thread is None or not _scanner_thread.is_alive():
        _scanner_thread = threading.Thread(target=_scanner_loop, daemon=True, name="anomaly-scanner")
        _scanner_thread.start()
    global _watchdog_thread
    if _watchdog_thread is None:
        _watchdog_thread = threading.Thread(target=_scanner_watchdog, daemon=True, name="anomaly-watchdog")
        _watchdog_thread.start()
    print(f"[anomaly V17b] Старт: режим={_state.get('scan_mode','off')}")
    if B:
        mode_str = "🟢 ВКЛ (авто)" if _state.get('scan_mode','off') == "auto" else "🔴 ВЫКЛ"
        B.send_telegram(
            f"🚨 <b>Anomaly V17b запущен</b>\n"
            f"📊 Стратегия: {mode_str}\n"
            f"👥 Пар: {len(PAIRS)} (без BTC) | MAX_OPEN={MAX_OPEN}\n"
            f"⏰ Скан: :{SCAN_MINUTE:02d}, 04:00-22:00 МСК (без 07:00, 15:00)\n"
            f"🎯 6 аномалий: Laggard, ZScoreDev, Divergence, LiqMagnet, Exhaustion, TrendAlignment\n"
            f"⚡ Вход: MIN={MIN_ANOMALIES} (комбо-подтверждение)\n"
            f"🛡️ SKIP_2_WEAK: combo из 2 слабых → skip\n"
            f"🚫 LONG в BEAR: запрещён полностью\n"
            f"🎯 Simple TP: trend={TP_TREND}×ATR, counter={TP_COUNTER}×ATR | SL={SL_MULT}×ATR\n"
            f"⏱️ Кулдаун: {SYMBOL_COOLDOWN_SEC//3600}ч | Blacklist: 48ч\n"
            f"Команды: /anomaly /anomaly_on /anomaly_off /anomaly_close /anomaly_clean /anomaly_alerts"
        )
