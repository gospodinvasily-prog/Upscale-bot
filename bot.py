# -*- coding: utf-8 -*-
"""
Бот v13.0 — Prophet V12-FINAL + Anomaly Scanner A+
===================================================
Стратегии:
  1. Prophet V12-FINAL (Multi-TF Scanner + Counter-trend TP + Bear-aware)
     - Скан в :30 каждого часа из AUTO_HOURS_MSK
     - Подтверждение через 15m свечу (:45)
     - MAX_OPEN=5, counter-trend TP, per-side daily stop
  2. Anomaly Scanner A+ (5 аномалий)
     - Скан в :50 каждого часа (24 скана/день)
     - MAX_OPEN=2, 24ч кулдаун по паре
     - 5 аномалий: Laggard, ZScoreDev, Divergence, LiqMagnet, Exhaustion
  3. Big Candle Strategy (BTC скальпер)
     - Окно 10:00-11:15 МСК, 15m свечи

Команды:
  /up — статус счёта
  /hist — история сделок
  /risk — риск-менеджмент
  /closeall — закрыть ВСЕ позиции
  /halt — пауза (новые входы заблокированы)
  /resume — снять паузу
  
  Prophet:
  /prophet — статус
  /prophet_on — включить
  /prophet_off — выключить
  /prophet_close — закрыть позиции Prophet
  /prophet_clean — cleaner зависших
  
  Anomaly:
  /anomaly — статус
  /anomaly_on — включить
  /anomaly_off — выключить
  /anomaly_close — закрыть позиции Anomaly
  /anomaly_clean — cleaner зависших
  
  Big Candle:
  /bc — статус
  /bc_on /bc_off — вкл/выкл

Зависимости: requests, upscale_exec.py, prophet.py, anomaly_scanner.py, big_candle_scanner.py
"""

import os, re, csv, html, math, time, threading, traceback, json, requests
from datetime import datetime, timezone, timedelta

# ─── CONFIG ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID        = "426470592"
BOT_VERSION    = "v13.0"
MSK = timezone(timedelta(hours=3))

# Риск по умолчанию (Prophet/Anomaly передают свой риск в сигнале)
RISK_USD    = float(os.environ.get("RISK_USD", "20"))
MAX_POS_USD = float(os.environ.get("MAX_POS_USD", "3000"))

# BTC VWAP (справочно)
BTC_VWAP_MARGIN_PCT = float(os.environ.get("BTC_VWAP_MARGIN_PCT", "0.2"))
VWAP_WINDOW_BARS    = 97

# Лимит запросов к Gate API
API_RATE_PER_SEC = 12
GATE = "https://api.gateio.ws/api/v4/futures/usdt"

# Файлы
_DEFAULT_DIR = os.environ.get("BOT_DATA_DIR", os.environ.get("LOG_DIR", "."))
LOG_DIR  = os.environ.get("LOG_DIR", _DEFAULT_DIR)
try: os.makedirs(LOG_DIR, exist_ok=True)
except: pass
EXEC_CSV = os.path.join(LOG_DIR, "exec_dry_v8.csv")

# ─── UPSCALE PAIRS ────────────────────────────────────────────────────────────
EXCLUDE_SYMBOLS = {x.strip().upper() for x in
                   (os.environ.get("EXCLUDE_SYMBOLS") or "").split(",") if x.strip()}

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
if EXCLUDE_SYMBOLS:
    UPSCALE_PAIRS = [p for p in UPSCALE_PAIRS if p.upper() not in EXCLUDE_SYMBOLS]

# ─── ИМПОРТ СТРАТЕГИЙ ────────────────────────────────────────────────────────
import upscale_exec
try:
    import big_candle_scanner as BCS
    _BCS_IMPORT_ERR = None
except Exception as e:
    BCS = None
    _BCS_IMPORT_ERR = str(e)
try:
    import prophet as PP
    _PP_IMPORT_ERR = None
except Exception as e:
    PP = None
    _PP_IMPORT_ERR = str(e)
try:
    import anomaly_scanner as ANOM
    _ANOM_IMPORT_ERR = None
except Exception as e:
    ANOM = None
    _ANOM_IMPORT_ERR = str(e)

# ─── УТИЛИТЫ ──────────────────────────────────────────────────────────────────
def esc(s): return html.escape(str(s), quote=False)
def fnum(x, default=0.0):
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except: return default

class RateLimiter:
    def __init__(self, rate):
        self.interval = 1.0 / rate
        self.lock = threading.Lock()
        self.next_ts = 0.0
    def wait(self):
        with self.lock:
            now = time.monotonic()
            if self.next_ts < now: self.next_ts = now
            delay = self.next_ts - now
            self.next_ts += self.interval
        if delay > 0: time.sleep(delay)

LIMITER = RateLimiter(API_RATE_PER_SEC)

def api_get(path, params, timeout=8):
    url = f"{GATE}/{path}"
    for attempt in range(2):
        LIMITER.wait()
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 429: time.sleep(1.5); continue
            if r.status_code != 200: return None
            return r.json()
        except Exception as e:
            if attempt == 1: print(f"[API ERROR] {path}: {e}")
    return None

# ─── TELEGRAM ─────────────────────────────────────────────────────────────────
def _escape_stray(text):
    out, i, n = [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "&":
            m = re.match(r"&(amp|lt|gt|quot|#\d+);", text[i:])
            out.append(text[i:i+m.end()] if m else "&amp;")
            i += m.end() if m else 1; continue
        if ch == "<":
            m = re.match(r"</?([a-zA-Z0-9]+)(\s[^<>]*)?/?>", text[i:])
            if m and m.group(1).lower() in ("b","i","u","s","code","pre","a"):
                out.append(text[i:i+m.end()]); i += m.end(); continue
            out.append("&lt;"); i += 1; continue
        out.append(ch); i += 1
    return "".join(out)

def _balance_html(text):
    text = _escape_stray(text)
    for tag in ("b","i","code","pre","u","s"):
        opened = text.count(f"<{tag}>") - text.count(f"</{tag}>")
        if opened > 0: text += f"</{tag}>" * opened
        elif opened < 0: text = f"<{tag}>" * (-opened) + text
    return text

def send_telegram(text):
    text = _balance_html(text)
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
                                     "disable_web_page_preview": True}, timeout=10)
        r.raise_for_status()
    except Exception as e:
        print(f"[TG ERROR] {e}")

def send_document(path, caption=""):
    if not os.path.exists(path) or os.path.getsize(path) == 0: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    try:
        with open(path, "rb") as f:
            r = requests.post(url, data={"chat_id": CHAT_ID, "caption": caption},
                              files={"document": (os.path.basename(path), f)}, timeout=30)
            r.raise_for_status()
    except Exception as e:
        print(f"[TG DOC ERROR] {path}: {e}")

# ─── СВЕЧИ ────────────────────────────────────────────────────────────────────
def parse_candles(raw):
    out = []
    if not isinstance(raw, list): return out
    for c in raw:
        try:
            out.append({"t": int(c["t"]), "o": float(c["o"]), "h": float(c["h"]),
                        "l": float(c["l"]), "c": float(c["c"]), "v": fnum(c.get("v", 0))})
        except: continue
    out.sort(key=lambda x: x["t"])
    return out

def get_candles(symbol, interval, limit):
    return parse_candles(api_get("candlesticks", {"contract": f"{symbol}_USDT", "interval": interval, "limit": limit}))

# ─── BTC VWAP (справочно) ─────────────────────────────────────────────────────
def vwap_rolling(candles):
    seg = candles[-VWAP_WINDOW_BARS:]
    vol = sum(c["v"] for c in seg)
    if not seg or vol <= 0: return None
    return sum((c["h"]+c["l"]+c["c"])/3 * c["v"] for c in seg) / vol

def btc_bias(now_ts=None):
    now_ts = now_ts or time.time()
    raw = get_candles("BTC", "15m", VWAP_WINDOW_BARS + 8)
    closed = [c for c in raw if c["t"] + 900 <= now_ts]
    price = raw[-1]["c"] if raw else 0.0
    if len(closed) < 40: return {"bias": None, "dev": 0.0, "price": price, "vwap": None}
    vw = vwap_rolling(closed)
    if not vw: return {"bias": None, "dev": 0.0, "price": price, "vwap": None}
    dev = (closed[-1]["c"] - vw) / vw * 100
    bias = "long" if dev > BTC_VWAP_MARGIN_PCT else "short" if dev < -BTC_VWAP_MARGIN_PCT else "neutral"
    return {"bias": bias, "dev": dev, "price": price, "vwap": vw}

def btc_line(b):
    if not b or b.get("bias") is None: return "₿ BTC: данных нет"
    ico = {"long":"🟢 лонг","short":"🔴 шорт","neutral":"⚪ нейтраль"}[b["bias"]]
    side = "выше" if b["dev"] >= 0 else "ниже"
    return f"₿ BTC VWAP: {ico} ({abs(b['dev']):.2f}% {side} VWAP)"

# ─── CSV ──────────────────────────────────────────────────────────────────────
def _append_csv(path, row):
    try:
        header = ",".join(row.keys())
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                first = f.readline().strip()
            if first != header: os.replace(path, f"{path}.old-{int(time.time())}")
        new = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(row.keys()))
            if new: w.writeheader()
            w.writerow(row)
    except Exception as e:
        print(f"[CSV ERROR] {path}: {e}")

# ─── EXECUTOR ─────────────────────────────────────────────────────────────────
EXECUTOR = upscale_exec.Executor(send_telegram, lambda row: _append_csv(EXEC_CSV, row),
                                 RISK_USD, MAX_POS_USD, UPSCALE_PAIRS + ["BTC"])

# ─── TELEGRAM COMMANDS ────────────────────────────────────────────────────────
TG_OFFSET = [0]
TG_READ = {"fails": 0, "quiet_until": 0.0, "conflicts": 0}

def tg_drop_webhook():
    if not TELEGRAM_TOKEN: return
    try:
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook",
                         params={"drop_pending_updates": "false"}, timeout=10)
        if r.ok and r.json().get("ok"):
            print("[TG] webhook снят")
    except: pass

def tg_updates():
    if not TELEGRAM_TOKEN or time.time() < TG_READ["quiet_until"]: return []
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    try:
        r = requests.get(url, params={"offset": TG_OFFSET[0] + 1, "timeout": 0, "limit": 20}, timeout=10)
        r.raise_for_status()
        data = r.json()
        TG_READ["fails"] = 0; TG_READ["conflicts"] = 0
        out = []
        for u in data.get("result", []) if data.get("ok") else []:
            TG_OFFSET[0] = max(TG_OFFSET[0], u.get("update_id", 0))
            cb = u.get("callback_query")
            if cb:
                cb_data = cb.get("data", "")
                if cb_data.startswith("prophet_") and PP is not None:
                    try: PP.on_callback(cb_data)
                    except: pass
                try:
                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
                                  json={"callback_query_id": cb.get("id", "")}, timeout=5)
                except: pass
                continue
            msg = u.get("message") or u.get("channel_post") or {}
            if str(msg.get("chat", {}).get("id")) != str(CHAT_ID): continue
            text = (msg.get("text") or "").strip()
            if text: out.append(text)
        return out
    except requests.HTTPError as e:
        if getattr(e.response, "status_code", 0) == 409:
            TG_READ["conflicts"] += 1
            TG_READ["quiet_until"] = time.time() + 60
        return []
    except Exception as e:
        TG_READ["fails"] += 1
        if TG_READ["fails"] > 3:
            TG_READ["quiet_until"] = time.time() + 600
            TG_READ["fails"] = 0
        return []

HELP_TEXT = (
    "<b>📊 СТАТИСТИКА</b>\n"
    "/up — статус счёта + все стратегии\n"
    "/status — обзор всех стратегий\n"
    "/hist — история сделок\n"
    "/risk — риск-менеджмент\n"
    "/btc — уклон BTC\n"
    "\n<b>⚙️ УПРАВЛЕНИЕ</b>\n"
    "/halt — пауза (новые входы заблокированы)\n"
    "/resume — снять паузу\n"
    "/closeall — закрыть ВСЕ позиции\n"
    "\n<b>🔮 PROPHET V12-FINAL</b>\n"
    "/prophet — статус\n"
    "/prophet_on — включить\n"
    "/prophet_off — выключить\n"
    "/prophet_close — закрыть позиции Prophet\n"
    "/prophet_clean — cleaner зависших\n"
    "\n<b>🚨 ANOMALY SCANNER A+</b>\n"
    "/anomaly — статус\n"
    "/anomaly_on — включить\n"
    "/anomaly_off — выключить\n"
    "/anomaly_close — закрыть позиции Anomaly\n"
    "/anomaly_clean — cleaner зависших\n"
    "\n<b>🚨 BIG CANDLE (BTC скальпер)</b>\n"
    "/bc — статус\n"
    "/bc_on — включить\n"
    "/bc_off — выключить\n"
    "/bc_close — закрыть позицию\n"
    "/bc_stats — статистика\n"
)

def _all_strategies_status():
    """Общий обзор всех стратегий: статус, открытые сделки, PnL."""
    lines = ["", "━━━ 📊 Обзор стратегий ━━━"]
    
    # Prophet
    if PP is not None:
        try:
            pp_mode = PP._state.get("scan_mode", "off")
            pp_status = "🟢 ВКЛ" if pp_mode == "auto" else "🔴 ВЫКЛ"
            pp_tracked = PP._load_tracked()
            pp_open = [(v.get("sym"), v.get("direction", v.get("dir","?")), v.get("entry",0)) 
                       for v in pp_tracked.values() 
                       if v.get("status") == "open" and not any(k.startswith("pending_") for k in [k for k,v2 in pp_tracked.items() if v2 == v])]
            pp_open_clean = []
            for pid, v in pp_tracked.items():
                if v.get("status") == "open":
                    pp_open_clean.append((v.get("sym"), v.get("direction", v.get("dir","?")), v.get("entry",0)))
            lines.append(f"\n🔮 Prophet V12: {pp_status} | открыто: {len(pp_open_clean)}/{PP.MAX_OPEN}")
            lines.append(f"   сканов: {PP._stats['scans']} | сигналов: {PP._stats['signals']} | сделок: {PP._stats['trades']}")
            if PP._stats["trades"] > 0:
                wr = PP._stats['wins']/PP._stats['trades']*100
                lines.append(f"   WR: {wr:.0f}% | PnL: ${PP._stats['pnl']:+.2f}")
            for sym, dir_v, entry in pp_open_clean:
                emoji = "🟢" if dir_v in ("LONG","long") else "🔴"
                lines.append(f"   {emoji} {sym} @ ${entry:.4g}")
        except Exception as e:
            lines.append(f"\n🔮 Prophet: ⚠️ {e}")
    else:
        lines.append(f"\n🔮 Prophet: ❌ не загружен")
    
    # Anomaly
    if ANOM is not None:
        try:
            an_mode = ANOM._state.get("scan_mode", "off")
            an_status = "🟢 ВКЛ" if an_mode == "auto" else "🔴 ВЫКЛ"
            an_tracked = ANOM._load_tracked()
            an_open_clean = []
            for pid, v in an_tracked.items():
                if v.get("status") == "open":
                    an_open_clean.append((v.get("sym"), v.get("direction", v.get("dir","?")), v.get("entry",0), v.get("n_anomalies",0)))
            lines.append(f"\n🚨 Anomaly A+: {an_status} | открыто: {len(an_open_clean)}/{ANOM.MAX_OPEN}")
            lines.append(f"   сканов: {ANOM._stats['scans']} | сигналов: {ANOM._stats['signals']} | сделок: {ANOM._stats['trades']}")
            if ANOM._stats["trades"] > 0:
                wr = ANOM._stats['wins']/ANOM._stats['trades']*100
                lines.append(f"   WR: {wr:.0f}% | PnL: ${ANOM._stats['pnl']:+.2f}")
            for sym, dir_v, entry, n_anom in an_open_clean:
                emoji = "🟢" if dir_v in ("LONG","long") else "🔴"
                lines.append(f"   {emoji} {sym} @ ${entry:.4g} ({n_anom}/5)")
        except Exception as e:
            lines.append(f"\n🚨 Anomaly: ⚠️ {e}")
    else:
        lines.append(f"\n🚨 Anomaly: ❌ не загружен")
    
    # Big Candle
    if BCS is not None:
        try:
            bc_enabled = BCS._state.get("enabled", False)
            bc_status = "🟢 ВКЛ" if bc_enabled else "🔴 ВЫКЛ"
            bc_tracked = BCS._load_tracked()
            bc_open = sum(1 for v in bc_tracked.values() if v.get("status") == "open")
            lines.append(f"\n🚨 Big Candle: {bc_status} | открыто: {bc_open}")
            lines.append(f"   сигналов: {BCS._stats['signals_total']} | сделок: {BCS._stats['trades_opened']}/{BCS._stats['trades_closed']}")
        except Exception as e:
            lines.append(f"\n🚨 Big Candle: ⚠️ {e}")
    else:
        lines.append(f"\n🚨 Big Candle: ❌ не загружен")
    
    # Общий итог
    total_open = 0
    if PP is not None:
        try:
            pp_tr = PP._load_tracked()
            total_open += sum(1 for v in pp_tr.values() if v.get("status") == "open" and not any(k.startswith("pending_") for k in [k for k,v2 in pp_tr.items() if v2 == v]))
        except: pass
    if ANOM is not None:
        try:
            an_tr = ANOM._load_tracked()
            total_open += sum(1 for v in an_tr.values() if v.get("status") == "open" and not any(k.startswith("pending_") for k in [k for k,v2 in an_tr.items() if v2 == v]))
        except: pass
    if BCS is not None:
        try:
            bc_tr = BCS._load_tracked()
            total_open += sum(1 for v in bc_tr.values() if v.get("status") == "open")
        except: pass
    
    lines.append(f"\n📊 Всего открыто позиций: {total_open}")
    lines.append(f"🤖 EXECUTOR halted: {'да' if EXECUTOR.halted else 'нет'}")
    
    return "\n".join(lines)


def handle_command(text):
    parts = text.replace(",", ".").split()
    if not parts or not parts[0].startswith("/"): return ""
    cmd = parts[0].lower().split("@")[0]
    arg = parts[1] if len(parts) > 1 else ""

    if cmd == "/up":
        return EXECUTOR.status() + "\n\n" + _all_strategies_status()
    if cmd == "/status":
        return _all_strategies_status()
    if cmd == "/btc":
        b = btc_bias()
        return btc_line(b) + (f"\nVWAP {b['vwap']:.2f}, цена {b['price']:.2f}" if b.get("vwap") else "")
    if cmd in ("/hist", "/history", "/trades"):
        days = int(arg) if arg.isdigit() else 7
        return EXECUTOR.history(max(1, min(60, days)))
    if cmd == "/risk":
        return EXECUTOR.risk_summary()
    if cmd == "/closeall":
        return EXECUTOR.closeall()
    if cmd == "/halt":
        return EXECUTOR.halt()
    if cmd == "/resume":
        return EXECUTOR.resume()
    if cmd in ("/log", "/files"):
        today = datetime.now(MSK).strftime("%Y-%m-%d")
        if os.path.exists(EXEC_CSV) and os.path.getsize(EXEC_CSV) > 0:
            send_document(EXEC_CSV, f"{today} — авто-слой")
            return "📒 Отправлено"
        return "📒 Журналы пустые"

    # ─── Prophet V12-FINAL ──────────────────────────────────────────────────
    if cmd == "/prophet":
        if PP is None: return f"⚠️ Prophet не загружен: {_PP_IMPORT_ERR}"
        return PP.cmd_status()
    if cmd == "/prophet_on":
        if PP is None: return "⚠️ Prophet не загружен"
        return PP.cmd_on()
    if cmd == "/prophet_off":
        if PP is None: return "⚠️ Prophet не загружен"
        return PP.cmd_off()
    if cmd == "/prophet_close":
        if PP is None: return "⚠️ Prophet не загружен"
        return PP.cmd_close()
    if cmd == "/prophet_clean":
        if PP is None: return "⚠️ Prophet не загружен"
        return PP.cmd_clean()

    # ─── Anomaly Scanner A+ ──────────────────────────────────────────────────
    if cmd == "/anomaly":
        if ANOM is None: return f"⚠️ Anomaly не загружен: {_ANOM_IMPORT_ERR}"
        return ANOM.cmd_status()
    if cmd == "/anomaly_on":
        if ANOM is None: return "⚠️ Anomaly не загружен"
        return ANOM.cmd_on()
    if cmd == "/anomaly_off":
        if ANOM is None: return "⚠️ Anomaly не загружен"
        return ANOM.cmd_off()
    if cmd == "/anomaly_close":
        if ANOM is None: return "⚠️ Anomaly не загружен"
        return ANOM.cmd_close()
    if cmd == "/anomaly_clean":
        if ANOM is None: return "⚠️ Anomaly не загружен"
        return ANOM.cmd_clean()

    # ─── Big Candle ──────────────────────────────────────────────────────────
    if cmd == "/bc":
        if BCS is None: return f"⚠️ Big Candle не загружен: {_BCS_IMPORT_ERR}"
        return BCS.status_report()
    if cmd == "/bc_on":
        if BCS is None: return "⚠️ BC не загружен"
        return BCS.cmd_on()
    if cmd == "/bc_off":
        if BCS is None: return "⚠️ BC не загружен"
        return BCS.cmd_off()
    if cmd == "/bc_close":
        if BCS is None: return "⚠️ BC не загружен"
        return BCS.cmd_close()
    if cmd == "/bc_stats":
        if BCS is None: return "⚠️ BC не загружен"
        return BCS.cmd_stats()

    if cmd in ("/help", "/start"):
        return HELP_TEXT
    return ""

def poll_commands():
    for text in tg_updates():
        try:
            answer = handle_command(text)
        except Exception as e:
            traceback.print_exc()
            answer = f"Ошибка: {esc(str(e))}"
        if answer:
            print(f"[CMD] {text}")
            send_telegram(answer)

# ─── SHUTDOWN ─────────────────────────────────────────────────────────────────
import signal as _signal
def _on_shutdown(signum, frame):
    print(f"[SHUTDOWN] сигнал {signum}")
    try:
        send_telegram("♻️ <b>Бот перезапускается</b> — отправляю журналы.")
        if os.path.exists(EXEC_CSV) and os.path.getsize(EXEC_CSV) > 0:
            send_document(EXEC_CSV, f"{datetime.now(MSK).strftime('%Y-%m-%d %H:%M')} — авто-слой")
    except: pass
    raise SystemExit(0)

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    tg_drop_webhook()
    _signal.signal(_signal.SIGTERM, _on_shutdown)
    _signal.signal(_signal.SIGINT, _on_shutdown)

    _data_dir_actual = os.environ.get("BOT_DATA_DIR", "/tmp")
    _disk_ok = "✅ Persistent Disk" if _data_dir_actual != "/tmp" else "⚠️ /tmp"

    start_lines = [
        f"🚀 <b>Upscale Bot {BOT_VERSION}</b> | режим: <b>{EXECUTOR.mode}</b>",
        "",
        f"📊 Пар: {len(UPSCALE_PAIRS)} | 💾 {_disk_ok}",
        "🔮 Prophet V12-FINAL: /prophet_on для включения",
        "🚨 Anomaly Scanner A+: /anomaly_on для включения",
        "🚨 Big Candle: окно 10:00-11:15 МСК, /bc_on",
        "💬 /help — все команды",
    ]
    send_telegram("\n".join(start_lines))

    # Запуск Big Candle
    if BCS is not None:
        try: BCS.start()
        except Exception as e:
            print(f"[BCS] ошибка: {e}")
            send_telegram(f"⚠️ Big Candle: {esc(str(e))}")

    # Запуск Prophet
    if PP is not None:
        try: PP.start()
        except Exception as e:
            print(f"[PP] ошибка: {e}")
            send_telegram(f"⚠️ Prophet: {esc(str(e))}")

    # Запуск Anomaly
    if ANOM is not None:
        try: ANOM.start()
        except Exception as e:
            print(f"[ANOM] ошибка: {e}")
            send_telegram(f"⚠️ Anomaly: {esc(str(e))}")

    # Команды в чате — в отдельном потоке
    def _command_loop():
        while True:
            try: poll_commands()
            except Exception as e: print(f"[CMD LOOP ERROR] {e}")
            time.sleep(2)
    threading.Thread(target=_command_loop, daemon=True, name="tg-commands").start()

    # Главный цикл (больше не сканирует Donchian — только ждёт)
    while True:
        try:
            time.sleep(5)
        except Exception as e:
            print(f"[LOOP ERROR] {e}")

if __name__ == "__main__":
    main()
