# -*- coding: utf-8 -*-
"""
prophet.py — Пророк V12 (Multi-TF Scanner + Auto-trade + SHORT + Dynamic TP + Confirmation)
============================================================================================
V12 ФИНАЛЬНАЯ ВЕРСИЯ. Изменения от рабочей V8:
  1. Добавлен SHORT (зеркальная логика LONG в analyze())
  2. Динамический TP: ADX<25 → TP=2.5×ATR, ADX≥25 → TP=3.0×ATR
  3. MIN_SCORE = 60 (было 70)
  4. MAX_OPEN = 3 (было 2)
  5. AUTO_HOURS_MSK = V12 (15 часов: 5,6,8,9,10,11,12,14,15,17,18,19,20,21,22, без 7/13/16)
  6. Сканы строго в HH:15 МСК (избегаем 4H candle close в HH:00)
  7. Подтверждение входа: после сигнала ждём 15 мин, перепроверяем 15m свечу
  8. TP/SL mirror для SHORT (stop выше, tp ниже)
  9. RSI scoring реверсивный для SHORT
 10. send_signal показывает 🟢 LONG / 🔴 SHORT
 11. execute_trade передаёт side="long" / side="short"
 12. tracked записи помечены "dir": side
 13. cmd_close / run_cleaner используют rec["dir"] для закрытия
 14. Direction cooldown 24h после убытка, symbol blacklist 7 дней

Режимы:
  /prophet              — статус
  /prophet_scan         — один скан сейчас
  /prophet_scan on      — непрерывный скан (каждый час в :15)
  /prophet_scan off     — остановить
  /prophet_auto on      — авто (V12 часы скана)
  /prophet_auto off     — остановить
  /prophet_close        — закрыть позицию
  /prophet_stats        — статистика
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

STRATEGY_TAG = "prophet_v12"
GATE_API = "https://api.gateio.ws/api/v4/spot"
MSK = dt.timezone(dt.timedelta(hours=3))

PAIRS = [
    "BTC","ETH","BNB","XRP","SOL","AAVE","ADA","AERO","ALGO","APT","ARB",
    "ASTER","ATOM","AVAX","AXS","BCH","BERA","BONK","BRETT","BSV",
    "CAKE","CHZ","CRO","CRV","DASH","DATA","DEEP","DEXE","DOGE","DOT",
    "DYDX","EIGEN","ENA","ENS","ETC","FARTCOIN","FET","FIL","FLOKI",
    "GALA","GRAM","GRASS","GRT","HBAR","HYPE","ICP","IMX","INJ","IOTA",
    "JASMY","JTO","JUP","KAIA","KAITO","KAS","LDO","LINEA","LINK","LTC",
    "MANA","MNT","MORPHO","MOVE","NEAR","ONDO","OP","ORDI","PENDLE",
    "PENGU","PEPE","PNUT","POL","POPCAT","PUMP","PYTH","QNT","RAY",
    "RENDER","RUNE","S","SAND","SEI","SHIB","SKY","STRK","STX","SUI",
    "TAO","TIA","TRUMP","TRX","TURBO","UNI","VET","VIRTUAL","WAL",
    "WIF","WLD","XLM","XMR","XTZ","ZEC","ZRO","0G"
]

# === V12 ПАРАМЕТРЫ ===
# ВАЖНО: критичные V12 параметры захардкожены в коде (без env var override),
# чтобы старые env vars на Render НЕ переопределили новые настройки V12.
# Если нужно изменить — правь прямо в этом файле.

# --- Нейтральные параметры (можно через env vars) ---
RISK_USD = float(os.environ.get("PROPHET_RISK_USD", "100"))
LEVERAGE = int(os.environ.get("PROPHET_LEVERAGE", "5"))
SL_MULT = float(os.environ.get("PROPHET_SL_MULT", "2.0"))
MAX_HOLD = int(os.environ.get("PROPHET_MAX_HOLD_HOURS", "48")) * 3600
MAX_MARGIN = float(os.environ.get("PROPHET_MAX_MARGIN", "3000"))
CLEANER_ENABLED = os.environ.get("PROPHET_CLEANER", "1") == "1"
CLEANER_GRACE_HOURS = int(os.environ.get("PROPHET_CLEANER_GRACE_HOURS", "2"))

# --- V12 СПЕЦИФИЧНЫЕ (захардкожены, env vars игнорируются) ---
# V12: динамический TP — рассчитывается в analyze() на основе ADX
TP_MULT_STRONG = 3.0   # ADX>=25 (сильный тренд) — TP дальше
TP_MULT_WEAK = 2.5     # ADX<25  (слабый тренд)   — TP ближе
# V12: пороги скора
MIN_SCORE = 60         # V12: 60 (V8 было 70) — было env, теперь хардкод
AUTO_SCORE = 60        # V12: 60 (V8 было 90) — было env, теперь хардкод
# V12: максимум одновременных позиций
MAX_OPEN = 5           # V12-max5: 5 (V8 было 2, V12-max3 было 3)
# V12-max5: ПИРАМИДИНГ — новая позиция в сторону только если предыдущие в плюсе
# - 1-я позиция: всегда можно открыть
# - 2-я, 3-я, 4-я, 5-я: только если ВСЕ предыдущие в этой стороне в плюсе
MAX_SAME_SIDE = 5      # до 5 LONG или 5 SHORT (при условии пирамидинга)
# V12: per-side daily stop — блокирует только убыточную сторону
# Если LONG набрал -$300 за день → блокируем LONG до завтра, SHORT остаётся работать
SIDE_DAILY_STOP_USD = 300.0  # 3 убытка × $100 = $300, потом блок стороны
# V12 окна сканов (15 часов, без 7/13/16 — убыточные в бэктестах)
AUTO_HOURS_MSK = [5, 6, 8, 9, 10, 11, 12, 14, 15, 17, 18, 19, 20, 21, 22]
SCAN_MINUTE = 15       # V12: сканы строго в :15 МСК (не пересекается с 4H close в :00)
CONFIRM_WAIT_SEC = 900 # V12: ждём 15 минут для подтверждения сигнала (одна 15m свеча)
SCAN_INTERVAL = 3600    # только для reference; реальный цикл управляется AUTO_HOURS_MSK + SCAN_MINUTE

_data_dir = os.environ.get("BOT_DATA_DIR", "/tmp")
try: os.makedirs(_data_dir, exist_ok=True)
except: pass
STATE_FILE = f"{_data_dir}/prophet_state.json"
TRACKED_FILE = f"{_data_dir}/prophet_tracked.json"

_session = requests.Session()
_lock = threading.Lock()
_tracked_lock = threading.Lock()
_candle_cache = {}
_cache_ts = 0
_pending_signals_lock = threading.Lock()  # V12: lock для pending confirmation

_state = {
    "scan_mode": "off",  # off / single / continuous / auto
    "last_scan_ts": 0,
    "last_signal_ts": 0,
    "pending_signal": None,  # ждёт ответа по кнопке (старый flow, для single)
    "trades_today": 0,
    "trades_today_date": "",
    "long_cooldown_until": 0,    # V12: cooldown после убыточного LONG (24h)
    "short_cooldown_until": 0,   # V12: cooldown после убыточного SHORT (24h)
    "symbol_blacklist": {},     # V12: {sym: ts_until}
    # V12-max5: per-side daily PnL (сброс по MSK-дню)
    "long_today_pnl": 0.0,       # кумулятивный PnL LONG за сегодня
    "short_today_pnl": 0.0,      # кумулятивный PnL SHORT за сегодня
    "pnl_date": "",              # для сброса в начале дня
}
_stats = {"scans": 0, "signals": 0, "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0}


def _now(): return time.time()
def _today(): return dt.datetime.now(MSK).strftime("%Y-%m-%d")
def _save():
    try:
        with open(STATE_FILE, "w") as f: json.dump(_state, f)
    except: pass

def _reset_daily_pnl_if_needed():
    """V12-max5: сброс per-side daily PnL при смене MSK-дня."""
    today = _today()
    if _state.get("pnl_date") != today:
        _state["pnl_date"] = today
        _state["long_today_pnl"] = 0.0
        _state["short_today_pnl"] = 0.0
        _save()

def _side_blocked(direction: str) -> bool:
    """V12-max5: проверить, заблокирована ли сторона из-за daily stop."""
    _reset_daily_pnl_if_needed()
    if direction == "LONG":
        return _state.get("long_today_pnl", 0.0) <= -SIDE_DAILY_STOP_USD
    else:
        return _state.get("short_today_pnl", 0.0) <= -SIDE_DAILY_STOP_USD

def _update_side_pnl(direction: str, pnl: float):
    """V12-max5: добавить PnL к дневному итогу по стороне."""
    _reset_daily_pnl_if_needed()
    if direction in ("LONG", "long"):
        old = _state.get("long_today_pnl", 0.0)
        _state["long_today_pnl"] = old + pnl
    else:
        old = _state.get("short_today_pnl", 0.0)
        _state["short_today_pnl"] = old + pnl
    _save()

def _fetch_current_price_for_sym(sym: str):
    """Получить текущую рыночную цену для символа."""
    try:
        r = _session.get(f"{GATE_API}/ticker", params={"currency_pair": f"{sym}_USDT"}, timeout=10)
        if r.ok:
            d = r.json()
            return float(d.get("last", 0))
    except: pass
    return None

def _can_pyramid(direction: str, tracked: dict) -> tuple:
    """
    V12-max5: ПИРАМИДИНГ — можно ли открыть новую позицию в эту сторону?
    Правила:
      - 1-я позиция в сторону: ВСЕГДА можно (если есть свободный слот)
      - 2-я, 3-я, 4-я, 5-я: только если ВСЕ предыдущие позиции в этой стороне В ПЛЮСЕ
        (по текущей рыночной цене)
    Возвращает (can_open: bool, reason: str)
    """
    side_long = direction in ("LONG", "long")
    # Считаем сколько позиций в этой стороне уже открыто
    side_positions = []
    for pid, rec in tracked.items():
        if rec.get("status") != "open": continue
        if pid.startswith("pending_"): continue
        rec_dir = rec.get("direction", rec.get("dir", "long"))
        rec_is_long = rec_dir in ("LONG", "long")
        if rec_is_long == side_long:
            side_positions.append(rec)

    n_side = len(side_positions)
    # Если в стороне ещё нет позиций — всегда можно (первая)
    if n_side == 0:
        return True, "первая позиция в сторону"

    # Для последующих — проверяем что ВСЕ предыдущие в плюсе
    profitable = 0
    losing = 0
    for rec in side_positions:
        sym = rec.get("sym")
        entry = rec.get("entry", 0)
        if not sym or not entry:
            continue
        cur_price = _fetch_current_price_for_sym(sym)
        if not cur_price:
            # Не можем проверить — считаем что не в плюсе (безопасно)
            losing += 1
            continue
        if side_long:
            # LONG: прибыль если cur > entry
            pnl_pct = (cur_price - entry) / entry * 100
        else:
            # SHORT: прибыль если cur < entry
            pnl_pct = (entry - cur_price) / entry * 100
        if pnl_pct > 0.1:  # мини-порог 0.1% чтобы избежать шума
            profitable += 1
        else:
            losing += 1

    if losing > 0:
        return False, f"в {direction} уже {n_side} позиций, {losing} в убытке — пирамидинг запрещён"

    return True, f"в {direction} {n_side} позиций, все в плюсе — пирамидинг разрешён"

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

# === INDICATORS ===

def _sma(v, p):
    return statistics.mean(v[-p:]) if len(v) >= p else None

def _ema(v, p):
    if len(v) < p: return None
    k = 2/(p+1); e = statistics.mean(v[:p])
    for x in v[p:]: e = x*k + e*(1-k)
    return e

def _rsi(c, p=14):
    if len(c) < p+1: return 50
    g, l = [], []
    for i in range(-p, 0):
        ch = c[i]["close"] - c[i-1]["close"]
        g.append(max(0, ch)); l.append(max(0, -ch))
    ag = statistics.mean(g) if g else 0
    al = statistics.mean(l) if l else 0.001
    return 100 - 100/(1 + (ag/al if al > 0 else 100))

def _atr(c, p=14):
    if len(c) < 2: return 0
    trs = [max(c[i]["high"]-c[i]["low"], abs(c[i]["high"]-c[i-1]["close"]), abs(c[i]["low"]-c[i-1]["close"])) for i in range(1, len(c))]
    return statistics.mean(trs[-p:]) if len(trs) >= p else statistics.mean(trs)

def _adx(c, p=14):
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

def _dc(c, p=20):
    if len(c) < p+1: return None, None, None
    r = c[-(p+1):-1]
    u = max(x["high"] for x in r); l = min(x["low"] for x in r)
    return u, l, (u+l)/2

def _bb(c, p=20, ns=2):
    if len(c) < p: return None, 0, False
    cl = [x["close"] for x in c[-p:]]
    m = statistics.mean(cl); s = statistics.stdev(cl) if len(cl) > 1 else 0
    bw = (2*ns*s/m*100) if m > 0 else 0
    exp = False
    if len(c) > p*2:
        pcl = [x["close"] for x in c[-(p*2):-p]]
        ps = statistics.stdev(pcl) if len(pcl) > 1 else 0
        exp = s > ps
    return m+ns*s, bw, exp

def _obv(c):
    if not c: return 0, 0
    o = 0; vals = []
    for i in range(1, len(c)):
        if c[i]["close"] > c[i-1]["close"]: o += c[i]["vol"]
        elif c[i]["close"] < c[i-1]["close"]: o -= c[i]["vol"]
        vals.append(o)
    sm = statistics.mean(vals[-20:]) if len(vals) >= 20 else (statistics.mean(vals) if vals else 0)
    return o, sm

# === CANDLE FETCHING ===

def _fetch(sym, iv, start_ts, end_ts):
    key = (sym, iv, start_ts, end_ts)
    if key in _candle_cache: return _candle_cache[key]
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
    _candle_cache[key] = all_c
    return all_c

def _btc_regime(at_ts):
    btc = _fetch("BTC", "1d", at_ts - 90*86400, at_ts + 86400)
    bc = [c for c in btc if c["ts"] <= at_ts]
    if len(bc) < 50: return 0
    sma = _sma([c["close"] for c in bc], 50)
    if not sma: return 0
    return +1 if bc[-1]["close"] > sma else -1

# === V12 DYNAMIC TP ===

def _get_tp_mult(adx: float) -> float:
    """V12: ADX<25 → TP=2.5 (слабый тренд), ADX≥25 → TP=3.0 (сильный тренд)."""
    return TP_MULT_WEAK if adx < 25 else TP_MULT_STRONG

# === ANALYSIS (V12: LONG + SHORT + Dynamic TP) ===

def analyze(sym, scan_ts, c4h, c1h, c15m, btc_reg, scan_dt):
    c4 = [c for c in c4h if c["ts"] <= scan_ts][-100:]
    if len(c4) < 60: return None
    last = c4[-1]; price = last["close"]
    atr = _atr(c4, 14); atr_pct = (atr/price)*100 if price > 0 else 0
    rsi = _rsi(c4, 14)
    adx, pdi, mdi = _adx(c4, 14)
    dc_u, dc_l, dc_m = _dc(c4, 20)
    sma50 = _sma([c["close"] for c in c4], 50)
    if not sma50: return None
    _, bw, bb_exp = _bb(c4, 20)
    obv, obv_sm = _obv(c4)

    # HARD GATES (общие)
    if btc_reg != +1: return None
    if atr_pct < 2.0 or atr_pct > 3.5: return None
    if rsi > 80 or rsi < 20: return None
    lcp = abs(last["close"]-last["open"])/last["open"]*100 if last["open"] > 0 else 0
    if lcp > 6: return None

    # MULTI-TF
    c1 = [c for c in c1h if c["ts"] <= scan_ts][-100:]
    d1 = "NEUTRAL"
    if len(c1) >= 20:
        e20 = _ema([c["close"] for c in c1], 20)
        if e20 and price > e20*1.005: d1 = "LONG"
        elif e20 and price < e20*0.995: d1 = "SHORT"

    c15 = [c for c in c15m if c["ts"] <= scan_ts][-50:]
    d15 = "NEUTRAL"
    if len(c15) >= 3:
        g = sum(1 for c in c15[-3:] if c["close"] > c["open"])
        d15 = "LONG" if g >= 2 else ("SHORT" if g <= 1 else "NEUTRAL")

    # V12: LONG если price > sma50*1.02 and rsi > 50
    #      SHORT если price < sma50*0.98 and rsi < 50
    d4 = "NEUTRAL"
    if price > sma50*1.02 and rsi > 50:
        d4 = "LONG"
    elif price < sma50*0.98 and rsi < 50:
        d4 = "SHORT"

    # V12: confluence в ОБЕ стороны
    long_agree = sum(1 for d in [d4, d1, d15] if d == "LONG")
    short_agree = sum(1 for d in [d4, d1, d15] if d == "SHORT")

    if long_agree >= 2 and d4 == "LONG":
        direction = "LONG"; agree = long_agree
    elif short_agree >= 2 and d4 == "SHORT":
        direction = "SHORT"; agree = short_agree
    else:
        return None

    is_long = direction == "LONG"

    # SCORING
    score = 0; details = []

    # 1. Confluence (0-40)
    if agree == 3: score += 40; details.append("3/3 TF")
    elif agree == 2: score += 25; details.append("2/3 TF")

    # 2. ATR (0-15)
    if 2.0 <= atr_pct <= 2.5: score += 15; details.append(f"ATR{atr_pct:.1f}✓")
    elif 2.5 < atr_pct <= 3.0: score += 10; details.append(f"ATR{atr_pct:.1f}")

    # 3. ADX (0-10)
    if 30 <= adx <= 40: score += 10; details.append(f"ADX{adx:.0f}✓")
    elif 20 <= adx < 30: score += 5; details.append(f"ADX{adx:.0f}")
    else: details.append(f"ADX{adx:.0f}")

    # 4. RSI (0-10) — V12: реверсивный для SHORT
    if is_long:
        if 60 <= rsi <= 80: score += 10; details.append(f"RSI{rsi:.0f}✓")
        elif 50 <= rsi < 60: score += 5; details.append(f"RSI{rsi:.0f}")
        else: details.append(f"RSI{rsi:.0f}")
    else:
        if 20 <= rsi <= 40: score += 10; details.append(f"RSI{rsi:.0f}✓")
        elif 40 < rsi <= 50: score += 5; details.append(f"RSI{rsi:.0f}")
        else: details.append(f"RSI{rsi:.0f}")

    # 5. DAY (-10 to +15)
    wd = scan_dt.weekday()
    wdn = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"][wd]
    if wd == 4: score += 15; details.append("Пт✓")
    elif wd == 5: score += 10; details.append("Сб✓")
    elif wd == 6: score += 5; details.append("Вс")
    elif wd == 3: score += 5; details.append("Чт")
    elif wd in (1, 2): score -= 10; details.append(f"{wdn}штраф")
    else: details.append("Пн")

    # 6. HOUR (0-10) — V12: бонусы за все часы скана
    hm = scan_dt.hour
    if hm in (11, 15): score += 10; details.append(f"{hm}:00✓")
    elif hm in (7, 23): score += 5; details.append(f"{hm}:00")
    else: details.append(f"{hm}:00")

    # Volume
    if len(c4) >= 20:
        av = statistics.mean([c["vol"] for c in c4[-20:]])
        vr = last["vol"]/av if av > 0 else 1
    else: vr = 1
    if vr > 1.5: score += 5; details.append(f"V×{vr:.1f}")

    # BB
    if bb_exp: score += 5; details.append("BB↑")

    # PENALTIES
    if agree < 3:
        score = min(score, 85); details.append("cap85")
    if atr_pct > 3.0:
        score -= 15; details.append("ATRштраф")

    score = max(0, min(score, 100))
    if score < MIN_SCORE: return None

    # V12: динамический TP
    tp_mult = _get_tp_mult(adx)
    sp = SL_MULT * atr_pct
    tp = tp_mult * atr_pct

    # V12: mirror TP/SL для SHORT
    if is_long:
        stop_price = price*(1-sp/100)
        tp_price = price*(1+tp/100)
    else:
        stop_price = price*(1+sp/100)
        tp_price = price*(1-tp/100)

    return {
        "symbol": sym, "price": price, "score": score, "direction": direction,
        "atr_pct": atr_pct, "rsi": rsi, "adx": adx, "agree": agree,
        "vol_ratio": vr, "bb_exp": bb_exp, "weekday": wdn, "hour": hm,
        "stop_pct": sp, "tp_pct": tp, "atr": atr, "tp_mult": tp_mult,
        "stop_price": stop_price, "tp_price": tp_price,
        "details": " | ".join(details),
    }

# === SCAN ===

def do_scan(scan_ts=None):
    """Запускает скан. Возвращает лучший сетап или None."""
    if not B: return None
    if scan_ts is None: scan_ts = int(_now())
    scan_dt = dt.datetime.fromtimestamp(scan_ts, tz=MSK)

    btc_reg = _btc_regime(scan_ts)
    if btc_reg != +1:
        return None

    # V12: clean expired blacklists
    now_ts = int(_now())
    bl = _state.get("symbol_blacklist", {})
    expired = [s for s, t in bl.items() if t < now_ts]
    for s in expired:
        bl.pop(s, None)
    if expired:
        _state["symbol_blacklist"] = bl
        _save()

    # Предзагрузка свечей (кешируем на 5 минут)
    global _cache_ts
    if _now() - _cache_ts > 300:
        _candle_cache.clear()
        _cache_ts = _now()

    tracked = _load_tracked()
    open_syms = {v.get("sym") for v in tracked.values() if v.get("status") == "open"}

    results = []
    for sym in PAIRS:
        if sym in open_syms: continue
        # V12: symbol blacklist
        if sym in _state.get("symbol_blacklist", {}): continue
        try:
            c4h = _fetch(sym, "4h", scan_ts - 30*86400, scan_ts + 3600)
            c1h = _fetch(sym, "1h", scan_ts - 7*86400, scan_ts + 3600)
            c15m = _fetch(sym, "15m", scan_ts - 3*86400, scan_ts + 3600)
            if not c4h or not c1h or not c15m: continue
            r = analyze(sym, scan_ts, c4h, c1h, c15m, btc_reg, scan_dt)
            if r is None: continue
            # V12: direction cooldown (24h после убытка)
            if r["direction"] == "LONG":
                if now_ts < _state.get("long_cooldown_until", 0): continue
            else:
                if now_ts < _state.get("short_cooldown_until", 0): continue
            # V12-max5: per-side daily stop — блокируем убыточную сторону
            if _side_blocked(r["direction"]):
                side_pnl = _state.get("long_today_pnl" if r["direction"]=="LONG" else "short_today_pnl", 0.0)
                print(f"[prophet V12-max5] {r['direction']} blocked: today PnL ${side_pnl:.0f} ≤ -${SIDE_DAILY_STOP_USD:.0f}")
                continue
            # V12-max5: ПИРАМИДИНГ — не открываем 2-ю позицию в сторону если 1-я в минусе
            # (но 1-я в сторону — всегда можно)
            can_pyramid, pyramid_reason = _can_pyramid(r["direction"], tracked)
            if not can_pyramid:
                print(f"[prophet V12-max5] {r['symbol']} {r['direction']}: пирамидинг запрещён — {pyramid_reason}")
                continue
            results.append(r)
        except Exception as e:
            print(f"[prophet] {sym}: {e}")

    if not results: return None
    results.sort(key=lambda x: -x["score"])
    return results[0]

# === SIGNAL (V12: показывает направление) ===

def send_signal(setup):
    """Отправляет сигнал в Telegram с inline кнопками."""
    if not B: return
    _state["pending_signal"] = setup
    _state["last_signal_ts"] = _now()
    _save()

    s = setup
    is_long = s.get("direction", "LONG") == "LONG"
    emoji = "🟢 LONG" if is_long else "🔴 SHORT"
    tp_mult = s.get("tp_mult", 3.0)

    msg = (
        f"🔮 <b>ПРОРОК V12 — СИГНАЛ</b>\n\n"
        f"🎯 {s['symbol']}/USDT — {emoji}\n"
        f"Score: <b>{s['score']:.0f}</b> | TP×ATR: {tp_mult:.1f}\n\n"
        f"📊 Анализ:\n"
        f"   {s['details']}\n\n"
        f"📈 Сделка:\n"
        f"   Вход: ${s['price']:.6g}\n"
        f"   Стоп: ${s['stop_price']:.6g} ({s['stop_pct']:.1f}%)\n"
        f"   Тейк: ${s['tp_price']:.6g} ({s['tp_pct']:.1f}%)\n"
        f"   RR: 1:{s['tp_pct']/s['stop_pct']:.1f}\n"
        f"   Риск: ${RISK_USD} | Плечо: {LEVERAGE}x\n\n"
        f"⏱️ Сигнал действителен 15 минут"
    )

    # Inline keyboard
    try:
        url = f"https://api.telegram.org/bot{B.TELEGRAM_TOKEN}/sendMessage"
        data = {
            "chat_id": B.CHAT_ID,
            "text": msg,
            "parse_mode": "HTML",
            "reply_markup": json.dumps({"inline_keyboard": [[
                {"text": "✅ ВОЙТИ", "callback_data": "prophet_enter"},
                {"text": "⏭️ ПРОПУСТИТЬ", "callback_data": "prophet_skip"},
            ]]}),
        }
        requests.post(url, json=data, timeout=10)
    except Exception as e:
        print(f"[prophet] send_signal error: {e}")
        B.send_telegram(msg)

def on_callback(data):
    """Обработка нажатия кнопки."""
    if not _state.get("pending_signal"): return
    sig = _state["pending_signal"]

    if data == "prophet_enter":
        _state["pending_signal"] = None
        _save()
        execute_trade(sig)
    elif data == "prophet_skip":
        _state["pending_signal"] = None
        _save()
        if B: B.send_telegram("⏭️ Пропущено. Жду следующий скан.")

# === V12 CONFIRMATION (15-min confirmation candle) ===

def _confirm_signal(setup):
    """
    V12: Подтверждение сигнала через 15 минут.
    Перепроверяем последнюю закрытую 15m свечу:
    - LONG: close > open (бычья)
    - SHORT: close < open (медвежья)
    Возвращает True если сигнал подтверждён.
    """
    sym = setup["symbol"]
    direction = setup.get("direction", "LONG")
    now_ts = int(_now())

    try:
        c15m = _fetch(sym, "15m", now_ts - 2*3600, now_ts + 60)
        if not c15m: return False
        # Последняя полностью закрытая свеча (не текущая формирующаяся)
        closed = [c for c in c15m if c["ts"] <= now_ts - 60]
        if len(closed) < 2: return False
        last_c = closed[-1]
        prev_c = closed[-2]
        is_long = direction == "LONG"

        # 1. Свеча подтверждает направление
        if is_long:
            candle_ok = last_c["close"] > last_c["open"]
        else:
            candle_ok = last_c["close"] < last_c["open"]

        # 2. Цена всё ещё близко к точке входа (не ушла больше чем на 1 ATR%)
        price_now = last_c["close"]
        entry = setup["price"]
        deviation = abs(price_now - entry) / entry * 100
        price_ok = deviation < setup.get("atr_pct", 2.5)

        # 3. Свеча не против направления (close движется в нужную сторону)
        if is_long:
            momentum_ok = last_c["close"] >= prev_c["close"]
        else:
            momentum_ok = last_c["close"] <= prev_c["close"]

        confirmed = candle_ok and price_ok and momentum_ok
        print(f"[prophet] confirm {sym} {direction}: candle={candle_ok} price_dev={deviation:.2f}% momentum={momentum_ok} → {confirmed}")
        return confirmed
    except Exception as e:
        print(f"[prophet] confirm error {sym}: {e}")
        return False

def _confirmation_worker(setup, fire_ts: int):
    """V12: воркер — ждёт 15 минут, проверяет подтверждение, исполняет если ОК."""
    sym = setup["symbol"]
    direction = setup.get("direction", "LONG")
    emoji_dir = "🟢 LONG" if direction == "LONG" else "🔴 SHORT"

    # Ждём до времени подтверждения
    wait_sec = CONFIRM_WAIT_SEC - (int(_now()) - fire_ts)
    if wait_sec > 0:
        time.sleep(wait_sec)

    try:
        # Перечитываем tracked — слот мог быть занят другим сигналом
        tracked = _load_tracked()
        open_count = sum(1 for v in tracked.values() if v.get("status") == "open")
        if open_count >= MAX_OPEN:
            if B: B.send_telegram(f"⏭️ {sym} {emoji_dir}: подтверждение пришло, но слоты заняты ({open_count}/{MAX_OPEN})")
            return

        # V12-max5: повторная проверка пирамидинга перед самым входом
        # (за 15 минут ожидания предыдущая позиция могла уйти в минус)
        can_pyramid, pyramid_reason = _can_pyramid(direction, tracked)
        if not can_pyramid:
            if B: B.send_telegram(
                f"⏭️ {sym} {emoji_dir}: подтверждение пришло, но пирамидинг запрещён\n"
                f"Причина: {pyramid_reason}"
            )
            return

        # Проверяем подтверждение свечи
        if _confirm_signal(setup):
            # Обновляем цену входа на текущую
            setup["price"] = _fetch_current_price(sym) or setup["price"]
            if B: B.send_telegram(
                f"✅ <b>ПОДТВЕРЖДЕНО</b> — {sym} {emoji_dir}\n"
                f"Вход по рынку: ${setup['price']:.6g}"
            )
            execute_trade(setup)
        else:
            if B: B.send_telegram(
                f"❌ <b>НЕ ПОДТВЕРЖДЕНО</b> — {sym} {emoji_dir}\n"
                f"15m свеча не подтвердила направление. Сигнал отменён."
            )
    except Exception as e:
        print(f"[prophet] confirmation_worker error: {e}")
        if B: B.send_telegram(f"⚠️ Prophet: ошибка подтверждения {sym}: {e}")

def _fetch_current_price(sym: str):
    """Получает текущую рыночную цену."""
    try:
        r = _session.get(f"{GATE_API}/ticker", params={"currency_pair": f"{sym}_USDT"}, timeout=10)
        if r.ok:
            d = r.json()
            return float(d.get("last", 0))
    except: pass
    return None

# === TRADE EXECUTION (V12: LONG + SHORT) ===

def execute_trade(setup):
    if not B or not hasattr(B, "EXECUTOR"): return
    executor = B.EXECUTOR
    if not executor: return

    sym = setup["symbol"]
    direction = setup.get("direction", "LONG")
    side = "long" if direction == "LONG" else "short"
    entry = setup["price"]
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
        "max_pos_usd": pos_size, "max_same_side": MAX_SAME_SIDE,  # V12-max5: 3 (было 1)
        "strategy": STRATEGY_TAG, "leverage_override": LEVERAGE, "ext_atr": 0.0,
    }

    try:
        score = int(setup["score"])
        executor.on_signal(sig, score)
        emoji_dir = "🟢 LONG" if side == "long" else "🔴 SHORT"
        if B:
            B.send_telegram(
                f"✅ <b>ПРОРОК V12: {sym} {emoji_dir} открыт</b>\n"
                f"Вход: ${entry:.6g}\n"
                f"Стоп: ${stop:.6g} ({sp:.1f}%)\n"
                f"Тейк: ${tp:.6g} ({setup['tp_pct']:.1f}%)\n"
                f"TP×ATR: {setup.get('tp_mult', 3.0):.1f}\n"
                f"Маржа: ${margin:.0f} × {LEVERAGE}x\n"
                f"Score: {setup['score']:.0f}"
            )
        _state["trades_today"] += 1
        _stats["trades"] += 1
        _save()
        # Track
        with _tracked_lock:
            tr = _load_tracked()
            tr[f"pending_{sym}_{int(_now())}"] = {
                "sym": sym, "side": side, "dir": side, "entry": entry,
                "stop": stop, "tp": tp, "opened_ts": _now(),
                "strategy": STRATEGY_TAG, "status": "open",
                "direction": direction,
            }
            _save_tracked(tr)
    except Exception as e:
        if B: B.send_telegram(f"⚠️ Prophet {sym}: ошибка — {e}")

# === POSITION MANAGEMENT ===

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
                del tracked[pid]; changed = True
            continue

        if pid not in live_ids:
            pnl = 0
            try:
                m = executor._mk.get(rec["sym"]) if executor._mk else None
                if m:
                    asset = str(m.get("id","")) or rec["sym"]
                    data = executor.client.orders_history(executor.account_id, asset, 50)
                    for o in UE._as_list(data):
                        if isinstance(o, dict):
                            ts_str = o.get("closedAt") or o.get("filledAt") or o.get("updatedAt") or o.get("createdAt")
                            if ts_str:
                                try:
                                    if isinstance(ts_str, str) and "T" in ts_str:
                                        ots = dt.datetime.fromisoformat(ts_str.replace("Z","+00:00")).timestamp()
                                    elif float(ts_str) > 1e11: ots = float(ts_str)/1000
                                    else: ots = float(ts_str)
                                    if ots >= rec.get("opened_ts", 0) - 60:
                                        pv = o.get("realizedPnl")
                                        if pv is not None:
                                            t = str(pv).strip()
                                            if t and t not in ("None","null"):
                                                try: pnl += float(t)
                                                except: pass
                                except: pass
            except: pass

            _stats["pnl"] += pnl
            # V12-max5: обновляем per-side daily PnL
            direction_field = rec.get("direction", rec.get("dir", "long"))
            _update_side_pnl(direction_field, pnl)
            side_pnl_now = _state.get("long_today_pnl" if direction_field in ("LONG","long") else "short_today_pnl", 0.0)
            # V12: cooldown после убытка
            if pnl < 0:
                if direction_field in ("LONG", "long"):
                    _state["long_cooldown_until"] = int(_now()) + 24*3600
                else:
                    _state["short_cooldown_until"] = int(_now()) + 24*3600
                # V12: blacklist символа на 7 дней
                bl = _state.get("symbol_blacklist", {})
                bl[rec["sym"]] = int(_now()) + 7*24*3600
                _state["symbol_blacklist"] = bl
                _save()
            # V12-max5: уведомление если сторона упёрлась в daily stop
            if side_pnl_now <= -SIDE_DAILY_STOP_USD:
                side_name = "LONG" if direction_field in ("LONG","long") else "SHORT"
                if B: B.send_telegram(
                    f"🚫 <b>V12-max5: {side_name} заблокирован до завтра</b>\n"
                    f"Сегодняшний PnL по {side_name}: ${side_pnl_now:+.0f}\n"
                    f"Лимит стороны: -${SIDE_DAILY_STOP_USD:.0f}\n"
                    f"Обратная сторона продолжает работать."
                )
            if pnl > 0: _stats["wins"] += 1
            else: _stats["losses"] += 1
            del tracked[pid]; changed = True
        else:
            hold = now - rec.get("opened_ts", now)
            if hold >= MAX_HOLD:
                pos = next((p for p in live if UE._pos_id(p) == pid), None)
                if pos:
                    mid = UE._pos_market(pos)
                    if mid:
                        # V12: dir из rec
                        dir_field = rec.get("dir", rec.get("direction", "long"))
                        if dir_field in ("SHORT", "short"): dir_field = "short"
                        else: dir_field = "long"
                        info = {"id": pid, "mid": mid, "dir": dir_field}
                        try:
                            executor.close_position(info, reason=f"PROPHET_TIMEOUT_{MAX_HOLD//3600}h")
                            if B: B.send_telegram(f"⏱️ Prophet {rec['sym']}: safety-net {MAX_HOLD//3600}ч")
                        except: pass

    if changed:
        with _tracked_lock: _save_tracked(tracked)

# === SCAN LOOPS ===

_scanner_thread = None

def _scanner_loop():
    """V12: Главный цикл — сканы строго в HH:15 МСК каждый час из AUTO_HOURS_MSK."""
    print(f"[prophet V12] Scanner запущен. Часы скана: {AUTO_HOURS_MSK}, мин={SCAN_MINUTE}")
    while True:
        try:
            mode = _state.get("scan_mode", "off")
            if mode == "off":
                time.sleep(60); continue

            now_dt = dt.datetime.now(MSK)
            now_ts = int(_now())

            # V12: скан строго в HH:15 (окно 10 минут, т.е. :10-:20)
            # Проверяем что час в AUTO_HOURS_MSK и минута в окне :10-:20
            if now_dt.hour not in AUTO_HOURS_MSK:
                time.sleep(60); continue
            if not (10 <= now_dt.minute <= 20):
                time.sleep(30); continue

            # Уже сканировали в этот час?
            last_scan = _state.get("last_scan_ts", 0)
            last_scan_dt = dt.datetime.fromtimestamp(last_scan, tz=MSK)
            if (last_scan_dt.year == now_dt.year and last_scan_dt.month == now_dt.month
                and last_scan_dt.day == now_dt.day and last_scan_dt.hour == now_dt.hour):
                time.sleep(60); continue

            # Лимит сделок
            if _state.get("trades_today_date") != _today():
                _state["trades_today"] = 0
                _state["trades_today_date"] = _today()

            # === CLEANER: перед каждым сканом проверяем зависшие позиции ===
            do_clean = (mode == "auto") or (mode == "continuous" and _now() - _state.get("last_clean_ts", 0) > 6*3600)
            if do_clean:
                try:
                    closed_n, _ = run_cleaner(reason=f"PRE_SCAN_{mode.upper()}")
                    _state["last_clean_ts"] = _now()
                    _save()
                    if closed_n > 0 and B:
                        tracked = _load_tracked()
                except Exception as e:
                    print(f"[prophet] cleaner error in scanner loop: {e}")

            # V12-max5: AUTO-SYNC — убираем orphan'ы перед сканом
            # (позиция уже закрыта на Upscale, но запись осталась в tracked)
            try:
                if B and hasattr(B, "EXECUTOR") and B.EXECUTOR and B.EXECUTOR.client:
                    _auto_sync_orphans()
            except Exception as e:
                print(f"[prophet V12-max5] auto-sync error: {e}")

            # Уже есть открытая позиция?
            tracked = _load_tracked()
            open_count = sum(1 for v in tracked.values() if v.get("status") == "open")
            if open_count >= MAX_OPEN:
                time.sleep(60); continue

            # Сканируем
            _state["last_scan_ts"] = _now()
            _stats["scans"] += 1
            _save()

            scan_dt_str = now_dt.strftime("%H:%M")
            print(f"[prophet V12] скан в {scan_dt_str} МСК, режим={mode}, пар={len(PAIRS)}, открыто={open_count}/{MAX_OPEN}")

            setup = do_scan(now_ts)

            if setup:
                direction = setup.get("direction", "LONG")
                emoji_dir = "🟢 LONG" if direction == "LONG" else "🔴 SHORT"
                print(f"[prophet V12] скан {scan_dt_str}: лучший сигнал {setup['symbol']} {direction} score={setup['score']:.0f}")
                min_s = AUTO_SCORE if mode == "auto" else MIN_SCORE
                if setup["score"] >= min_s:
                    _stats["signals"] += 1

                    # V12: ПОДТВЕРЖДЕНИЕ ВХОДА
                    # В auto режиме — запускаем confirmation worker
                    # В continuous (ручном) — отправляем сигнал с кнопками как раньше
                    if mode == "auto":
                        if B:
                            B.send_telegram(
                                f"🔮 <b>V12 — сигнал найден</b>\n"
                                f"🎯 {setup['symbol']} {emoji_dir} score={setup['score']:.0f}\n"
                                f"⏱️ Жду подтверждения 15 мин (закрытие 15m свечи)..."
                            )
                        # Запускаем воркер подтверждения в отдельном потоке
                        fire_ts = int(_now())
                        t = threading.Thread(
                            target=_confirmation_worker,
                            args=(setup, fire_ts),
                            daemon=True,
                            name=f"prophet-confirm-{setup['symbol']}-{fire_ts}"
                        )
                        t.start()
                    else:
                        # Continuous (ручной режим): отправляем в Telegram с кнопками
                        print(f"[prophet V12] сигнал {setup['symbol']} score={setup['score']:.0f} → отправляю в Telegram")
                        send_signal(setup)
                else:
                    print(f"[prophet V12] сигнал {setup['symbol']} score={setup['score']:.0f} < порога {min_s} → пропуск")
                    if B:
                        threshold_str = f"авто ≥{AUTO_SCORE}" if mode == "auto" else f"ручной ≥{MIN_SCORE}"
                        mode_label = {"auto": "🤖 Авто", "continuous": "🔄 Непрерывный", "single": "🔍 Разовый"}.get(mode, mode)
                        B.send_telegram(
                            f"🔮 V12 Скан {scan_dt_str} МСК ({mode_label})\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"Проверено пар: {len(PAIRS)}\n"
                            f"Лучший: {setup['symbol']} {emoji_dir} score={setup['score']:.0f}\n"
                            f"⏸️ Ниже порога ({threshold_str}) — пропуск\n"
                            f"Открыто: {open_count}/{MAX_OPEN}"
                        )
            else:
                print(f"[prophet V12] скан {scan_dt_str}: сигналов не найдено")
                if B:
                    mode_label = {"auto": "🤖 Авто", "continuous": "🔄 Непрерывный", "single": "🔍 Разовый"}.get(mode, mode)
                    B.send_telegram(
                        f"🔮 V12 Скан {scan_dt_str} МСК ({mode_label})\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"Проверено пар: {len(PAIRS)}\n"
                        f"❌ Сигналов не найдено\n"
                        f"Открыто: {open_count}/{MAX_OPEN}"
                    )

            time.sleep(60)
        except Exception as e:
            print(f"[prophet V12] loop error: {e}")
            time.sleep(60)

# === COMMANDS ===

def cmd_status():
    # V12-max5: при запросе статуса — сначала синхронизируем tracked с Upscale
    # (молча убираем orphan'ы, чтобы статус показывал реальные позиции)
    sync_msg = None
    try:
        sync_msg = _auto_sync_orphans(verbose_telegram=False)
        # Если что-то убрано — уведомим в Telegram (даже не в verbose режиме)
        if sync_msg and ("SYNC:" in sync_msg and "убрано" in sync_msg):
            if B: B.send_telegram("🧹 " + sync_msg)
    except Exception as e:
        print(f"[prophet V12-max5] status auto-sync error: {e}")

    mode = _state.get("scan_mode", "off")
    mode_str = {"off": "🔴 Выкл",
                "continuous": "🔄 Непрерывный (каждый час в :15)",
                "auto": f"🤖 Авто (V12 часы: {AUTO_HOURS_MSK})"}.get(mode, mode)
    lines = ["", "━━━ 🔮 Пророк V12 ━━━"]
    lines.append(f"Режим: {mode_str}")
    lines.append(f"Сделок сегодня: {_state.get('trades_today', 0)}/{MAX_OPEN}")
    tracked = _load_tracked()
    open_n = sum(1 for v in tracked.values() if v.get("status") == "open")
    lines.append(f"Открыто: {open_n}")
    lines.append(f"Сканирований: {_stats['scans']}")
    lines.append(f"Сигналов: {_stats['signals']}")
    if _stats["trades"] > 0:
        wr = _stats["wins"]/_stats["trades"]*100
        lines.append(f"Сделок: {_stats['trades']} (WR {wr:.0f}%)")
        lines.append(f"PnL: ${_stats['pnl']:+.2f}")
    lines.append(f"\n📅 V12-max5 часы: {AUTO_HOURS_MSK} МСК, скан в :{SCAN_MINUTE:02d}")
    lines.append(f"👥 Пар: {len(PAIRS)} | MAX_OPEN={MAX_OPEN} | MAX_SAME_SIDE={MAX_SAME_SIDE}")
    lines.append(f"Порог: ручной ≥{MIN_SCORE} | авто ≥{AUTO_SCORE}")
    lines.append(f"TP: динамический (ADX<25 → {TP_MULT_WEAK}×ATR, ADX≥25 → {TP_MULT_STRONG}×ATR)")
    lines.append(f"SL={SL_MULT}×ATR | Hold={MAX_HOLD//3600}ч")
    lines.append(f"✅ Подтверждение входа: {CONFIRM_WAIT_SEC//60} мин (15m свеча)")
    lines.append(f"🛡️ Per-side daily stop: -${SIDE_DAILY_STOP_USD:.0f} (блокирует только сторону)")
    lines.append(f"🧹 Cleaner: {'вкл' if CLEANER_ENABLED else 'выкл'} (grace +{CLEANER_GRACE_HOURS}ч)")
    # V12-max5: per-side daily PnL
    _reset_daily_pnl_if_needed()
    long_pnl = _state.get("long_today_pnl", 0.0)
    short_pnl = _state.get("short_today_pnl", 0.0)
    long_block = "🚫" if long_pnl <= -SIDE_DAILY_STOP_USD else "✅"
    short_block = "🚫" if short_pnl <= -SIDE_DAILY_STOP_USD else "✅"
    # Считаем открытые позиции по сторонам
    tracked = _load_tracked()  # перечитываем для актуального count
    long_open = sum(1 for v in tracked.values() if v.get("status") == "open"
                    and v.get("direction", v.get("dir", "long")) in ("LONG", "long")
                    and not v.get("sym", "").startswith("pending_"))
    short_open = sum(1 for v in tracked.values() if v.get("status") == "open"
                     and v.get("direction", v.get("dir", "short")) in ("SHORT", "short")
                     and not v.get("sym", "").startswith("pending_"))
    lines.append(f"\n📊 Сегодня PnL:")
    lines.append(f"  🟢 LONG:  ${long_pnl:+.0f} / -${SIDE_DAILY_STOP_USD:.0f} {long_block} | открыто: {long_open}/{MAX_SAME_SIDE}")
    lines.append(f"  🔴 SHORT: ${short_pnl:+.0f} / -${SIDE_DAILY_STOP_USD:.0f} {short_block} | открыто: {short_open}/{MAX_SAME_SIDE}")
    lines.append(f"  📌 Пирамидинг: новые только если предыдущие в плюсе")
    # V12 cooldowns
    now_ts = int(_now())
    long_cd = _state.get("long_cooldown_until", 0)
    short_cd = _state.get("short_cooldown_until", 0)
    if long_cd > now_ts:
        lines.append(f"⏸️ LONG 24h cooldown: ещё {(long_cd-now_ts)//3600}ч")
    if short_cd > now_ts:
        lines.append(f"⏸️ SHORT 24h cooldown: ещё {(short_cd-now_ts)//3600}ч")
    bl = _state.get("symbol_blacklist", {})
    active_bl = {s: t for s, t in bl.items() if t > now_ts}
    if active_bl:
        lines.append(f"⛔ Blacklist: {len(active_bl)} пар (на 7д после убытка)")
    lines.append(f"\nКоманды:")
    lines.append(f"  /prophet_scan — один скан")
    lines.append(f"  /prophet_scan on — непрерывный (каждый час в :15)")
    lines.append(f"  /prophet_auto on — авто (V12 часы, с подтверждением)")
    lines.append(f"  /prophet_clean — cleaner зависших (по возрасту)")
    lines.append(f"  /prophet_sync — синхронизация tracked с Upscale (убрать orphan)")
    return "\n".join(lines)

def cmd_scan(arg=""):
    if arg.lower() == "on":
        # V12 fix: при переключении режимов сбрасываем pending_signal
        _state["scan_mode"] = "continuous"
        _state["pending_signal"] = None
        _save()
        return "🔄 V12 непрерывный скан включён (каждый час в :15 МСК). /prophet_scan off — остановить."
    elif arg.lower() == "off":
        _state["scan_mode"] = "off"
        _state["pending_signal"] = None
        _save()
        return "🔴 Непрерывный скан выключен."
    else:
        # Single scan
        def _run():
            try:
                setup = do_scan()
                if setup and setup["score"] >= MIN_SCORE:
                    _stats["signals"] += 1
                    send_signal(setup)
                elif B:
                    if setup:
                        direction = setup.get("direction", "LONG")
                        emoji = "🟢" if direction == "LONG" else "🔴"
                        B.send_telegram(f"🔮 V12 Скан завершён\nЛучший: {emoji} {setup['symbol']} {direction} score={setup['score']:.0f}\nНиже порога {MIN_SCORE} — пропуск")
                    else:
                        B.send_telegram("🔮 V12 Скан завершён\n❌ Сигналов не найдено")
            except Exception as e:
                if B: B.send_telegram(f"⚠️ /prophet_scan ошибка: {e}")
        threading.Thread(target=_run, daemon=True, name="prophet-single").start()
        return "🔍 Запускаю скан..."

def cmd_auto(arg=""):
    if arg.lower() == "on":
        # V12 fix: при переключении режимов сбрасываем висящий pending_signal от continuous
        # (чтобы старая кнопка "ВОЙТИ" от ручного режима не путала пользователя в auto)
        old_pending = _state.get("pending_signal")
        _state["scan_mode"] = "auto"
        _state["pending_signal"] = None
        _save()
        msg = f"🤖 V12 Авто-режим включён.\nЧасы скана: {AUTO_HOURS_MSK}\nМинута: :{SCAN_MINUTE:02d}\nПорог: ≥{AUTO_SCORE}\nПодтверждение: {CONFIRM_WAIT_SEC//60} мин (15m свеча, автоматически)"
        if old_pending:
            msg += f"\n\n⏭️ Старый сигнал от ручного режима ({old_pending['symbol']}) отменён — в auto режиме бот сам принимает решения."
        return msg
    elif arg.lower() == "off":
        _state["scan_mode"] = "off"
        _state["pending_signal"] = None
        _save()
        return "🔴 Авто-режим выключен."
    return "Использование: /prophet_auto on | /prophet_auto off"

def cmd_close():
    if not B or not hasattr(B, "EXECUTOR"): return "⚠️ Executor недоступен"
    executor = B.EXECUTOR
    if not executor or not executor.client: return "⚠️ Нет клиента"
    tracked = _load_tracked()
    open_recs = [(pid, v) for pid, v in tracked.items() if v.get("status") == "open" and not pid.startswith("pending_")]
    if not open_recs: return "📭 Нет открытых Prophet позиций"
    closed = 0
    for pid, rec in open_recs:
        try:
            executor._ensure_account()
            live = UE._as_list(executor.client.positions(executor.account_id))
            pos = next((p for p in live if UE._pos_id(p) == pid), None)
            if pos:
                mid = UE._pos_market(pos)
                if mid:
                    # V12: dir из rec
                    dir_field = rec.get("dir", rec.get("direction", "long"))
                    if dir_field in ("SHORT", "short"): dir_field = "short"
                    else: dir_field = "long"
                    info = {"id": pid, "mid": mid, "dir": dir_field}
                    res = executor.close_position(info, reason="PROPHET_MANUAL")
                    with _tracked_lock:
                        tr = _load_tracked()
                        if pid in tr: tr[pid]["status"] = "closed"; _save_tracked(tr)
                    closed += 1
                    if B: B.send_telegram(f"🛑 Prophet {rec['sym']}: закрыто ({res})")
        except Exception as e:
            print(f"[prophet] close error: {e}")
    return f"🛑 Закрыто: {closed}"

def cmd_clean():
    """Ручной запуск cleaner — закрыть зависшие позиции старше MAX_HOLD + GRACE."""
    closed, msg = run_cleaner(reason="MANUAL_CLEAN")
    return msg

def cmd_sync():
    """
    V12-max5: Принудительная синхронизация tracked.json с live позициями Upscale.
    Удаляет orphan-записи (есть в tracked, но нет в live) БЕЗ попытки закрыть —
    позиция уже закрыта на Upscale, нужно только убрать мусор из tracked.
    Решает проблему "Открыто: 2" когда реально открыта 1.
    """
    return _auto_sync_orphans(verbose_telegram=True)


def _auto_sync_orphans(verbose_telegram: bool = False) -> str:
    """
    V12-max5: Синхронизация tracked.json с live позициями Upscale.
    Убирает orphan-записи:
      - real orphan: pid не в live_ids → позиция закрыта на Upscale
      - pending orphan: pending_* старше 60 сек, нет соответствия в live → не открылась
    """
    if not B or not hasattr(B, "EXECUTOR"):
        return "⚠️ Executor недоступен"
    executor = B.EXECUTOR
    if not executor or not executor.client:
        return "⚠️ Нет клиента Upscale"

    try:
        executor._ensure_account()
        if not executor.account_id:
            return "⚠️ Нет account_id"
        live = UE._as_list(executor.client.positions(executor.account_id))
        live_ids = {UE._pos_id(p) for p in live if isinstance(p, dict)}
        # Также собираем symbol -> position для matching pending_ по символу
        live_syms = {}
        if executor._mk:
            for lp in live:
                if not isinstance(lp, dict): continue
                lp_mid = UE._pos_market(lp)
                for s, m in executor._mk.items():
                    if str(m.get("id","")) == lp_mid:
                        live_syms[s] = UE._pos_id(lp)
                        break
    except Exception as e:
        return f"⚠️ Ошибка получения live позиций: {e}"

    with _tracked_lock:
        tracked = _load_tracked()

    now_ts = int(_now())
    cleaned_count = 0
    cleaned_list = []

    # 1. Чистим pending_ orphan'ы (старше 60 сек, не подтверждены в live)
    for pid in list(tracked.keys()):
        rec = tracked[pid]
        if rec.get("status") != "open": continue
        if not pid.startswith("pending_"): continue
        sym = rec.get("sym", "?")
        opened_ts = rec.get("opened_ts", 0)
        age_sec = now_ts - opened_ts if opened_ts else 0
        # Если pending_ старше 60 сек и символа нет в live → удаляем
        if age_sec > 60 and sym not in live_syms:
            direction = rec.get("direction", rec.get("dir", "long"))
            with _tracked_lock:
                tr = _load_tracked()
                if pid in tr:
                    tr[pid]["status"] = "closed"
                    tr[pid]["close_reason"] = "AUTO_SYNC_PENDING_ORPHAN"
                    tr[pid]["closed_ts"] = now_ts
                    _save_tracked(tr)
            cleaned_count += 1
            cleaned_list.append(f"⏳ {sym} {direction} (pending {age_sec//60}мин, не открылась)")
        # Если pending_ и символ есть в live → конвертируем в real entry
        elif sym in live_syms:
            real_id = live_syms[sym]
            with _tracked_lock:
                tr = _load_tracked()
                if pid in tr:
                    # Копируем запись под real_id, удаляем pending_
                    tr[real_id] = tr[pid]
                    tr[real_id]["close_reason"] = ""
                    del tr[pid]
                    _save_tracked(tr)
            # Регистрируем стратегию
            try:
                executor._registry_set(real_id, STRATEGY_TAG)
            except: pass
            cleaned_count += 1
            cleaned_list.append(f"🔄 {sym} (pending→real {real_id})")

    # 2. Чистим real orphan'ы (pid не в live_ids)
    # Перечитываем tracked после pending_ очистки
    with _tracked_lock:
        tracked = _load_tracked()
    open_recs = [(pid, v) for pid, v in tracked.items()
                 if v.get("status") == "open" and not pid.startswith("pending_")]
    for pid, rec in open_recs:
        if pid not in live_ids:
            sym = rec.get("sym", "?")
            direction = rec.get("direction", rec.get("dir", "long"))
            opened_ts = rec.get("opened_ts", 0)
            age_min = (now_ts - opened_ts) / 60 if opened_ts else 0
            with _tracked_lock:
                tr = _load_tracked()
                if pid in tr:
                    tr[pid]["status"] = "closed"
                    tr[pid]["close_reason"] = "AUTO_SYNC_ORPHAN"
                    tr[pid]["closed_ts"] = now_ts
                    _save_tracked(tr)
            cleaned_count += 1
            cleaned_list.append(f"👻 {sym} {direction} (real, {age_min:.0f}мин, закрыта на Upscale)")

    # Перечитываем tracked для финального count
    tracked_after = _load_tracked()
    open_n = sum(1 for v in tracked_after.values() if v.get("status") == "open")

    if cleaned_count > 0:
        msg_lines = [f"🔄 V12-max5 SYNC: убрано {cleaned_count} orphan-записей:"]
        for c in cleaned_list:
            msg_lines.append(f"  • {c}")
        msg_lines.append(f"\n📊 Теперь открыто: {open_n}")
        msg = "\n".join(msg_lines)
        if verbose_telegram and B:
            B.send_telegram(msg)
        print(f"[prophet V12-max5] auto-sync: убрано {cleaned_count} orphan, теперь открыто {open_n}")
        return msg
    else:
        live_count = len(live_ids)
        msg = f"✅ SYNC: tracked актуален (open в tracked={len(open_recs)}, live на Upscale={live_count})"
        if verbose_telegram and B:
            B.send_telegram(msg)
        return msg

def run_cleaner(reason="AUTO_CLEAN"):
    """
    Cleaner: проверяет все открытые Prophet позиции и закрывает те, что:
    - Открыты дольше MAX_HOLD + CLEANER_GRACE_HOURS
    - Или имеют статус "open" в tracked, но отсутствуют в live позициях (orphan)
    """
    if not CLEANER_ENABLED:
        return 0, "🧹 Cleaner выключен (PROPHET_CLEANER=0)"

    if not B or not hasattr(B, "EXECUTOR"):
        return 0, "⚠️ Executor недоступен"
    executor = B.EXECUTOR
    if not executor or not executor.client:
        return 0, "⚠️ Нет клиента Upscale"

    with _tracked_lock:
        tracked = _load_tracked()

    open_recs = [(pid, v) for pid, v in tracked.items()
                 if v.get("status") == "open" and not pid.startswith("pending_")]
    if not open_recs:
        return 0, "🧹 Нет открытых позиций для проверки"

    try:
        executor._ensure_account()
        if not executor.account_id:
            return 0, "⚠️ Нет account_id"
        live = UE._as_list(executor.client.positions(executor.account_id))
        live_ids = {UE._pos_id(p) for p in live if isinstance(p, dict)}
    except Exception as e:
        return 0, f"⚠️ Ошибка получения позиций: {e}"

    now = _now()
    max_hold_with_grace = MAX_HOLD + CLEANER_GRACE_HOURS * 3600
    closed_count = 0
    closed_list = []

    for pid, rec in open_recs:
        opened_ts = rec.get("opened_ts", 0)
        age_h = (now - opened_ts) / 3600 if opened_ts else 0
        sym = rec.get("sym", "?")

        too_old = opened_ts and (now - opened_ts > max_hold_with_grace)
        is_orphan = pid not in live_ids

        if too_old or is_orphan:
            close_reason = []
            if too_old:
                close_reason.append(f"старая ({age_h:.1f}ч > {max_hold_with_grace/3600:.0f}ч)")
            if is_orphan:
                close_reason.append("orphan (нет в live)")
            reason_str = ", ".join(close_reason)

            try:
                pos = next((p for p in live if UE._pos_id(p) == pid), None)
                if pos:
                    mid = UE._pos_market(pos)
                    if mid:
                        # V12: dir из rec
                        dir_field = rec.get("dir", rec.get("direction", "long"))
                        if dir_field in ("SHORT", "short"): dir_field = "short"
                        else: dir_field = "long"
                        info = {"id": pid, "mid": mid, "dir": dir_field}
                        executor.close_position(info, reason=reason)
                        closed_list.append(f"{sym} ({age_h:.0f}ч, {reason_str})")
                        closed_count += 1
                elif is_orphan:
                    closed_list.append(f"{sym} ({age_h:.0f}ч, orphan-уборка)")
                    closed_count += 1

                with _tracked_lock:
                    tr = _load_tracked()
                    if pid in tr:
                        tr[pid]["status"] = "closed"
                        tr[pid]["close_reason"] = reason
                        tr[pid]["closed_ts"] = now
                        _save_tracked(tr)
            except Exception as e:
                print(f"[prophet] cleaner error for {sym} ({pid}): {e}")

    if closed_count > 0:
        msg_lines = [f"🧹 Cleaner V12 ({reason}): закрыто {closed_count} позиций:"]
        for c in closed_list:
            msg_lines.append(f"  • {c}")
        msg = "\n".join(msg_lines)
        if B:
            B.send_telegram(msg)
        return closed_count, msg
    else:
        return 0, f"🧹 Cleaner V12 ({reason}): все открытые позиции в норме (проверено {len(open_recs)})"

def cmd_stats():
    lines = ["", "━━━ 🔮 Пророк V12 — Статистика ━━━"]
    lines.append(f"Сканирований: {_stats['scans']}")
    lines.append(f"Сигналов: {_stats['signals']}")
    lines.append(f"Сделок: {_stats['trades']}")
    if _stats["trades"] > 0:
        wr = _stats["wins"]/_stats["trades"]*100
        lines.append(f"Win rate: {wr:.0f}%")
        lines.append(f"PnL: ${_stats['pnl']:+.2f}")
    return "\n".join(lines)

# === START ===

def start():
    _load()
    _reset_daily_pnl_if_needed()
    global _scanner_thread
    if _scanner_thread is None:
        _scanner_thread = threading.Thread(target=_scanner_loop, daemon=True, name="prophet-scanner-v12")
        _scanner_thread.start()
    print(f"[prophet V12-max5] Старт: режим={_state.get('scan_mode','off')}")
    if B:
        B.send_telegram(
            f"🔮 <b>Пророк V12-max5 запущен</b>\n"
            f"📊 Версия: V12-max5 ФИНАЛ\n"
            f"👥 Пар: {len(PAIRS)} | MAX_OPEN={MAX_OPEN} | MAX_SAME_SIDE={MAX_SAME_SIDE}\n"
            f"Режим: {_state.get('scan_mode','off')}\n"
            f"Порог: ручной ≥{MIN_SCORE} | авто ≥{AUTO_SCORE}\n"
            f"📈 TP: динамический (ADX&lt;25 → {TP_MULT_WEAK}×ATR, ADX≥25 → {TP_MULT_STRONG}×ATR)\n"
            f"🛡️ SL={SL_MULT}×ATR | Hold={MAX_HOLD//3600}ч\n"
            f"⏰ Сканы: {AUTO_HOURS_MSK} МСК в :{SCAN_MINUTE:02d}\n"
            f"✅ Подтверждение входа: {CONFIRM_WAIT_SEC//60} мин (15m свеча)\n"
            f"🛡️ Per-side daily stop: -${SIDE_DAILY_STOP_USD:.0f} (LONG/SHORT раздельно)\n"
            f"🟢 LONG + 🔴 SHORT\n"
            f"🧹 Cleaner: {'вкл' if CLEANER_ENABLED else 'выкл'} (grace +{CLEANER_GRACE_HOURS}ч)\n"
            f"Команды: /prophet /prophet_scan /prophet_auto /prophet_close /prophet_clean"
        )
