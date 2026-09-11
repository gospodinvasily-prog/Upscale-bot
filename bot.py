import logging
import requests
import time
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = "8751822340:AAH-sKgtw58OUiUPnor5av_VeoIAMyWb5JA"
CHAT_ID = "426470592"

TG_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

UPSCALE_SYMBOLS = [
    "ETH","BNB","XRP","SOL","AAVE","ADA","ALGO","APT","ARB","ATOM",
    "AVAX","AXS","BCH","CRV","DASH","DOGE","DOT","ENA","ETC","FET",
    "FIL","GRT","HBAR","HYPE","ICP","INJ","IOTA","JASMY","JTO","JUP",
    "KAIA","KAS","LDO","LINK","LTC","MANA","MNT","NEAR","ONDO","OP",
    "PENDLE","PENGU","PNUT","POL","RAY","RENDER","RUNE","SAND","SEI",
    "STRK","STX","SUI","TAO","TIA","TRUMP","TRX","UNI","VET","WIF",
    "WLD","XLM","XMR","XTZ","ZEC","SHIB","BONK","PEPE","TURBO","MOVE"
]

def send_tg(text):
    try:
        r = requests.post(TG_URL, json={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML"
        }, timeout=10)
        result = r.json()
        if not result.get("ok"):
            logger.error(f"TG error: {result}")
        else:
            logger.info("Message sent OK")
    except Exception as e:
        logger.error(f"TG error: {e}")

def get_prices():
    """Используем CoinCap API - работает без блокировок"""
    try:
        # Сначала получаем BTC
        btc_r = requests.get(
            "https://api.coincap.io/v2/assets/bitcoin",
            timeout=15
        )
        btc_data = btc_r.json()
        btc_change = float(btc_data["data"]["changePercent24Hr"])
        btc_price = float(btc_data["data"]["priceUsd"])

        # Получаем топ 200 монет
        r = requests.get(
            "https://api.coincap.io/v2/assets",
            params={"limit": 200},
            timeout=15
        )
        data = r.json()

        prices = {"BTC": {"price": btc_price, "change": btc_change}}

        for asset in data["data"]:
            sym = asset["symbol"].upper()
            if sym in UPSCALE_SYMBOLS:
                try:
                    prices[sym] = {
                        "price": float(asset["priceUsd"]),
                        "change": float(asset["changePercent24Hr"])
                    }
                except:
                    pass

        logger.info(f"Got {len(prices)} prices from CoinCap")
        return prices

    except Exception as e:
        logger.error(f"CoinCap error: {e}")
        return None

def fmt(p):
    if p >= 1000: return f"${p:,.0f}"
    if p >= 1: return f"${p:.3f}"
    if p >= 0.01: return f"${p:.5f}"
    return f"${p:.8f}"

def scan():
    data = get_prices()
    if not data or "BTC" not in data:
        logger.warning("No data received")
        return

    btc_change = data["BTC"]["change"]
    btc_price = data["BTC"]["price"]

    candidates = []
    for sym, info in data.items():
        if sym == "BTC":
            continue
        diff = info["change"] - btc_change
        if diff >= 1.5:
            candidates.append({
                "sym": sym,
                "price": info["price"],
                "change": info["change"],
                "diff": diff
            })

    candidates.sort(key=lambda x: x["diff"], reverse=True)
    top = candidates[:3]

    if not top:
        logger.info("No candidates with decorrelation")
        return

    now = datetime.now().strftime("%H:%M")
    btc_arrow = "⬇️" if btc_change < 0 else "⬆️"
    msg = f"📊 <b>СКАН {now} МСК</b>\n\nBTC: {fmt(btc_price)} | {btc_change:+.2f}% {btc_arrow}\n{'─'*22}\n\n"

    medals = ["🥇", "🥈", "🥉"]
    for i, c in enumerate(top):
        price = c["price"]
        stop = price * 0.96
        tp1 = price * 1.06
        tp2 = price * 1.10
        sig = "🔥" if c["diff"] >= 4 else "⚡" if c["diff"] >= 2.5 else "👀"

        msg += f"{medals[i]} <b>{c['sym']}/USD</b> {sig} +{c['diff']:.1f}% vs BTC\n"
        msg += f"📍 Вход: {fmt(price)}\n"
        msg += f"🛑 Стоп: {fmt(stop)}\n"
        msg += f"🎯 TP1: {fmt(tp1)} | TP2: {fmt(tp2)}\n\n"

    msg += "💡 Входи в 1-2 лучших\nЕсли один против — выходишь, второй держишь"

    send_tg(msg)
    logger.info(f"Signal sent: {[c['sym'] for c in top]}")

def main():
    logger.info("Upscale Signal Bot started!")
    send_tg("🤖 <b>Upscale Signal Bot запущен!</b>\nСканирую каждые 15 минут...\nИсточник: CoinCap API")

    while True:
        try:
            logger.info("Running scan...")
            scan()
        except Exception as e:
            logger.error(f"Scan error: {e}")
        time.sleep(900)

if __name__ == "__main__":
    main()
