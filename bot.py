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
    "NEAR","ONDO","OP","ORDI",
    "PENDLE","PENGU","PEPE","PNUT","POL","POPCAT","PUMP","PYTH",
    "QNT","RAY","RENDER","RUNE",
    "S","SAND","SEI","SHIB","SKY","STRK","STX","SUI",
    "TAO","TIA","TRUMP","TRX","TURBO",
    "UNI","VET","VIRTUAL",
    "WAL","WIF","WLD",
    "XLM","XMR","XTZ","ZEC","ZRO",
    "1000BONK","1000PEPE","1000SHIB"
])

START_HOUR = 4
END_HOUR = 22

prev_volumes = {}
scan_count = 0
last_status_hour = -1

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
            logger.info("TG sent OK")
    except Exception as e:
        logger.error(f"TG error: {e}")

def get_msk_time():
    now = datetime.utcnow()
    msk_hour = (now.hour + 3) % 24
    return msk_hour, f"{msk_hour:02d}:{now.minute:02d}"

def is_trading_hours():
    msk_hour, _ = get_msk_time()
    return START_HOUR <= msk_hour < END_HOUR

def get_prices():
    try:
        headers = {"X-CMC_PRO_API_KEY": CMC_API_KEY}
        params = {"limit": 500, "convert": "USD", "sort": "market_cap"}
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

def scan():
    global prev_volumes, scan_count, last_status_hour

    msk_hour, msk_time = get_msk_time()

    if not is_trading_hours():
        logger.info(f"Outside trading hours. MSK: {msk_time}")
        if msk_hour == START_HOUR:
            prev_volumes.clear()
            scan_count = 0
            last_status_hour = -1
            logger.info("New day — reset")
        return

    data = get_prices()
    if not data or "BTC" not in data:
        logger.warning("No data")
        return

    scan_count += 1
    btc = data["BTC"]
    btc_change_1h = btc["change_1h"]
    btc_price = btc["price"]

    # Считаем раскорреляции для статуса
    decorr_count = sum(
        1 for sym, info in data.items()
        if sym != "BTC" and (info["change_1h"] - btc_change_1h) >= 1.5
    )

    # Статус раз в час
    if msk_hour != last_status_hour:
        btc_arrow = "⬇️" if btc_change_1h < 0 else "⬆️"
        status_msg = (
            f"📡 <b>Статус {msk_time} МСК</b>\n\n"
            f"BTC: {fmt(btc_price)} | 1h: {btc_change_1h:+.2f}% {btc_arrow}\n"
            f"Сканирую: {len(data)-1} пар\n"
            f"Раскорреляций 1h: {decorr_count}\n\n"
            f"😴 Жду спайк объёма..."
        )
        send_tg(status_msg)
        last_status_hour = msk_hour
        logger.info(f"Status sent for hour {msk_hour}")

    # Первый скан — только заполняем память объёмов
    if scan_count == 1:
        for sym, info in data.items():
            prev_volumes[sym] = info["volume_24h"]
        logger.info("First scan — volume memory filled")
        return

    candidates = []
    for sym, info in data.items():
        if sym == "BTC":
            continue

        cur_vol = info["volume_24h"]
        prev_vol = prev_volumes.get(sym, 0)

        vol_spike = False
        vol_delta_pct = 0

        if prev_vol > 0 and cur_vol > 0:
            vol_delta_pct = (cur_vol - prev_vol) / prev_vol * 100
            vol_spike = vol_delta_pct >= 20

        diff_1h = info["change_1h"] - btc_change_1h

        if diff_1h >= 1.5 and vol_spike:
            candidates.append({
                "sym": sym,
                "price": info["price"],
                "change_1h": info["change_1h"],
                "diff_1h": diff_1h,
                "vol_delta_pct": vol_delta_pct
            })

    # Обновляем память объёмов
    for sym, info in data.items():
        prev_volumes[sym] = info["volume_24h"]

    if not candidates:
        logger.info(f"No signals. Scan #{scan_count}, decorr: {decorr_count}")
        return

    candidates.sort(key=lambda x: x["vol_delta_pct"] * x["diff_1h"], reverse=True)
    top = candidates[:3]

    btc_arrow = "⬇️" if btc_change_1h < 0 else "⬆️"
    msg = f"🚨 <b>СИГНАЛ {msk_time} МСК</b>\n\n"
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
        msg += f"📈 Объём +{c['vol_delta_pct']:.0f}% за 15 мин\n"
        msg += f"⚡ 1h: {c['change_1h']:+.2f}% | vs BTC: +{c['diff_1h']:.1f}%\n"
        msg += f"📍 Вход: {fmt(price)}\n"
        msg += f"🛑 Стоп: {fmt(stop)}\n"
        msg += f"🎯 TP1: {fmt(tp1)} | TP2: {fmt(tp2)}\n\n"

    msg += "💡 Входи в 1-2 лучших\nЕсли один против — выходишь, второй держишь"

    send_tg(msg)
    logger.info(f"Signals sent: {[c['sym'] for c in top]}")

def main():
    logger.info("Bot v3.1 started!")
    send_tg(
        "🤖 <b>Upscale Signal Bot v3.1</b>\n\n"
        "✅ Скачок объёма за 15 мин (+20%)\n"
        "✅ Раскорреляция с BTC по 1h\n"
        "✅ Торговые часы: 04:00-22:00 МСК\n"
        "✅ Статус каждый час\n"
        f"✅ {len(UPSCALE_SYMBOLS)} пар USDT\n\n"
        "Жду реальный спайк объёма..."
    )
    while True:
        try:
            scan()
        except Exception as e:
            logger.error(f"Error: {e}")
        time.sleep(900)

if __name__ == "__main__":
    main()
