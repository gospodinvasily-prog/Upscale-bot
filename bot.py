import os
import time
import requests
from datetime import datetime, timezone, timedelta

# ─── CONFIG ───────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID        = "426470592"

TRADING_START_MSK = 4
TRADING_END_MSK   = 22
MSK = timezone(timedelta(hours=3))

# ── RS Momentum параметры ──
SCAN_INTERVAL        = 5       # минут (5M таймфрейм)
BTC_DECORR_THRESHOLD = 1.5     # % раскорреляция для лонга
BTC_DECORR_SHORT     = -1.5    # % раскорреляция для шорта (альт падает сильнее)
RVOL_THRESHOLD       = 1.2     # минимальный объём
RVOL_HOT_THRESHOLD   = 8.0     # горячий объём 🔥🔥
CLOSE_POS_THRESHOLD  = 0.6     # закрытие в верхних 40% для лонга
CLOSE_POS_SHORT      = 0.4     # закрытие в нижних 40% для шорта
FUNDING_MAX_LONG     = 0.06    # % — перегруз лонгами (предупреждение для лонга)
FUNDING_MIN_SHORT    = -0.06   # % — перегруз шортами (предупреждение для шорта)
FUNDING_EXTREME      = 0.05    # для sweep
ROOM_LOOKBACK        = 20      # свечей для комнаты до хая/лоя
STOP_PCT             = -3.0    # % стоп
ATR_TP2_MULT         = 2.0     # ATR множитель для TP2
TOP_N                = 3       # топ сигналов

# ─── UPSCALE PAIRS ────────────────────────────────────────────────────────────

UPSCALE_PAIRS = [
    "ETH","BNB","XRP","SOL","AAVE","ADA","AERO","ALGO","APT","ARB",
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

# ─── TELEGRAM ─────────────────────────────────────────────────────────────────

def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
        r.raise_for_status()
    except Exception as e:
        print(f"[TG ERROR] {e}")

# История OI для отслеживания изменений (3 скана = 15 минут)
OI_HISTORY: list = []   # [{sym: oi, ...}, ...]
OI_HISTORY_MAX = 3

def get_oi_change(sym: str) -> float:
    if len(OI_HISTORY) < OI_HISTORY_MAX:
        return None
    old_oi = OI_HISTORY[0].get(sym, 0)
    new_oi = OI_HISTORY[-1].get(sym, 0)
    if not old_oi:
        return None
    return round((new_oi - old_oi) / old_oi * 100, 2)

def analyze_signal(s: dict, is_long: bool, btc_chg: float) -> tuple:
    score = 0
    notes = []

    # RVOL
    rvol = s.get("rvol", 0)
    mode = s.get("signal_mode", "explosion")
    if mode == "accumulation":
        if rvol >= 3:     score += 2; notes.append("накопление объёма")
        elif rvol >= 1.5: score += 1
    else:
        if rvol >= 10:    score += 3; notes.append("взрывной объём")
        elif rvol >= 5:   score += 2; notes.append("сильный объём")
        elif rvol >= 3:   score += 1

    # OI
    oi = s.get("oi_chg")
    if oi is not None:
        if oi > 1.5:    score += 2; notes.append("OI растёт")
        elif oi > 0.5:  score += 1
        elif oi < -1:   score -= 2; notes.append("OI падает")

    # Закрытие свечи (для шорта инвертируем)
    cp = s.get("close_position", 0.5)
    eff_cp = cp if is_long else (1 - cp)
    if eff_cp >= 0.90:   score += 2
    elif eff_cp >= 0.75: score += 1
    elif eff_cp < 0.60:  score -= 1; notes.append("слабое закрытие свечи")

    # Раскорр (абсолютное значение)
    decorr = abs(s.get("decorr", 0))
    if decorr >= 3:    score += 2; notes.append("сильный раскорр")
    elif decorr >= 2:  score += 1

    # Рост 24ч
    ch24 = s.get("change_24h", 0)
    if is_long:
        if ch24 < 5:     score += 1
        elif ch24 > 25:  score -= 2; notes.append("монета перегрета")
        elif ch24 > 15:  score -= 1
    else:
        if ch24 > 15:    score += 1; notes.append("перегрета — шорт логичен")
        elif ch24 < -10: score += 1

    # Фандинг
    funding = s.get("funding", 0)
    if is_long:
        if funding < -0.005:  score += 1
        elif funding > 0.03:  score -= 1; notes.append("фандинг перегрет")
    else:
        if funding > 0.02:    score += 1; notes.append("лонги перегружены")
        elif funding < -0.02: score -= 1

    # TP1 — есть структурная цель?
    tp1_label = s.get("tp1_label", "")
    if "хай" in tp1_label or "лой" in tp1_label:
        score += 1

    # BTC 15M контекст
    if is_long:
        if btc_chg > 0.1:    score += 1
        elif btc_chg < -0.3: score -= 1; notes.append("BTC 15M против")
    else:
        if btc_chg < -0.1:   score += 1
        elif btc_chg > 0.3:  score -= 1; notes.append("BTC 15M против")

    # BTC 4H тренд — важнее 15M!
    h4_bias = _market_cache.get("h4_bias", "❓")
    if is_long:
        if "Восходящий" in h4_bias:   score += 1
        elif "Нисходящий" in h4_bias: score -= 2; notes.append("⚠️ BTC 4H нисходящий — против тренда")
        elif "Ниже EMA20" in h4_bias: score -= 1; notes.append("BTC 4H слабый")
    else:
        if "Нисходящий" in h4_bias:   score += 1
        elif "Восходящий" in h4_bias: score -= 2; notes.append("⚠️ BTC 4H восходящий — против тренда")
        elif "Выше EMA20" in h4_bias: score -= 1; notes.append("BTC 4H сильный")

    # Вердикт
    if score >= 8:
        v = "🟢 Сильный"
        if notes: v += f" — {', '.join(notes[:2])}"
        v += ". Входи."
    elif score >= 5:
        v = "🟡 Нормальный"
        if notes: v += f" — {notes[0]}"
        v += ". Проверь CVD."
    elif score >= 2:
        v = "🟠 Слабый"
        if notes: v += f" — {notes[0]}"
        v += ". Уменьши позицию."
    else:
        v = "🔴 Пропустить"
        if notes: v += f" — {', '.join(notes[:2])}"

    return score, v

def is_trading_hours() -> bool:
    return TRADING_START_MSK <= datetime.now(MSK).hour < TRADING_END_MSK

# Мёртвые зоны — откаты, сигналы не отправляем
DEAD_ZONES = [
    (13, 45, 14, 30),
    (15, 45, 16, 30),
    (17, 45, 18, 30),
]

def is_dead_zone() -> bool:
    now = datetime.now(MSK)
    now_min = now.hour * 60 + now.minute
    for h_start, m_start, h_end, m_end in DEAD_ZONES:
        if h_start * 60 + m_start <= now_min < h_end * 60 + m_end:
            return True
    return False

def msk_time_str() -> str:
    return datetime.now(MSK).strftime("%H:%M МСК")

# ─── GATE.IO: тикеры (funding + OI) ──────────────────────────────────────────

def get_gate_tickers() -> dict:
    url = "https://api.gateio.ws/api/v4/futures/usdt/tickers"
    try:
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return {}
        data = r.json()
        if data:
            print(f"[TICKERS] Пример: {data[0]}")
        result = {}
        for t in data:
            contract = t.get("contract", "")
            if not contract.endswith("_USDT"):
                continue
            sym = contract.replace("_USDT", "")
            try:
                funding    = float(t.get("funding_rate", 0)) * 100
                oi_raw     = t.get("total_size") or t.get("open_interest") or t.get("position_size") or 0
                oi         = float(oi_raw)
                change_24h = float(t.get("change_percentage", 0))
            except (ValueError, TypeError):
                continue
            result[sym] = {"funding": funding, "oi": oi, "change_24h": change_24h}
        print(f"[TICKERS] Загружено {len(result)} пар")
        return result
    except Exception as e:
        print(f"[TICKERS ERROR] {e}")
        return {}

# ─── GATE.IO: 1H свечи ────────────────────────────────────────────────────────

def get_gate_candles_1h(symbol: str, limit: int = 60) -> list:
    url = "https://api.gateio.ws/api/v4/futures/usdt/candlesticks"
    try:
        r = requests.get(url, params={"contract": f"{symbol}_USDT", "interval": "1h", "limit": limit}, timeout=10)
        return r.json() if r.status_code == 200 else []
    except Exception as e:
        print(f"[1H ERROR] {symbol}: {e}")
        return []

# ─── ИНДИКАТОРЫ ───────────────────────────────────────────────────────────────

def calc_ema_simple(prices: list, period: int) -> float:
    if len(prices) < period:
        return prices[-1] if prices else 0
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
    return ema

def calc_atr(highs, lows, closes, period=14) -> list:
    trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
           for i in range(1, len(closes))]
    if len(trs) < period:
        return [sum(trs)/len(trs)] if trs else [0]
    atrs = [sum(trs[:period])/period]
    for tr in trs[period:]:
        atrs.append((atrs[-1]*(period-1) + tr) / period)
    return atrs

# ─── КОНТЕКСТ РЫНКА: BTC 1D + 4H + 1H + EQH/EQL ─────────────────────────────

_market_cache = {"text": "", "updated_at": 0, "h4_bias": "❓"}

def find_eq_levels(levels: list, price: float, above: bool, tolerance: float = 0.15) -> list:
    filtered = [l for l in levels if (l > price if above else l < price)]
    if not filtered:
        return []
    filtered.sort(key=lambda x: abs(x - price))
    groups = []
    used = set()
    for i, level in enumerate(filtered):
        if i in used:
            continue
        group = [level]
        for j, other in enumerate(filtered):
            if j != i and j not in used:
                if abs(other - level) / level * 100 <= tolerance:
                    group.append(other)
                    used.add(j)
        if len(group) >= 2:
            groups.append((sum(group)/len(group), len(group)))
        used.add(i)
    groups.sort(key=lambda x: abs(x[0] - price))
    return groups[:2]

def get_swing_levels(highs: list, lows: list) -> tuple:
    swing_highs, swing_lows = [], []
    for i in range(2, len(highs) - 2):
        if highs[i] > highs[i-1] and highs[i] > highs[i-2] and highs[i] > highs[i+1] and highs[i] > highs[i+2]:
            swing_highs.append(highs[i])
        if lows[i] < lows[i-1] and lows[i] < lows[i-2] and lows[i] < lows[i+1] and lows[i] < lows[i+2]:
            swing_lows.append(lows[i])
    return swing_highs, swing_lows

def format_liq_line(eq_highs, eq_lows, price, tf_label):
    parts = []
    # Фильтруем уровни которые цена уже прошла
    eq_highs = [(lvl, cnt) for lvl, cnt in eq_highs if lvl > price * 1.001]
    eq_lows  = [(lvl, cnt) for lvl, cnt in eq_lows  if lvl < price * 0.999]
    if eq_highs:
        lvl, cnt = eq_highs[0]
        pct = (lvl - price) / price * 100
        stars = "⭐" * min(cnt, 3)
        parts.append(f"⬆️ EQH: {lvl:,.0f} ({pct:+.1f}%) {stars}")
    if eq_lows:
        lvl, cnt = eq_lows[0]
        pct = (lvl - price) / price * 100
        stars = "⭐" * min(cnt, 3)
        parts.append(f"⬇️ EQL: {lvl:,.0f} ({pct:+.1f}%) {stars}")
    if eq_highs and eq_lows:
        dist_up   = abs(eq_highs[0][0] - price)
        dist_down = abs(eq_lows[0][0]  - price)
        nearest = "⬆️ вверх" if dist_up < dist_down else "⬇️ вниз"
        parts.append(f"🎯 {nearest}")
    elif eq_highs:
        parts.append("🎯 ⬆️ вверх")
    elif eq_lows:
        parts.append("🎯 ⬇️ вниз")
    if parts:
        return f"   {tf_label}: " + " | ".join(parts)
    return ""

def get_market_context() -> str:
    global _market_cache
    now_ts = time.time()
    if now_ts - _market_cache["updated_at"] < 900 and _market_cache["text"]:
        return _market_cache["text"]

    url = "https://api.gateio.ws/api/v4/futures/usdt/candlesticks"
    try:
        # 1D
        r1d = requests.get(url, params={"contract": "BTC_USDT", "interval": "1d", "limit": 210}, timeout=10)
        c1d = r1d.json() if r1d.status_code == 200 else []

        # 4H
        r4h = requests.get(url, params={"contract": "BTC_USDT", "interval": "4h", "limit": 100}, timeout=10)
        c4h = r4h.json() if r4h.status_code == 200 else []

        # 15М — последние 3 закрытые свечи (45 минут)
        r15 = requests.get(url, params={"contract": "BTC_USDT", "interval": "15m", "limit": 10}, timeout=10)
        c15 = r15.json() if r15.status_code == 200 else []

        # ── 1D bias ──
        d_bias = "❓"
        price  = 0
        if c1d and len(c1d) >= 60:
            closes_1d = [float(c["c"]) for c in c1d]
            price     = closes_1d[-1]
            ema50     = calc_ema_simple(closes_1d, 50)
            ema200    = calc_ema_simple(closes_1d, 200) if len(closes_1d) >= 200 else None
            if ema200 and price > ema200 and price > ema50:   d_bias = "🐂 Бычий"
            elif ema200 and price < ema200 and price < ema50: d_bias = "🐻 Медвежий"
            elif price > ema50:                               d_bias = "📈 Выше EMA50"
            else:                                             d_bias = "📉 Ниже EMA50"

        # ── 4H bias + EQH/EQL ──
        h4_bias = "❓"
        h4_liq  = ""
        if c4h and len(c4h) >= 20:
            cl4  = [float(c["c"]) for c in c4h]
            hi4  = [float(c["h"]) for c in c4h]
            lo4  = [float(c["l"]) for c in c4h]
            p4   = cl4[-1]
            price = p4
            ema20 = calc_ema_simple(cl4, 20)
            rh = [max(hi4[i-3:i]) for i in range(3, len(hi4))]
            rl = [min(lo4[i-3:i]) for i in range(3, len(lo4))]
            hh = rh[-1] > rh[-4] if len(rh) >= 4 else None
            hl = rl[-1] > rl[-4] if len(rl) >= 4 else None
            lh = rh[-1] < rh[-4] if len(rh) >= 4 else None
            ll = rl[-1] < rl[-4] if len(rl) >= 4 else None

            if hh and hl:    h4_bias = "📈 Восходящий"
            elif lh and ll:  h4_bias = "📉 Нисходящий"
            elif p4 > ema20: h4_bias = "↗️ Выше EMA20"
            else:            h4_bias = "↘️ Ниже EMA20"
            _market_cache["h4_bias"] = h4_bias

            sh4, sl4 = get_swing_levels(hi4, lo4)
            eq_h4 = find_eq_levels(sh4, p4, above=True)
            eq_l4 = find_eq_levels(sl4, p4, above=False)
            h4_liq = format_liq_line(eq_h4, eq_l4, p4, "4H")

        # ── BTC 15М: последние 3 закрытые свечи (45 минут) ──
        btc_price_str = f"{price:,.0f} USDT" if price else "—"
        m15_line = ""
        if c15 and len(c15) >= 5:
            candles3 = c15[-4:-1]  # 3 закрытые свечи
            arrows = []
            for c in candles3:
                o = float(c["o"]); cl = float(c["c"])
                chg = (cl - o) / o * 100
                if chg > 0.05:    arrows.append(f"⬆️{chg:+.2f}%")
                elif chg < -0.05: arrows.append(f"⬇️{chg:+.2f}%")
                else:             arrows.append(f"➡️{chg:+.2f}%")
            # Итог за 45 минут
            first_open = float(c15[-4]["o"])
            last_close = float(c15[-2]["c"])
            total_chg  = (last_close - first_open) / first_open * 100
            total_emoji = "⬆️" if total_chg > 0.05 else ("⬇️" if total_chg < -0.05 else "➡️")
            m15_line = f"   15М (45 мин): {' → '.join(arrows)} | Итого: {total_emoji}{total_chg:+.2f}%"

        # ── Сборка ──
        lines = [
            f"📊 BTC: {btc_price_str}",
            f"   1D: {d_bias}",
            f"   4H: {h4_bias}",
        ]
        if m15_line:
            lines.append(m15_line)
        if h4_liq:
            lines.append("💧 BTC ликвидность 4H:")
            lines.append(h4_liq)

        context = "\n".join(lines)
        _market_cache = {"text": context, "updated_at": now_ts}
        print("[MARKET] обновлён")
        return context

    except Exception as e:
        print(f"[MARKET ERROR] {e}")
        return "📊 BTC: данные недоступны"


def get_fresh_price(symbol: str):
    """
    Лёгкий одиночный запрос — самая свежая цена прямо перед отправкой сигнала.
    Используем только для топ-N отобранных сигналов, не для всех 103 пар.
    """
    url = "https://api.gateio.ws/api/v4/futures/usdt/tickers"
    try:
        r = requests.get(url, params={"contract": f"{symbol}_USDT"}, timeout=5)
        if r.status_code != 200:
            return None
        data = r.json()
        if data and len(data) > 0:
            return float(data[0].get("last", 0)) or None
    except Exception as e:
        print(f"[FRESH PRICE ERROR] {symbol}: {e}")
    return None

def refresh_signal(s: dict, is_long: bool) -> dict:
    """
    Обновляет вход, стоп, комнату и TP% на самой свежей цене прямо перед отправкой.
    Структурные уровни (TP1/TP2 хаи/лои) не пересчитываются — только цена входа
    и зависящие от неё проценты, чтобы "комната до хая/лоя" была актуальной.
    """
    fresh = get_fresh_price(s["symbol"])
    if not fresh:
        return s  # не удалось обновить — отправляем как было

    old_price = s["price"]
    s["price"] = fresh

    if is_long:
        s["stop"] = fresh * (1 + STOP_PCT / 100)
        tp1 = s["tp1_price"]
        s["room_pct"]  = (tp1 - fresh) / fresh * 100
        s["near_wall"] = s["room_pct"] < 0.5
    else:
        s["stop"] = fresh * (1 - STOP_PCT / 100)
        tp1 = s["tp1_price"]
        s["room_pct"]   = (fresh - tp1) / fresh * 100
        s["near_floor"] = s["room_pct"] < 0.5

    s["tp1_pct"] = (s["tp1_price"] - fresh) / fresh * 100
    s["tp2_pct"] = (s["tp2_price"] - fresh) / fresh * 100
    print(f"  [REFRESH] {s['symbol']}: {old_price:.6g} → {fresh:.6g}")
    return s

# ─── RS MOMENTUM СКАН (лонг + шорт) ─────────────────────────────────────────

def run_rs_momentum_scan():
    print(f"[RS] Старт {msk_time_str()}")

    # BTC 15М свечи
    url = "https://api.gateio.ws/api/v4/futures/usdt/candlesticks"
    try:
        r = requests.get(url, params={"contract": "BTC_USDT", "interval": "15m", "limit": 30}, timeout=10)
        btc_candles = r.json() if r.status_code == 200 else []
    except Exception:
        btc_candles = []

    if not btc_candles or len(btc_candles) < 4:
        print("[RS] BTC свечи не получены")
        return 0, None

    btc_open  = float(btc_candles[-2]["o"])
    btc_close = float(btc_candles[-2]["c"])
    btc_chg   = (btc_close - btc_open) / btc_open * 100 if btc_open else 0
    print(f"[RS] BTC 15М: {btc_chg:+.2f}%")

    # Тикеры (funding + OI)
    ticker_data = get_gate_tickers()

    # Снимок OI в историю
    oi_snapshot = {sym: d["oi"] for sym, d in ticker_data.items()}
    OI_HISTORY.append(oi_snapshot)
    if len(OI_HISTORY) > OI_HISTORY_MAX:
        OI_HISTORY.pop(0)

    # Контекст рынка
    market_ctx = get_market_context()

    longs  = []
    shorts = []

    for sym in UPSCALE_PAIRS:
        try:
            r = requests.get(url,
                params={"contract": f"{sym}_USDT", "interval": "5m", "limit": 60},
                timeout=8)
            if r.status_code != 200:
                time.sleep(0.05); continue

            candles = r.json()
            if not candles or len(candles) < ROOM_LOOKBACK + 5:
                time.sleep(0.05); continue

            # Средний объём за последние 24 закрытые свечи (2 часа)
            history_vols = [float(c["v"]) for c in candles[-26:-2]]
            vol_avg = sum(history_vols) / len(history_vols) if history_vols else 0
            if vol_avg == 0:
                time.sleep(0.05); continue

            # ── Условие 1: ВЗРЫВ ──
            # Хотя бы одна из 3 последних закрытых свечей даёт RVOL >= порога
            best_candle = None
            best_rvol   = 0
            for idx in [-2, -3, -4]:
                c = candles[idx]
                v = float(c["v"])
                r_vol = round(v / vol_avg, 2) if vol_avg > 0 else 0
                if r_vol > best_rvol:
                    best_rvol   = r_vol
                    best_candle = c

            signal_mode = None
            if best_rvol >= RVOL_THRESHOLD:
                signal_mode = "explosion"
                rvol = best_rvol

            # ── Условие 2: НАКОПЛЕНИЕ ──
            # Объём 4 закрытых свечей подряд растёт И средний RVOL > 1.5x
            if signal_mode is None:
                vols4 = [float(candles[i]["v"]) for i in [-5, -4, -3, -2]]
                growing = all(vols4[i] < vols4[i+1] for i in range(3))
                avg_rvol4 = round(sum(vols4) / (4 * vol_avg), 2) if vol_avg > 0 else 0
                if growing and avg_rvol4 >= 1.5:
                    signal_mode = "accumulation"
                    best_candle = candles[-2]  # последняя закрытая
                    rvol = avg_rvol4

            if signal_mode is None:
                time.sleep(0.05); continue
            alt_open  = float(best_candle["o"])
            alt_close = float(best_candle["c"])
            alt_high  = float(best_candle["h"])
            alt_low   = float(best_candle["l"])
            alt_curr  = float(candles[-1]["c"])

            if alt_open == 0:
                time.sleep(0.05); continue

            alt_chg = (alt_close - alt_open) / alt_open * 100
            decorr  = alt_chg - btc_chg

            if rvol < RVOL_THRESHOLD:
                time.sleep(0.05); continue

            # Размер свечи
            candle_range   = alt_high - alt_low
            close_position = (alt_close - alt_low) / candle_range if candle_range > 0 else 0.5

            # Funding
            funding    = ticker_data.get(sym, {}).get("funding", 0)
            change_24h = ticker_data.get(sym, {}).get("change_24h", 0)
            oi_chg     = get_oi_change(sym)

            # ATR для TP2
            all_highs  = [float(c["h"]) for c in candles]
            all_lows   = [float(c["l"]) for c in candles]
            all_closes = [float(c["c"]) for c in candles]
            atrs = calc_atr(all_highs, all_lows, all_closes, 14)
            atr  = atrs[-1] if atrs else 0

            # ── ЛОНГ ──
            if decorr >= BTC_DECORR_THRESHOLD and close_position >= CLOSE_POS_THRESHOLD:
                # Свинг-хаи из всех доступных свечей
                swing_highs_sym = []
                for i in range(2, len(all_highs) - 1):
                    if all_highs[i] > all_highs[i-1] and all_highs[i] > all_highs[i-2] and \
                       all_highs[i] > all_highs[i+1]:
                        swing_highs_sym.append(all_highs[i])

                # Все хаи выше текущей цены, сортируем по близости
                highs_above = sorted([h for h in swing_highs_sym if h > alt_curr * 1.0001])

                stop = alt_curr * (1 + STOP_PCT / 100)

                # TP1 = ближайший свинг-хай выше цены
                if highs_above:
                    tp1_price = highs_above[0]
                    tp1_pct   = (tp1_price - alt_curr) / alt_curr * 100
                    tp1_label = "следующий хай"
                else:
                    tp1_price = alt_curr + atr
                    tp1_pct   = (tp1_price - alt_curr) / alt_curr * 100
                    tp1_label = "ATR×1.0"

                # Комната до TP1 (расстояние от цены до ближайшего хая/TP1)
                local_high = tp1_price
                room_pct   = (tp1_price - alt_curr) / alt_curr * 100
                near_wall  = room_pct < 0.5  # TP1 ближе 0.5% — стена

                # TP2 = следующий свинг-хай после TP1 или ATR×2.0
                highs_above_tp2 = [h for h in highs_above if h > tp1_price * 1.001]
                if highs_above_tp2:
                    tp2_price = highs_above_tp2[0]
                    tp2_pct   = (tp2_price - alt_curr) / alt_curr * 100
                    tp2_label = "следующий хай"
                else:
                    tp2_price = alt_curr + ATR_TP2_MULT * atr
                    tp2_pct   = (tp2_price - alt_curr) / alt_curr * 100
                    tp2_label = f"ATR×{ATR_TP2_MULT}"

                # Метки качества
                quality = 0; marks = ""
                if rvol >= RVOL_HOT_THRESHOLD:       quality += 2; marks += "🔥🔥"
                elif rvol >= RVOL_THRESHOLD * 2:     quality += 1; marks += "🔥"
                if funding > FUNDING_MAX_LONG:        quality -= 1; marks += "⚠️фандинг"
                if near_wall:                        quality -= 1; marks += "🧱стена"

                longs.append({
                    "symbol": sym, "price": alt_curr, "decorr": decorr,
                    "alt_chg": alt_chg, "rvol": rvol, "signal_mode": signal_mode,
                    "close_position": close_position, "funding": funding, "change_24h": change_24h, "oi_chg": oi_chg,
                    "room_pct": room_pct, "near_wall": near_wall, "local_high": local_high,
                    "tp1_price": tp1_price, "tp1_pct": tp1_pct, "tp1_label": tp1_label,
                    "tp2_price": tp2_price, "tp2_pct": tp2_pct, "tp2_label": tp2_label,
                    "stop": stop, "quality": quality, "marks": marks,
                })

            # ── ШОРТ ──
            elif decorr <= BTC_DECORR_SHORT and close_position <= CLOSE_POS_SHORT:
                # Свинг-лои из всех доступных свечей
                swing_lows_sym = []
                for i in range(2, len(all_lows) - 1):
                    if all_lows[i] < all_lows[i-1] and all_lows[i] < all_lows[i-2] and \
                       all_lows[i] < all_lows[i+1]:
                        swing_lows_sym.append(all_lows[i])

                # Все лои ниже текущей цены, сортируем по близости (ближайший первый)
                lows_below = sorted([l for l in swing_lows_sym if l < alt_curr * 0.9999], reverse=True)

                stop = alt_curr * (1 - STOP_PCT / 100)

                # TP1 = ближайший свинг-лой ниже цены
                if lows_below:
                    tp1_price = lows_below[0]
                    tp1_pct   = (tp1_price - alt_curr) / alt_curr * 100
                    tp1_label = "следующий лой"
                else:
                    tp1_price = alt_curr - atr
                    tp1_pct   = (tp1_price - alt_curr) / alt_curr * 100
                    tp1_label = "ATR×1.0"

                # Комната до TP1 (расстояние от цены до ближайшего лоя/TP1)
                local_low  = tp1_price
                room_pct   = (alt_curr - tp1_price) / alt_curr * 100
                near_floor = room_pct < 0.5  # TP1 ближе 0.5% — пол

                # TP2 = следующий свинг-лой после TP1 или ATR×2.0
                lows_below_tp2 = [l for l in lows_below if l < tp1_price * 0.999]
                if lows_below_tp2:
                    tp2_price = lows_below_tp2[0]
                    tp2_pct   = (tp2_price - alt_curr) / alt_curr * 100
                    tp2_label = "следующий лой"
                else:
                    tp2_price = alt_curr - ATR_TP2_MULT * atr
                    tp2_pct   = (tp2_price - alt_curr) / alt_curr * 100
                    tp2_label = f"ATR×{ATR_TP2_MULT}"

                quality = 0; marks = ""
                if rvol >= RVOL_HOT_THRESHOLD:       quality += 2; marks += "🔥🔥"
                elif rvol >= RVOL_THRESHOLD * 2:     quality += 1; marks += "🔥"
                if funding < FUNDING_MIN_SHORT:       quality -= 1; marks += "⚠️фандинг"
                if near_floor:                       quality -= 1; marks += "🧱пол"

                shorts.append({
                    "symbol": sym, "price": alt_curr, "decorr": decorr,
                    "alt_chg": alt_chg, "rvol": rvol, "signal_mode": signal_mode,
                    "close_position": close_position, "funding": funding, "change_24h": change_24h, "oi_chg": oi_chg,
                    "room_pct": room_pct, "near_floor": near_floor, "local_low": local_low,
                    "tp1_price": tp1_price, "tp1_pct": tp1_pct, "tp1_label": tp1_label,
                    "tp2_price": tp2_price, "tp2_pct": tp2_pct, "tp2_label": tp2_label,
                    "stop": stop, "quality": quality, "marks": marks,
                })

        except Exception as e:
            print(f"  [ERROR] {sym}: {e}")
        time.sleep(0.05)

    print(f"[RS] Лонгов: {len(longs)} | Шортов: {len(shorts)}")

    medals = ["🥇","🥈","🥉"]

    # ── Отправка лонгов ──
    if longs:
        longs.sort(key=lambda x: (x["quality"], x["decorr"]), reverse=True)
        top = longs[:TOP_N]
        top = [refresh_signal(s, is_long=True) for s in top]  # свежая цена перед отправкой
        lines = [
            f"📡 <b>RS MOMENTUM — ЛОНГ</b> | {msk_time_str()}\n"
            f"BTC 15М: <b>{btc_chg:+.2f}%</b>\n"
            f"{market_ctx}\n"
        ]
        for i, s in enumerate(top):
            medal = medals[i] if i < len(medals) else "▪️"
            marks = f" {s['marks']}" if s['marks'] else ""
            score, verdict = analyze_signal(s, is_long=True, btc_chg=btc_chg)
            lines.append(
                f"{medal} <b>{s['symbol']}/USDT</b>{marks}\n"
                f"   RVOL: <b>{s['rvol']}x</b> {'🚀 Взрыв' if s.get('signal_mode') == 'explosion' else '📊 Накопление'} ✅ Gate.io\n"
                f"   Раскорр: <b>{s['decorr']:+.2f}%</b> vs BTC | Альт 15М: {s['alt_chg']:+.2f}%\n"
                f"   Закрытие свечи: {s['close_position']*100:.0f}% | Фандинг: {s['funding']:+.3f}%\n"
                f"   📈 Рост 24ч: {s.get('change_24h', 0):+.2f}%\n"
                f"   {'📈 OI +' + str(s['oi_chg']) + '% — новые лонги ✅' if s.get('oi_chg') is not None and s['oi_chg'] > 1 else '📉 OI ' + str(s['oi_chg']) + '% — шорты закрываются ⚠️' if s.get('oi_chg') is not None and s['oi_chg'] < -1 else '➡️ OI ' + str(s['oi_chg']) + '% (стоит)' if s.get('oi_chg') is not None else '⏳ OI накапливается...'}\n"
                f"   Комната до хая: {s['room_pct']:.2f}%\n"
                f"   Вход: <b>{s['price']:.6g}</b>\n"
                f"   Стоп: {s['stop']:.6g} ({STOP_PCT}%)\n"
                f"   TP1: {s['tp1_price']:.6g} ({s['tp1_pct']:+.1f}%) — {s['tp1_label']} — 50%\n"
                f"   TP2: {s['tp2_price']:.6g} ({s['tp2_pct']:+.1f}%) — {s['tp2_label']} — 50%\n"
                f"   💡 {verdict}\n"
            )
        send_telegram("\n".join(lines))

    # ── Отправка шортов ──
    if shorts:
        shorts.sort(key=lambda x: (x["quality"], abs(x["decorr"])), reverse=True)
        top = shorts[:TOP_N]
        top = [refresh_signal(s, is_long=False) for s in top]  # свежая цена перед отправкой
        lines = [
            f"📡 <b>RS MOMENTUM — ШОРТ</b> | {msk_time_str()}\n"
            f"BTC 15М: <b>{btc_chg:+.2f}%</b>\n"
            f"{market_ctx}\n"
        ]
        for i, s in enumerate(top):
            medal = medals[i] if i < len(medals) else "▪️"
            marks = f" {s['marks']}" if s['marks'] else ""
            score, verdict = analyze_signal(s, is_long=False, btc_chg=btc_chg)
            lines.append(
                f"{medal} <b>{s['symbol']}/USDT</b>{marks}\n"
                f"   RVOL: <b>{s['rvol']}x</b> {'🚀 Взрыв' if s.get('signal_mode') == 'explosion' else '📊 Накопление'} ✅ Gate.io\n"
                f"   Раскорр: <b>{s['decorr']:+.2f}%</b> vs BTC | Альт 15М: {s['alt_chg']:+.2f}%\n"
                f"   Закрытие свечи: {s['close_position']*100:.0f}% | Фандинг: {s['funding']:+.3f}%\n"
                f"   📈 Рост 24ч: {s.get('change_24h', 0):+.2f}%\n"
                f"   {'📈 OI +' + str(s['oi_chg']) + '% — новые шорты ✅' if s.get('oi_chg') is not None and s['oi_chg'] > 1 else '📉 OI ' + str(s['oi_chg']) + '% — лонги закрываются ⚠️' if s.get('oi_chg') is not None and s['oi_chg'] < -1 else '➡️ OI ' + str(s['oi_chg']) + '% (стоит)' if s.get('oi_chg') is not None else '⏳ OI накапливается...'}\n"
                f"   Комната до лоя: {s['room_pct']:.2f}%\n"
                f"   Вход: <b>{s['price']:.6g}</b>\n"
                f"   Стоп: {s['stop']:.6g} (+{abs(STOP_PCT):.0f}%)\n"
                f"   TP1: {s['tp1_price']:.6g} ({s['tp1_pct']:+.1f}%) — {s['tp1_label']} — 50%\n"
                f"   TP2: {s['tp2_price']:.6g} ({s['tp2_pct']:+.1f}%) — {s['tp2_label']} — 50%\n"
                f"   💡 {verdict}\n"
            )
        send_telegram("\n".join(lines))

    return len(longs) + len(shorts), btc_chg

# ─── СТАТУС ───────────────────────────────────────────────────────────────────

def send_status(signal_count=0, btc_chg=None):
    now = datetime.now(MSK)
    ctx = get_market_context()
    btc_line = f"BTC 15М: <b>{btc_chg:+.2f}%</b>\n" if btc_chg is not None else ""
    status = (
        f"🤖 <b>Upscale Bot v7.2</b> | {now.strftime('%H:%M МСК')}\n"
        f"✅ RS Momentum 15М (Лонг + Шорт)\n"
        f"{btc_line}"
        f"{ctx}\n"
        f"Сигналов: <b>{signal_count}</b>\n"
        f"Пар в скане: {len(UPSCALE_PAIRS)}"
    )
    send_telegram(status)

# ─── ГЛАВНЫЙ ЦИКЛ ─────────────────────────────────────────────────────────────

def main():
    send_telegram(
        "🚀 <b>Upscale Bot v7.2 запущен</b>\n"
        "📡 RS Momentum 15М — Лонг + Шорт\n"
        "📊 BTC контекст: 1D + 4H + 1H + EQH/EQL ликвидность\n"
        f"Пар: {len(UPSCALE_PAIRS)} | Часы: {TRADING_START_MSK}:00–{TRADING_END_MSK}:00 МСК"
    )

    last_signal_count = 0
    last_btc          = None
    status_sent_hour  = -1
    last_scan         = 0

    while True:
        now_msk  = datetime.now(MSK)
        cur_hour = now_msk.hour
        now_ts   = time.time()

        if cur_hour != status_sent_hour:
            send_status(last_signal_count, last_btc)
            status_sent_hour = cur_hour

        if is_trading_hours():
            if is_dead_zone():
                print(f"[LOOP] Мёртвая зона, скан пропущен ({msk_time_str()})")
            elif now_ts - last_scan >= SCAN_INTERVAL * 60:
                result = run_rs_momentum_scan()
                if result:
                    last_signal_count, last_btc = result
                last_scan = now_ts

        time.sleep(10)

if __name__ == "__main__":
    main()
