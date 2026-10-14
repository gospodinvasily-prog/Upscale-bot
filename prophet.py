# -*- coding: utf-8 -*-
"""
prophet.py — Провидец v2 (Multi-TF Scanner + Auto-trade)
=========================================================
Мульти-таймфрейм сканер: 4H + 1H + 15m
Golden filter: LONG + ATR 2.0-3.0% + confluence 3/3 + BTC bullish
Скоринг: день недели + час + ATR + ADX + RSI + Volume + BB

Режимы:
  /prophet           — статус
  /prophet_scan       — один скан сейчас
  /prophet_scan on    — непрерывный скан (каждый час)
  /prophet_scan off   — остановить
  /prophet_auto on    — авто (только 11:00 и 15:00 МСК)
  /prophet_auto off   — остановить
  /prophet_close      — закрыть позицию
  /prophet_stats      — статистика
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

STRATEGY_TAG = "prophet_v2"
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

# Params
RISK_USD = float(os.environ.get("PROPHET_RISK_USD", "100"))
LEVERAGE = int(os.environ.get("PROPHET_LEVERAGE", "5"))
TP_MULT = float(os.environ.get("PROPHET_TP_MULT", "3.0"))
SL_MULT = float(os.environ.get("PROPHET_SL_MULT", "2.0"))
MAX_HOLD = int(os.environ.get("PROPHET_MAX_HOLD_HOURS", "48")) * 3600
MIN_SCORE = int(os.environ.get("PROPHET_MIN_SCORE", "70"))
AUTO_SCORE = int(os.environ.get("PROPHET_AUTO_SCORE", "90"))
SCAN_INTERVAL = int(os.environ.get("PROPHET_SCAN_INTERVAL", "3600"))
MAX_MARGIN = float(os.environ.get("PROPHET_MAX_MARGIN", "3000"))
MAX_OPEN = int(os.environ.get("PROPHET_MAX_OPEN", "2"))
AUTO_HOURS_MSK = [int(x) for x in os.environ.get("PROPHET_AUTO_HOURS", "11,15").split(",")]
# Cleaner: при auto-scan закрывает позиции старше MAX_HOLD (по умолчанию 48ч)
CLEANER_ENABLED = os.environ.get("PROPHET_CLEANER", "1") == "1"
# Дополнительный таймаут для cleaner: если позиция открыта дольше MAX_HOLD + GRACE, принудительно закрыть
CLEANER_GRACE_HOURS = int(os.environ.get("PROPHET_CLEANER_GRACE_HOURS", "2"))

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

_state = {
    "scan_mode": "off",  # off / single / continuous / auto
    "last_scan_ts": 0,
    "last_signal_ts": 0,
    "pending_signal": None,  # ждёт ответа по кнопке
    "trades_today": 0,
    "trades_today_date": "",
}
_stats = {"scans": 0, "signals": 0, "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0}


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
            with open(STATE_FILE) as f: _state.update(json.load(f))
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

# === ANALYSIS ===

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
    
    # HARD GATES
    if btc_reg != +1: return None
    if atr_pct < 2.0 or atr_pct > 3.5: return None
    if rsi > 80 or rsi < 20: return None
    lcp = abs(last["close"]-last["open"])/last["open"]*100 if last["open"] > 0 else 0
    if lcp > 6: return None
    if price < sma50 * 0.98: return None
    
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
    
    d4 = "LONG" if price > sma50*1.02 and rsi > 50 else "NEUTRAL"
    
    agree = sum(1 for d in [d4, d1, d15] if d == "LONG")
    if agree < 2: return None
    if d4 != "LONG": return None
    
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
    
    # 4. RSI (0-10)
    if 60 <= rsi <= 80: score += 10; details.append(f"RSI{rsi:.0f}✓")
    elif 50 <= rsi < 60: score += 5; details.append(f"RSI{rsi:.0f}")
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
    
    # 6. HOUR (0-10)
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
    
    sp = SL_MULT * atr_pct; tp = TP_MULT * atr_pct
    return {
        "symbol": sym, "price": price, "score": score, "direction": "LONG",
        "atr_pct": atr_pct, "rsi": rsi, "adx": adx, "agree": agree,
        "vol_ratio": vr, "bb_exp": bb_exp, "weekday": wdn, "hour": hm,
        "stop_pct": sp, "tp_pct": tp, "atr": atr,
        "stop_price": price*(1-sp/100), "tp_price": price*(1+tp/100),
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
    
    # Предзагрузка свечей (кешируем на 5 минут)
    global _cache_ts
    if _now() - _cache_ts > 300:
        _candle_cache.clear()
        _cache_ts = _now()
    
    results = []
    for sym in PAIRS:
        try:
            c4h = _fetch(sym, "4h", scan_ts - 30*86400, scan_ts + 3600)
            c1h = _fetch(sym, "1h", scan_ts - 7*86400, scan_ts + 3600)
            c15m = _fetch(sym, "15m", scan_ts - 3*86400, scan_ts + 3600)
            if not c4h or not c1h or not c15m: continue
            r = analyze(sym, scan_ts, c4h, c1h, c15m, btc_reg, scan_dt)
            if r: results.append(r)
        except Exception as e:
            print(f"[prophet] {sym}: {e}")
    
    if not results: return None
    results.sort(key=lambda x: -x["score"])
    return results[0]

# === SIGNAL ===

def send_signal(setup):
    """Отправляет сигнал в Telegram с inline кнопками."""
    if not B: return
    _state["pending_signal"] = setup
    _state["last_signal_ts"] = _now()
    _save()
    
    s = setup
    msg = (
        f"🔮 <b>ПРОРОК-СИГНАЛ</b>\n\n"
        f"🎯 {s['symbol']}/USDT — 🟢 LONG\n"
        f"Score: <b>{s['score']:.0f}</b>\n\n"
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

# === TRADE EXECUTION ===

def execute_trade(setup):
    if not B or not hasattr(B, "EXECUTOR"): return
    executor = B.EXECUTOR
    if not executor: return
    
    sym = setup["symbol"]
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
        "symbol": sym, "side": "long", "price": entry,
        "stop": stop, "stop_pct": sp, "tp1_price": tp,
        "exit_mode": "tp_ladder", "risk_usd": RISK_USD,
        "max_pos_usd": pos_size, "max_same_side": 1,
        "strategy": STRATEGY_TAG, "leverage_override": LEVERAGE, "ext_atr": 0.0,
    }
    
    try:
        score = int(setup["score"])
        executor.on_signal(sig, score)
        if B:
            B.send_telegram(
                f"✅ <b>ПРОРОК: {sym} LONG открыт</b>\n"
                f"Вход: ${entry:.6g}\n"
                f"Стоп: ${stop:.6g} ({sp:.1f}%)\n"
                f"Тейк: ${tp:.6g} ({setup['tp_pct']:.1f}%)\n"
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
                "sym": sym, "side": "long", "entry": entry,
                "stop": stop, "tp": tp, "opened_ts": _now(),
                "strategy": STRATEGY_TAG, "status": "open",
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
                        info = {"id": pid, "mid": mid, "dir": "long"}
                        try:
                            executor.close_position(info, reason=f"PROPHET_TIMEOUT_{MAX_HOLD//3600}h")
                            if B: B.send_telegram(f"⏱️ Prophet {rec['sym']}: safety-net {MAX_HOLD//3600}ч")
                        except: pass
    
    if changed:
        with _tracked_lock: _save_tracked(tracked)

# === SCAN LOOPS ===

_scanner_thread = None

def _scanner_loop():
    """Главный цикл: continuous + auto режимы."""
    print("[prophet] Scanner запущен")
    while True:
        try:
            mode = _state.get("scan_mode", "off")
            if mode == "off":
                time.sleep(60); continue
            
            now_ts = int(_now())
            now_dt = dt.datetime.now(MSK)
            
            if mode == "auto":
                if now_dt.hour not in AUTO_HOURS_MSK:
                    time.sleep(60); continue
                # Проверяем не сканировали ли в этот час
                if _now() - _state.get("last_scan_ts", 0) < 3600:
                    time.sleep(60); continue
            elif mode == "continuous":
                if _now() - _state.get("last_scan_ts", 0) < SCAN_INTERVAL:
                    time.sleep(60); continue
            
            # Лимит сделок
            if _state.get("trades_today_date") != _today():
                _state["trades_today"] = 0
                _state["trades_today_date"] = _today()
            
            # === CLEANER: перед каждым сканом проверяем зависшие позиции ===
            # В auto-режиме — всегда; в continuous — каждые 6 часов
            do_clean = False
            if mode == "auto":
                do_clean = True
            elif mode == "continuous":
                last_clean = _state.get("last_clean_ts", 0)
                if _now() - last_clean > 6 * 3600:
                    do_clean = True
            
            if do_clean:
                try:
                    closed_n, _ = run_cleaner(reason=f"PRE_SCAN_{mode.upper()}")
                    _state["last_clean_ts"] = _now()
                    _save()
                    if closed_n > 0 and B:
                        # После cleaner'а перечитываем tracked — слотов могло освободиться
                        tracked = _load_tracked()
                except Exception as e:
                    print(f"[prophet] cleaner error in scanner loop: {e}")
            
            # Уже есть открытая позиция?
            tracked = _load_tracked()
            open_count = sum(1 for v in tracked.values() if v.get("status") == "open")
            if open_count >= MAX_OPEN:
                time.sleep(60); continue
            
            # Сканируем
            _state["last_scan_ts"] = _now()
            _stats["scans"] += 1
            _save()
            
            scan_dt_str = dt.datetime.now(MSK).strftime("%H:%M")
            setup = do_scan(now_ts)
            
            if setup:
                min_s = AUTO_SCORE if mode == "auto" else MIN_SCORE
                if setup["score"] >= min_s:
                    _stats["signals"] += 1
                    send_signal(setup)
                else:
                    # Сигнал есть, но ниже порога
                    if B and mode in ("auto", "single"):
                        threshold_str = f"авто ≥{AUTO_SCORE}" if mode == "auto" else f"ручной ≥{MIN_SCORE}"
                        B.send_telegram(
                            f"🔮 Скан {scan_dt_str} МСК завершён\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"Проверено пар: {len(PAIRS)}\n"
                            f"Лучший сигнал: {setup['symbol']} LONG score={setup['score']:.0f}\n"
                            f"⏸️ Ниже порога ({threshold_str}) — пропуск\n"
                            f"Открыто: {open_count}/{MAX_OPEN}"
                        )
            else:
                # Сигналов вообще не найдено
                if B and mode in ("auto", "single"):
                    B.send_telegram(
                        f"🔮 Скан {scan_dt_str} МСК завершён\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"Проверено пар: {len(PAIRS)}\n"
                        f"❌ Сигналов не найдено\n"
                        f"Открыто: {open_count}/{MAX_OPEN}"
                    )
            
            time.sleep(30)
        except Exception as e:
            print(f"[prophet] loop error: {e}")
            time.sleep(60)

# === COMMANDS ===

def cmd_status():
    mode = _state.get("scan_mode", "off")
    mode_str = {"off": "🔴 Выкл", "continuous": "🔄 Непрерывный (каждый час)", "auto": "🤖 Авто (11:00/15:00)"}.get(mode, mode)
    lines = ["", "━━━ 🔮 Пророк ━━━"]
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
    lines.append(f"\n📅 Авто: 11:00/15:00 МСК | Пар: {len(PAIRS)} | MAX_OPEN={MAX_OPEN}")
    lines.append(f"Порог: ручной ≥{MIN_SCORE} | авто ≥{AUTO_SCORE}")
    lines.append(f"TP={TP_MULT}×ATR | SL={SL_MULT}×ATR | Hold={MAX_HOLD//3600}ч")
    lines.append(f"🧹 Cleaner: {'вкл' if CLEANER_ENABLED else 'выкл'} (grace +{CLEANER_GRACE_HOURS}ч)")
    lines.append(f"\nКоманды:")
    lines.append(f"  /prophet_scan — один скан")
    lines.append(f"  /prophet_scan on — непрерывный")
    lines.append(f"  /prophet_auto on — авто по графику")
    lines.append(f"  /prophet_clean — cleaner зависших")
    return "\n".join(lines)

def cmd_scan(arg=""):
    if arg.lower() == "on":
        _state["scan_mode"] = "continuous"
        _save()
        return "🔄 Непрерывный скан включён (каждый час). /prophet_scan off — остановить."
    elif arg.lower() == "off":
        _state["scan_mode"] = "off"
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
                        B.send_telegram(f"🔮 Скан завершён\nЛучший: {setup['symbol']} LONG score={setup['score']:.0f}\nНиже порога {MIN_SCORE} — пропуск")
                    else:
                        B.send_telegram("🔮 Скан завершён\n❌ Сигналов не найдено")
            except Exception as e:
                if B: B.send_telegram(f"⚠️ /prophet_scan ошибка: {e}")
        threading.Thread(target=_run, daemon=True, name="prophet-single").start()
        return "🔍 Запускаю скан..."

def cmd_auto(arg=""):
    if arg.lower() == "on":
        _state["scan_mode"] = "auto"
        _save()
        return f"🤖 Авто-режим включён. Скан в {AUTO_HOURS_MSK} МСК. Порог ≥{AUTO_SCORE}."
    elif arg.lower() == "off":
        _state["scan_mode"] = "off"
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
                    info = {"id": pid, "mid": mid, "dir": "long"}
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

def run_cleaner(reason="AUTO_CLEAN"):
    """
    Cleaner: проверяет все открытые Prophet позиции и закрывает те, что:
    - Открыты дольше MAX_HOLD + CLEANER_GRACE_HOURS (по умолчанию 48 + 2 = 50 часов)
    - Или имеют статус "open" в tracked, но отсутствуют в live позициях Upscale (orphan)
    
    Возвращает (closed_count, message).
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
    
    # Получаем live позиции с Upscale
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
        
        # Условие 1: позиция старше MAX_HOLD + GRACE
        too_old = opened_ts and (now - opened_ts > max_hold_with_grace)
        
        # Условие 2: orphan — в tracked есть, в live нет
        is_orphan = pid not in live_ids
        
        if too_old or is_orphan:
            close_reason = []
            if too_old:
                close_reason.append(f"старая ({age_h:.1f}ч > {max_hold_with_grace/3600:.0f}ч)")
            if is_orphan:
                close_reason.append("orphan (нет в live)")
            reason_str = ", ".join(close_reason)
            
            try:
                # Найти live позицию для закрытия (если не orphan)
                pos = next((p for p in live if UE._pos_id(p) == pid), None)
                if pos:
                    mid = UE._pos_market(pos)
                    if mid:
                        info = {"id": pid, "mid": mid, "dir": "long"}
                        executor.close_position(info, reason=reason)
                        closed_list.append(f"{sym} ({age_h:.0f}ч, {reason_str})")
                        closed_count += 1
                elif is_orphan:
                    # Orphan — только обновляем tracked, в live нет
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
        msg_lines = [f"🧹 Cleaner ({reason}): закрыто {closed_count} позиций:"]
        for c in closed_list:
            msg_lines.append(f"  • {c}")
        msg = "\n".join(msg_lines)
        if B:
            B.send_telegram(msg)
        return closed_count, msg
    else:
        return 0, f"🧹 Cleaner ({reason}): все открытые позиции в норме (проверено {len(open_recs)})"

def cmd_stats():
    lines = ["", "━━━ 🔮 Пророк — Статистика ━━━"]
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
    global _scanner_thread
    if _scanner_thread is None:
        _scanner_thread = threading.Thread(target=_scanner_loop, daemon=True, name="prophet-scanner")
        _scanner_thread.start()
    print(f"[prophet] Старт: режим={_state.get('scan_mode','off')}")
    if B:
        B.send_telegram(
            f"🔮 <b>Пророк v2 запущен</b>\n"
            f"Пар: {len(PAIRS)} | MAX_OPEN={MAX_OPEN}\n"
            f"Режим: {_state.get('scan_mode','off')}\n"
            f"Порог: ручной ≥{MIN_SCORE} | авто ≥{AUTO_SCORE}\n"
            f"TP={TP_MULT}×ATR | SL={SL_MULT}×ATR | Hold={MAX_HOLD//3600}ч\n"
            f"Авто: {AUTO_HOURS_MSK} МСК | Cleaner: {'вкл' if CLEANER_ENABLED else 'выкл'} (grace +{CLEANER_GRACE_HOURS}ч)\n"
            f"Команды: /prophet /prophet_scan /prophet_auto /prophet_close /prophet_clean"
        )
