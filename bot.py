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
SCAN_INTERVAL        = 15      # минут
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

def is_trading_hours() -> bool:
    return TRADING_START_MSK <= datetime.now(MSK).hour < TRADING_END_MSK

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
                funding = float(t.get("funding_rate", 0)) * 100
                oi_raw  = t.get("total_size") or t.get("open_interest") or t.get("position_size") or 0
                oi      = float(oi_raw)
            except (ValueError, TypeError):
                continue
            result[sym] = {"funding": funding, "oi": oi}
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

_market_cache = {"text": "", "updated_at": 0}

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
    if now_ts - _market_cache["updated_at"] < 3600 and _market_cache["text"]:
        return _market_cache["text"]

    url = "https://api.gateio.ws/api/v4/futures/usdt/candlesticks"
    try:
        # 1D
        r1d = requests.get(url, params={"contract": "BTC_USDT", "interval": "1d", "limit": 210}, timeout=10)
        c1d = r1d.json() if r1d.status_code == 200 else []

        # 4H
        r4h = requests.get(url, params={"contract": "BTC_USDT", "interval": "4h", "limit": 100}, timeout=10)
        c4h = r4h.json() if r4h.status_code == 200 else []

        # 1H
        r1h = requests.get(url, params={"contract": "BTC_USDT", "interval": "1h", "limit": 50}, timeout=10)
        c1h = r1h.json() if r1h.status_code == 200 else []

        # ── 1D bias ──
        if c1d and len(c1d) >= 60:
            closes_1d = [float(c["c"]) for c in c1d]
            price     = closes_1d[-1]
            ema50     = calc_ema_simple(closes_1d, 50)
            ema200    = calc_ema_simple(closes_1d, 200) if len(closes_1d) >= 200 else None
            if ema200 and price > ema200 and price > ema50:
                d_bias = "🐂 Бычий"
            elif ema200 and price < ema200 and price < ema50:
                d_bias = "🐻 Медвежий"
            elif price > ema50:
                d_bias = "📈 Выше EMA50"
            else:
                d_bias = "📉 Ниже EMA50"
        else:
            d_bias = "❓"
            price  = 0

        # ── 4H bias + EQH/EQL ──
        h4_liq = ""
        if c4h and len(c4h) >= 20:
            cl4 = [float(c["c"]) for c in c4h]
            hi4 = [float(c["h"]) for c in c4h]
            lo4 = [float(c["l"]) for c in c4h]
            p4  = cl4[-1]
            price = p4

            ema20 = calc_ema_simple(cl4, 20)
            rh = [max(hi4[i-3:i]) for i in range(3, len(hi4))]
            rl = [min(lo4[i-3:i]) for i in range(3, len(lo4))]
            hh = rh[-1] > rh[-4] if len(rh) >= 4 else None
            hl = rl[-1] > rl[-4] if len(rl) >= 4 else None
            lh = rh[-1] < rh[-4] if len(rh) >= 4 else None
            ll = rl[-1] < rl[-4] if len(rl) >= 4 else None

            if hh and hl:     h4_bias = "📈 Восходящий"
            elif lh and ll:   h4_bias = "📉 Нисходящий"
            elif p4 > ema20:  h4_bias = "↗️ Выше EMA20"
            else:             h4_bias = "↘️ Ниже EMA20"

            sh4, sl4 = get_swing_levels(hi4, lo4)
            eq_h4 = find_eq_levels(sh4, p4, above=True)
            eq_l4 = find_eq_levels(sl4, p4, above=False)
            h4_liq = format_liq_line(eq_h4, eq_l4, p4, "4H")
        else:
            h4_bias = "❓"

        # ── 1H bias + EQH/EQL + направление ──
        h1_bias = "❓"
        h1_liq  = ""
        h1_dir  = ""
        btc_price_str = ""
        if c1h and len(c1h) >= 20:
            cl1 = [float(c["c"]) for c in c1h]
            hi1 = [float(c["h"]) for c in c1h]
            lo1 = [float(c["l"]) for c in c1h]
            p1  = cl1[-1]
            price = p1
            btc_price_str = f"{p1:,.0f} USDT"

            # 1H изменение за последнюю закрытую свечу
            h1_chg = (cl1[-2] - cl1[-3]) / cl1[-3] * 100 if len(cl1) >= 3 else 0

            ema20_1h = calc_ema_simple(cl1, 20)
            if p1 > ema20_1h and h1_chg > 0:
                h1_dir  = "⬆️ Лонг"
                h1_bias = "⬆️ Лонг"
            elif p1 < ema20_1h and h1_chg < 0:
                h1_dir  = "⬇️ Шорт"
                h1_bias = "⬇️ Шорт"
            elif p1 > ema20_1h:
                h1_dir  = "↗️ Лонг тенденция"
                h1_bias = "↗️"
            else:
                h1_dir  = "↘️ Шорт тенденция"
                h1_bias = "↘️"

            sh1, sl1 = get_swing_levels(hi1, lo1)
            eq_h1 = find_eq_levels(sh1, p1, above=True)
            eq_l1 = find_eq_levels(sl1, p1, above=False)
            h1_liq = format_liq_line(eq_h1, eq_l1, p1, "1H")

        lines = [
            f"📊 BTC: {btc_price_str} | 1H: {h1_dir}",
            f"   1D: {d_bias}",
            f"   4H: {h4_bias}",
        ]
        liq_parts = []
        if h4_liq: liq_parts.append(h4_liq)
        if h1_liq: liq_parts.append(h1_liq)
        if liq_parts:
            lines.append("💧 BTC ликвидность:")
            lines.extend(liq_parts)

        context = "\n".join(lines)
        _market_cache = {"text": context, "updated_at": now_ts}
        print(f"[MARKET] обновлён")
        return context

    except Exception as e:
        print(f"[MARKET ERROR] {e}")
        return "📊 BTC: данные недоступны"

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

    # Тикеры (funding)
    ticker_data = get_gate_tickers()

    # Контекст рынка
    market_ctx = get_market_context()

    longs  = []
    shorts = []

    for sym in UPSCALE_PAIRS:
        try:
            r = requests.get(url,
                params={"contract": f"{sym}_USDT", "interval": "15m", "limit": 30},
                timeout=8)
            if r.status_code != 200:
                time.sleep(0.2); continue

            candles = r.json()
            if not candles or len(candles) < ROOM_LOOKBACK + 3:
                time.sleep(0.2); continue

            # Закрытая свеча [-2]
            alt_open  = float(candles[-2]["o"])
            alt_close = float(candles[-2]["c"])
            alt_high  = float(candles[-2]["h"])
            alt_low   = float(candles[-2]["l"])
            alt_curr  = float(candles[-1]["c"])

            if alt_open == 0:
                time.sleep(0.2); continue

            alt_chg = (alt_close - alt_open) / alt_open * 100
            decorr  = alt_chg - btc_chg

            # RVOL
            vol_closed   = float(candles[-2]["v"])
            history_vols = [float(c["v"]) for c in candles[-22:-2]]
            vol_avg      = sum(history_vols) / len(history_vols) if history_vols else 0
            rvol         = round(vol_closed / vol_avg, 2) if vol_avg > 0 else 0

            if rvol < RVOL_THRESHOLD:
                time.sleep(0.2); continue

            # Размер свечи
            candle_range   = alt_high - alt_low
            close_position = (alt_close - alt_low) / candle_range if candle_range > 0 else 0.5

            # Funding
            funding = ticker_data.get(sym, {}).get("funding", 0)

            # ATR для TP2
            all_highs  = [float(c["h"]) for c in candles]
            all_lows   = [float(c["l"]) for c in candles]
            all_closes = [float(c["c"]) for c in candles]
            atrs = calc_atr(all_highs, all_lows, all_closes, 14)
            atr  = atrs[-1] if atrs else 0

            # ── ЛОНГ ──
            if decorr >= BTC_DECORR_THRESHOLD and close_position >= CLOSE_POS_THRESHOLD:
                # Локальный хай (TP1)
                highs_window = [float(c["h"]) for c in candles[-(ROOM_LOOKBACK+2):-2]]
                local_high   = max(highs_window) if highs_window else alt_curr
                room_pct     = (local_high - alt_curr) / alt_curr * 100
                near_wall    = alt_curr >= local_high * 0.995

                tp1_price = local_high
                tp1_pct   = (tp1_price - alt_curr) / alt_curr * 100
                tp2_price = alt_curr + ATR_TP2_MULT * atr
                tp2_pct   = (tp2_price - alt_curr) / alt_curr * 100
                stop      = alt_curr * (1 + STOP_PCT / 100)

                # Метки качества
                quality = 0; marks = ""
                if rvol >= RVOL_HOT_THRESHOLD:       quality += 2; marks += "🔥🔥"
                elif rvol >= RVOL_THRESHOLD * 2:     quality += 1; marks += "🔥"
                if funding > FUNDING_MAX_LONG:        quality -= 1; marks += "⚠️фандинг"
                if near_wall:                        quality -= 1; marks += "🧱стена"

                longs.append({
                    "symbol": sym, "price": alt_curr, "decorr": decorr,
                    "alt_chg": alt_chg, "rvol": rvol,
                    "close_position": close_position, "funding": funding,
                    "room_pct": room_pct, "near_wall": near_wall,
                    "tp1_price": tp1_price, "tp1_pct": tp1_pct,
                    "tp2_price": tp2_price, "tp2_pct": tp2_pct,
                    "stop": stop, "quality": quality, "marks": marks,
                })

            # ── ШОРТ ──
            elif decorr <= BTC_DECORR_SHORT and close_position <= CLOSE_POS_SHORT:
                # Локальный лой (TP1 для шорта)
                lows_window = [float(c["l"]) for c in candles[-(ROOM_LOOKBACK+2):-2]]
                local_low   = min(lows_window) if lows_window else alt_curr
                room_pct    = (alt_curr - local_low) / alt_curr * 100  # расстояние до лоя
                near_floor  = alt_curr <= local_low * 1.005

                tp1_price = local_low
                tp1_pct   = (tp1_price - alt_curr) / alt_curr * 100  # отрицательный
                tp2_price = alt_curr - ATR_TP2_MULT * atr
                tp2_pct   = (tp2_price - alt_curr) / alt_curr * 100  # отрицательный
                stop      = alt_curr * (1 - STOP_PCT / 100)  # стоп выше для шорта

                quality = 0; marks = ""
                if rvol >= RVOL_HOT_THRESHOLD:       quality += 2; marks += "🔥🔥"
                elif rvol >= RVOL_THRESHOLD * 2:     quality += 1; marks += "🔥"
                if funding < FUNDING_MIN_SHORT:       quality -= 1; marks += "⚠️фандинг"
                if near_floor:                       quality -= 1; marks += "🧱пол"

                shorts.append({
                    "symbol": sym, "price": alt_curr, "decorr": decorr,
                    "alt_chg": alt_chg, "rvol": rvol,
                    "close_position": close_position, "funding": funding,
                    "room_pct": room_pct, "near_floor": near_floor,
                    "tp1_price": tp1_price, "tp1_pct": tp1_pct,
                    "tp2_price": tp2_price, "tp2_pct": tp2_pct,
                    "stop": stop, "quality": quality, "marks": marks,
                })

        except Exception as e:
            print(f"  [ERROR] {sym}: {e}")
        time.sleep(0.2)

    print(f"[RS] Лонгов: {len(longs)} | Шортов: {len(shorts)}")

    medals = ["🥇","🥈","🥉"]

    # ── Отправка лонгов ──
    if longs:
        longs.sort(key=lambda x: (x["quality"], x["decorr"]), reverse=True)
        top = longs[:TOP_N]
        lines = [
            f"📡 <b>RS MOMENTUM — ЛОНГ</b> | {msk_time_str()}\n"
            f"BTC 15М: <b>{btc_chg:+.2f}%</b>\n"
            f"{market_ctx}\n"
        ]
        for i, s in enumerate(top):
            medal = medals[i] if i < len(medals) else "▪️"
            marks = f" {s['marks']}" if s['marks'] else ""
            lines.append(
                f"{medal} <b>{s['symbol']}/USDT</b>{marks}\n"
                f"   RVOL: <b>{s['rvol']}x</b> ✅ Gate.io\n"
                f"   Раскорр: <b>{s['decorr']:+.2f}%</b> vs BTC | Альт 15М: {s['alt_chg']:+.2f}%\n"
                f"   Закрытие свечи: {s['close_position']*100:.0f}% | Фандинг: {s['funding']:+.3f}%\n"
                f"   Комната до хая: {s['room_pct']:.2f}%\n"
                f"   Вход: <b>{s['price']:.6g}</b>\n"
                f"   Стоп: {s['stop']:.6g} ({STOP_PCT}%)\n"
                f"   TP1: {s['tp1_price']:.6g} ({s['tp1_pct']:+.1f}%) — локальный хай — 50%\n"
                f"   TP2: {s['tp2_price']:.6g} ({s['tp2_pct']:+.1f}%) — ATR×{ATR_TP2_MULT} — 50%\n"
            )
        lines.append("⚠️ Проверь CVD. Решение за тобой.")
        send_telegram("\n".join(lines))

    # ── Отправка шортов ──
    if shorts:
        shorts.sort(key=lambda x: (x["quality"], abs(x["decorr"])), reverse=True)
        top = shorts[:TOP_N]
        lines = [
            f"📡 <b>RS MOMENTUM — ШОРТ</b> | {msk_time_str()}\n"
            f"BTC 15М: <b>{btc_chg:+.2f}%</b>\n"
            f"{market_ctx}\n"
        ]
        for i, s in enumerate(top):
            medal = medals[i] if i < len(medals) else "▪️"
            marks = f" {s['marks']}" if s['marks'] else ""
            lines.append(
                f"{medal} <b>{s['symbol']}/USDT</b>{marks}\n"
                f"   RVOL: <b>{s['rvol']}x</b> ✅ Gate.io\n"
                f"   Раскорр: <b>{s['decorr']:+.2f}%</b> vs BTC | Альт 15М: {s['alt_chg']:+.2f}%\n"
                f"   Закрытие свечи: {s['close_position']*100:.0f}% | Фандинг: {s['funding']:+.3f}%\n"
                f"   Комната до лоя: {s['room_pct']:.2f}%\n"
                f"   Вход: <b>{s['price']:.6g}</b>\n"
                f"   Стоп: {s['stop']:.6g} (+{abs(STOP_PCT):.0f}%)\n"
                f"   TP1: {s['tp1_price']:.6g} ({s['tp1_pct']:+.1f}%) — локальный лой — 50%\n"
                f"   TP2: {s['tp2_price']:.6g} ({s['tp2_pct']:+.1f}%) — ATR×{ATR_TP2_MULT} — 50%\n"
            )
        lines.append("⚠️ Проверь CVD. Решение за тобой.")
        send_telegram("\n".join(lines))

    return len(longs) + len(shorts), btc_chg

# ─── СТАТУС ───────────────────────────────────────────────────────────────────

def send_status(signal_count=0, btc_chg=None):
    now = datetime.now(MSK)
    ctx = get_market_context()
    btc_line = f"BTC 15М: <b>{btc_chg:+.2f}%</b>\n" if btc_chg is not None else ""
    status = (
        f"🤖 <b>Upscale Bot v7.0</b> | {now.strftime('%H:%M МСК')}\n"
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
        "🚀 <b>Upscale Bot v7.0 запущен</b>\n"
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
            if now_ts - last_scan >= SCAN_INTERVAL * 60:
                result = run_rs_momentum_scan()
                if result:
                    last_signal_count, last_btc = result
                last_scan = now_ts
        else:
            print(f"[LOOP] Вне часов ({msk_time_str()})")

        time.sleep(60)

if __name__ == "__main__":
    main()
