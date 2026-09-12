import logging
import requests
import time
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = "8751822340:AAH-sKgtw58OUiUPnor5av_VeoIAMyWb5JA"
CHAT_ID = "426470592"
CMC_API_KEY = "b2e925cc66dc4dacacb1c3de4af26f35"

TG_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
CMC_URL = "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest"

# Все пары из Upscale (проверено по скринам)
UPSCALE_SYMBOLS = set([
    "ETH","BNB","XRP","SOL",
    "AAVE","ADA","AERO","ALGO","APT","ARB","ASTER","ATOM","AVAX","AXS",
    "BCH","BERA","BGB","BONK","BRETT","BSV",
    "CAKE","CHZ","CRO","CRV",
    "DASH","DATA","DEEP","DEXE","DOGE","DOT","DYDX",
    "EIGEN","ENA","ENS","ETC",
    "FARTCOIN","FET","FIL","FLOKI",
    "GALA","GRAM","GRASS","GRT",
    "HBAR","HYPE",
    "ICP","IMX","INJ","IOTA",
    "JASMY","JTO","JUP",
    "KAIA","KAITO","KAS",
    "LDO","LINEA","LINK","LTC",
    "MANA","MNT","MORPHO","MOVE",
    "NEAR",
    "ONDO","OP","ORDI",
    "PENDLE","PENGU","PEPE","PNUT","POL","POPCAT","PUMP","PYTH",
    "QNT",
    "RAY","RENDER","RUNE",
    "S","SAND","SEI","SHIB","SKY","STRK","STX","SUI",
    "TAO","TIA","TRUMP","TRX","TURBO",
    "UNI",
    "VET","VIRTUAL",
    "WAL","WIF","WLD",
    "XLM","XMR","XTZ",
    "ZEC","ZRO",
    "1000BONK","1000PEPE","1000SHIB"
])

# Время работы бота (МСК = UTC+3)
START_HOUR = 4   # 04:00 МСК
END_HOUR = 22    # 22:00 МСК

# Память объёмов и базовый объём
prev_volumes = {}
base_volumes = {}  # средний объём накопленный с утра
scan_count = 0

def send_tg(text):
    try:
        r = requests.post(TG_URL, json={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML"
        }, timeout=10)
        if not r.json().get("ok"):
            logger.error(f"TG error: {r.json()}")
        else:
            logger.info("Message sent OK")
    except Exception as e:
        logger.error(f"TG error: {e}")

def is_trading_hours():
    """Проверяем торговые часы МСК (UTC+3)"""
    utc_hour = datetime.utcnow().hour
    msk_hour = (utc_hour + 3) % 24
    return START_HOUR <= msk_hour < END_HOUR

def get_prices():
    try:
        headers = {"X-CMC_PRO_API_KEY": CMC_API_KEY}
        params = {
            "limit": 500,
            "convert": "USD",
            "sort": "market_cap"
        }
        r = requests.get(CMC_URL, headers=headers, params=params, timeout=20)
        data = r.json()

        if data.get("status", {}).get("error_code") != 0:
            logger.error(f"CMC error: {data.get('status')}")
            return None

        prices = {}
        for coin in data["data"]:
            sym = coin["symbol"]
            if sym == "BTC" or sym in UPSCALE_SYMBOLS:
                try:
                    q = coin["quote"]["USD"]
                    prices[sym] = {
                        "price": q["price"],
                        "change_1h": q.get("percent_change_1h", 0) or 0,
                        "change_24h": q.get("percent_change_24h", 0) or 0,
                        "volume_24h": q.get("volume_24h", 0) or 0,
                    }
                except:
                    pass

        logger.info(f"Got {len(prices)} prices")
        return prices
    except Exception as e:
        logger.error(f"CMC error: {e}")
        return None

def fmt(p):
    if p >= 1000: return f"${p:,.0f}"
    if p >= 1: return f"${p:.3f}"
    if p >= 0.01: return f"${p:.5f}"
    return f"${p:.8f}"

def update_base_volumes(data):
    """Обновляем базовый средний объём"""
    global base_volumes, scan_count
    scan_count += 1
    for sym, info in data.items():
        vol = info["volume_24h"]
        if vol <= 0:
            continue
        if sym not in base_volumes:
            base_volumes[sym] = vol
        else:
            # Скользящее среднее
            base_volumes[sym] = (base_volumes[sym] * (scan_count - 1) + vol) / scan_count

def scan():
    global prev_volumes, scan_count

    # Проверяем торговые часы
    if not is_trading_hours():
        utc_hour = datetime.utcnow().hour
        msk_hour = (utc_hour + 3) % 24
        logger.info(f"Outside trading hours. MSK: {msk_hour:02d}:xx")

        # Сбрасываем базу в начале нового дня
        if msk_hour == START_HOUR:
            base_volumes.clear()
            scan_count = 0
            logger.info("New day — volume base reset")
        return

    data = get_prices()
    if not data or "BTC" not in data:
        logger.warning("No data")
        return

    # Накапливаем базовый объём
    update_base_volumes(data)

    btc = data["BTC"]
    btc_change_1h = btc["change_1h"]
    btc_price = btc["price"]

    # Нужно минимум 4 скана (1 час) для надёжной базы
    if scan_count < 4:
        msk_hour = (datetime.utcnow().hour + 3) % 24
        logger.info(f"Building volume base... scan {scan_count}/4. MSK: {msk_hour:02d}:xx")
        return

    candidates = []
    for sym, info in data.items():
        if sym == "BTC":
            continue

        # Раскорреляция по 1h
        diff_1h = info["change_1h"] - btc_change_1h
        if diff_1h < 1.5:
            continue

        # Проверяем спайк объёма
        cur_vol = info["volume_24h"]
        base_vol = base_volumes.get(sym, 0)

        if base_vol <= 0 or cur_vol <= 0:
            continue

        vol_ratio = cur_vol / base_vol
        vol_spike = vol_ratio >= 1.5  # объём выше базы на 50%+

        # Без спайка объёма — пропускаем
        if not vol_spike:
            continue

        candidates.append({
            "sym": sym,
            "price": info["price"],
            "change_1h": info["change_1h"],
            "diff_1h": diff_1h,
            "vol_ratio": vol_ratio,
            "vol_pct": (vol_ratio - 1) * 100
        })

    if not candidates:
        logger.info("No signals with volume spike")
        return

    # Сортируем по силе объёма * раскорреляции
    candidates.sort(key=lambda x: x["vol_ratio"] * x["diff_1h"], reverse=True)
    top = candidates[:3]

    now = datetime.now().strftime("%H:%M")
    btc_arrow = "⬇️" if btc_change_1h < 0 else "⬆️"
    msg = f"🚨 <b>СИГНАЛ {now} МСК</b>\n\n"
    msg += f"BTC: {fmt(btc_price)} | 1h: {btc_change_1h:+.2f}% {btc_arrow}\n"
    msg += f"{'─'*22}\n\n"

    medals = ["🥇", "🥈", "🥉"]
    for i, c in enumerate(top):
        sig = "🔥" if c["diff_1h"] >= 3 else "⚡"
        price = c["price"]
        stop = price * 0.97
        tp1 = price * 1.05
        tp2 = price * 1.09

        msg += f"{medals[i]} <b>{c['sym']}/USDT</b> {sig}\n"
        msg += f"📈 Объём: +{c['vol_pct']:.0f}% от базы\n"
        msg += f"⚡ 1h: {c['change_1h']:+.2f}% | vs BTC: +{c['diff_1h']:.1f}%\n"
        msg += f"📍 Вход: {fmt(price)}\n"
        msg += f"🛑 Стоп: {fmt(stop)}\n"
        msg += f"🎯 TP1: {fmt(tp1)} | TP2: {fmt(tp2)}\n\n"

    msg += "💡 Входи в 1-2 лучших\nЕсли один против — выходишь, второй держишь"

    send_tg(msg)
    logger.info(f"Signals sent: {[c['sym'] for c in top]}")

def main():
    global scan_count, base_volumes
    logger.info("Upscale Signal Bot v3.0 started!")
    send_tg(
        "🤖 <b>Upscale Signal Bot v3.0</b>\n\n"
        "✅ Торговые окна: 04:00 - 22:00 МСК\n"
        "✅ Спайк объёма +50% от базы\n"
        "✅ Раскорреляция по 1h\n"
        f"✅ {len(UPSCALE_SYMBOLS)} пар USDT\n\n"
        "Без спайка объёма — молчу.\n"
        "Накапливаю базу объёма 1 час после 04:00 МСК..."
    )

    while True:
        try:
            scan()
        except Exception as e:
            logger.error(f"Error: {e}")
        time.sleep(900)

if __name__ == "__main__":
    main()
