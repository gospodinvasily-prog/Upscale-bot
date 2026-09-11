import asyncio
import aiohttp
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Конфигурация
TELEGRAM_TOKEN = "YOUR_BOT_TOKEN"
CHAT_ID = "426470592"

# Пары Upscale (Binance тикеры)
UPSCALE_PAIRS = [
    "ETHUSDT","BNBUSDT","XRPUSDT","SOLUSDT","AAVEUSDT","ADAUSDT",
    "AEROUSDT","ALGOUSDT","APTUSDT","ARBUSDT","ATOMUSDT","AVAXUSDT",
    "AXSUSDT","BCHUSDT","CAKEUSDT","CHZUSDT","CROUSDT","CRVUSDT",
    "DASHUSDT","DOGEUSDT","DOTUSDT","DYDXUSDT","EIGENUSDT","ENAUSDT",
    "ENSUSDT","ETCUSDT","FETUSDT","FILUSDT","FLOKIUSDT","GALAUSDT",
    "GRASSUSDT","GRTUSDT","HBARUSDT","HYPEUSDT","ICPUSDT","IMXUSDT",
    "INJUSDT","IOTAUSDT","JASMYUSDT","JTOUSDT","JUPUSDT","KAIAUSDT",
    "KASUSDT","LDOUSDT","LINKUSDT","LTCUSDT","MANAUSDT","MNTUSDT",
    "MOVEUSDT","NEARUSDT","ONDOUSDT","OPUSDT","ORDIUSDT","PENDLEUSDT",
    "PENGUUSDT","PNUTUSDT","POLUSDT","PYTHUSDT","QNTUSDT","RAYUSDT",
    "RENDERUSDT","RUNEUSDT","SANDUSDT","SEIUSDT","STRKUSDT","STXUSDT",
    "SUIUSDT","TAOUSDT","TIAUSDT","TRUMPUSDT","TRXUSDT","TURBOUSDT",
    "UNIUSDT","VETUSDT","WIFUSDT","WLDUSDT","XLMUSDT","XMRUSDT",
    "XTZUSDT","ZECUSDT","1000SHIBUSDT","1000BONKUSDT","1000PEPEUSDT",
    "MORPHOUSDT","POPCAT1000USDT","BRETTUSDT","BERAUSDT","EIGENUSDT",
    "SUSDT","FARTCOINUSDT","GRASSUSDT"
]

BYBIT_BASE = "https://api.bybit.com"
TELEGRAM_BASE = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

async def send_telegram(session, text):
    url = f"{TELEGRAM_BASE}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        async with session.post(url, json=payload) as r:
            result = await r.json()
            if not result.get("ok"):
                logger.error(f"Telegram error: {result}")
    except Exception as e:
        logger.error(f"Send error: {e}")

async def get_prices(session):
    """Получаем цены с Bybit — работает из России"""
    try:
        url = f"{BYBIT_BASE}/v5/market/tickers"
        params = {"category": "linear"}
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
            data = await r.json()
            if data.get("retCode") != 0:
                return None
            tickers = data["result"]["list"]
            prices = {}
            for t in tickers:
                symbol = t["symbol"]
                if symbol in UPSCALE_PAIRS or symbol == "BTCUSDT":
                    prices[symbol] = {
                        "price": float(t["lastPrice"]),
                        "change": float(t["price24hPcnt"]) * 100
                    }
            return prices
    except Exception as e:
        logger.error(f"Price fetch error: {e}")
        return None

async def get_klines(session, symbol, limit=50):
    """Получаем свечи для анализа уровней"""
    try:
        url = f"{BYBIT_BASE}/v5/market/kline"
        params = {
            "category": "linear",
            "symbol": symbol,
            "interval": "15",
            "limit": limit
        }
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json()
            if data.get("retCode") != 0:
                return None
            return data["result"]["list"]
    except Exception as e:
        logger.error(f"Klines error {symbol}: {e}")
        return None

def find_levels(klines):
    """Находим уровни поддержки и сопротивления"""
    if not klines or len(klines) < 10:
        return None, None
    
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    closes = [float(k[4]) for k in klines]
    
    current = closes[0]
    
    # Ближайшая поддержка (минимум за последние 20 свечей)
    recent_lows = lows[:20]
    support = min(recent_lows)
    
    # Ближайшее сопротивление (максимум за последние 20 свечей)
    recent_highs = highs[:20]
    resistance = max(recent_highs)
    
    return support, resistance

def calc_signal(symbol, price, change, btc_change, klines):
    """Рассчитываем сигнал входа"""
    diff = change - btc_change
    if diff < 1.5:
        return None
    
    support, resistance = find_levels(klines)
    if not support or not resistance:
        return None
    
    # Стоп под поддержку
    stop = support * 0.995
    stop_pct = (price - stop) / price * 100
    
    # Если стоп слишком далеко — пропускаем
    if stop_pct > 5:
        return None
    
    # TP1 и TP2
    tp1 = price + (price - stop) * 1.5
    tp2 = price + (price - stop) * 2.5
    rr = 2.0
    
    # Риск в долларах на позицию ($5000 счёт, риск $35)
    risk_usd = 35
    qty = risk_usd / (price - stop)
    position_usd = qty * price
    
    if diff >= 4:
        strength = "🔥 Сильный"
    elif diff >= 2.5:
        strength = "⚡ Средний"
    else:
        strength = "👀 Наблюдать"
    
    return {
        "symbol": symbol.replace("USDT", ""),
        "price": price,
        "change": change,
        "diff": diff,
        "stop": stop,
        "tp1": tp1,
        "tp2": tp2,
        "rr": rr,
        "strength": strength,
        "position_usd": min(position_usd, 1500)
    }

def fmt_price(price):
    if price >= 1000:
        return f"${price:,.0f}"
    elif price >= 1:
        return f"${price:.3f}"
    elif price >= 0.01:
        return f"${price:.5f}"
    else:
        return f"${price:.8f}"

async def scan_and_signal(session):
    """Основная функция сканирования"""
    prices = await get_prices(session)
    if not prices:
        logger.warning("No prices received")
        return
    
    btc = prices.get("BTCUSDT")
    if not btc:
        return
    
    btc_change = btc["change"]
    btc_price = btc["price"]
    
    # Ищем раскорреляцию
    candidates = []
    for symbol in UPSCALE_PAIRS:
        if symbol not in prices:
            continue
        data = prices[symbol]
        diff = data["change"] - btc_change
        if diff >= 1.5:
            candidates.append({
                "symbol": symbol,
                "price": data["price"],
                "change": data["change"],
                "diff": diff
            })
    
    # Сортируем по силе раскорреляции
    candidates.sort(key=lambda x: x["diff"], reverse=True)
    top = candidates[:6]
    
    if not top:
        logger.info("No candidates found")
        return
    
    # Анализируем топ кандидатов
    signals = []
    for c in top:
        klines = await get_klines(session, c["symbol"])
        await asyncio.sleep(0.2)
        if not klines:
            continue
        sig = calc_signal(
            c["symbol"], c["price"], c["change"],
            btc_change, klines
        )
        if sig:
            signals.append(sig)
        if len(signals) >= 3:
            break
    
    if not signals:
        logger.info("No valid signals")
        return
    
    # Формируем сообщение
    now = datetime.now().strftime("%H:%M МСК")
    btc_arrow = "⬇️" if btc_change < 0 else "⬆️"
    
    msg = f"📊 <b>СКАН {now}</b>\n\n"
    msg += f"BTC: {fmt_price(btc_price)} | {btc_change:+.2f}% {btc_arrow}\n"
    msg += "─" * 25 + "\n\n"
    
    medals = ["🥇", "🥈", "🥉"]
    for i, sig in enumerate(signals):
        medal = medals[i] if i < 3 else "📍"
        msg += f"{medal} <b>{sig['symbol']}/USD</b> — +{sig['diff']:.1f}% vs BTC\n"
        msg += f"{sig['strength']}\n"
        msg += f"📍 Вход: {fmt_price(sig['price'])}\n"
        msg += f"🛑 Стоп: {fmt_price(sig['stop'])}\n"
        msg += f"🎯 TP1: {fmt_price(sig['tp1'])} | TP2: {fmt_price(sig['tp2'])}\n"
        msg += f"⚖️ RR 1:{sig['rr']:.1f} | Позиция: ~${sig['position_usd']:.0f}\n"
        msg += "\n"
    
    msg += "─" * 25 + "\n"
    msg += "💡 Входи в 1-2 лучших\nЕсли один против — выходишь, второй держишь"
    
    await send_telegram(session, msg)
    logger.info(f"Signal sent: {[s['symbol'] for s in signals]}")

async def main():
    logger.info("Upscale Signal Bot started!")
    
    async with aiohttp.ClientSession() as session:
        # Стартовое сообщение
        await send_telegram(session, 
            "🤖 <b>Upscale Signal Bot запущен!</b>\n\n"
            "Сканирую рынок каждые 15 минут.\n"
            "Буду присылать топ-3 сигнала раскорреляции с BTC.\n\n"
            "Ожидай первый скан..."
        )
        
        while True:
            try:
                logger.info("Running scan...")
                await scan_and_signal(session)
            except Exception as e:
                logger.error(f"Scan error: {e}")
            
            # Ждём 15 минут
            await asyncio.sleep(15 * 60)

if __name__ == "__main__":
    asyncio.run(main())
