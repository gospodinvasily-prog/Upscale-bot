
"""
Бот v11.0 — автоматическая торговля стратегией Donchian Regime v4.4 на Upscale.

Старая воронка ЗАРЯД → ПРОБОЙ → ИМПУЛЬС → УКЛОН (ручной дискреционный вход по
сигналам в Telegram) полностью убрана — она давала минус на демо-счету.
Теперь единственная стратегия — donchian_regime_v42 (exec_donchian_regime_v42.py):
полностью автоматическая, без ручных команд входа/выхода.

Что делает этот файл:
  - инфраструктура: Gate.io REST API, Telegram, CSV-журнал авто-слоя
  - EXECUTOR (upscale_exec.py) — реальное исполнение ордеров на Upscale (demo/dry)
  - раз в сутки (после закрытия дневной свечи UTC) запускает
    exec_donchian_regime_v42.check_signals() — сканирует пары, шлёт сигналы в EXECUTOR
  - команды в чате: /up /btc /hist /risk /riskraw /uptest /closeall /halt /resume /log /help
  - диспетчер бэктестов: RUN_BACKTEST=<mode> запускает соответствующий bt_*.py один раз

Зависимости: requests, upscale_exec.py, exec_donchian_regime_v42.py.
Переменные окружения: TELEGRAM_TOKEN, LOG_DIR (необязательно).
"""

import os
import re
import csv
import signal as _signal
import html
import math
import time
import threading
import traceback
import json
import requests
import upscale_exec
import exec_donchian_regime_v42 as DR42
from datetime import datetime, timezone, timedelta

# ─── CONFIG ───────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID        = "426470592"
BOT_VERSION    = "v11.0"

MSK = timezone(timedelta(hours=3))

GATE = "https://api.gateio.ws/api/v4/futures/usdt"

# ── Риск по умолчанию для EXECUTOR (donchian передаёт свой риск в каждом
# сигнале — compound sizing от текущего equity Upscale, см.
# exec_donchian_regime_v42.py — эти значения остаются только как запасной
# дефолт конструктора Executor, реально по ним торговли не бывает) ──
RISK_USD      = float(os.environ.get("RISK_USD", "20"))
MAX_POS_USD   = float(os.environ.get("MAX_POS_USD", "3000"))

# ── BTC VWAP — используется только командой /btc (информационная справка,
# не влияет на торговлю — donchian сам считает режим BTC по SMA(50)) ──
BTC_VWAP_MARGIN_PCT = float(os.environ.get("BTC_VWAP_MARGIN_PCT", "0.2"))
VWAP_WINDOW_BARS    = 97      # ≈ сутки 15м свечей

# ── Когда запускать скан сигналов donchian (раз в сутки, после закрытия
# дневной свечи Gate.io по UTC). Небольшой буфер, чтобы свеча точно закрылась
# и успела обновиться. ──
DR42_CHECK_HOUR_UTC = int(os.environ.get("DR42_CHECK_HOUR_UTC", "0"))
DR42_CHECK_MIN_UTC  = int(os.environ.get("DR42_CHECK_MIN_UTC", "10"))

# ── Сеть ──
API_RATE_PER_SEC = 12      # общий лимит запросов к Gate (с запасом к публичному лимиту)

# ── Логирование авто-слоя ──
LOG_DIR  = os.environ.get("LOG_DIR", ".")
EXEC_CSV = os.path.join(LOG_DIR, "exec_dry_v8.csv")    # что сделал (или бы сделал) авто-слой

# ─── UPSCALE PAIRS ────────────────────────────────────────────────────────────

# Монеты, которые не торгуем. Задаётся переменной EXCLUDE_SYMBOLS через запятую,
# например EXCLUDE_SYMBOLS=ENA,POPCAT. Кода менять не надо.
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

# ─── ГЛОБАЛЬНОЕ СОСТОЯНИЕ ────────────────────────────────────────────────────

TG_OFFSET = [0]               # id последнего прочитанного сообщения в Telegram

# ─── УТИЛИТЫ ──────────────────────────────────────────────────────────────────

def esc(s) -> str:
    return html.escape(str(s), quote=False)

def fnum(x, default=0.0) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default

def pct(a: float, b: float) -> float:
    """Изменение от a к b в %."""
    return (b - a) / a * 100 if a else 0.0

def msk_time_str(ts=None) -> str:
    d = datetime.fromtimestamp(ts, MSK) if ts else datetime.now(MSK)
    return d.strftime("%H:%M МСК")

class RateLimiter:
    """Равномерно распределяет запросы между потоками."""
    def __init__(self, rate: float):
        self.interval = 1.0 / rate
        self.lock = threading.Lock()
        self.next_ts = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            if self.next_ts < now:
                self.next_ts = now
            delay = self.next_ts - now
            self.next_ts += self.interval
        if delay > 0:
            time.sleep(delay)

LIMITER = RateLimiter(API_RATE_PER_SEC)

def api_get(path: str, params: dict, timeout: int = 8):
    url = f"{GATE}/{path}"
    for attempt in range(2):
        LIMITER.wait()
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                time.sleep(1.5)
                continue
            if r.status_code != 200:
                return None
            return r.json()
        except Exception as e:
            if attempt == 1:
                print(f"[API ERROR] {path} {params.get('contract', '')}: {e}")
    return None

# ─── TELEGRAM ─────────────────────────────────────────────────────────────────

def send_telegram(text: str):
    # Экранируем ПЕРЕД отправкой: любой одиночный "<" (например, из текста
    # ошибки HTTP, в котором могла прилететь HTML-страница 502/503 от
    # Upscale) иначе ловит 400 "can't parse entities", и сообщение тихо
    # не доходит — а это бьёт и по самым важным алертам (аварийная защита,
    # close-all не принят). Раньше это экранирование было только в
    # send_blocks(), а send_telegram() вызывается напрямую из upscale_exec.py
    # и exec_donchian_regime_v42.py почти везде.
    text = _balance_html(text)
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
                                     "disable_web_page_preview": True}, timeout=10)
        r.raise_for_status()
    except Exception as e:
        print(f"[TG ERROR] {e}")

def send_document(path: str, caption: str = ""):
    """Отправляет файл в Telegram. На Render диск стирается при каждом деплое —
    так статистика сохраняется у пользователя в чате."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    try:
        with open(path, "rb") as f:
            r = requests.post(url, data={"chat_id": CHAT_ID, "caption": caption},
                              files={"document": (os.path.basename(path), f)}, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print(f"[TG DOC ERROR] {path}: {e}")

_HTML_OK = ("b", "i", "u", "s", "code", "pre", "a")

def _escape_stray(text: str) -> str:
    """Экранирует «<» и «&», которые НЕ являются частью разрешённого тега.
    Telegram отвечает 400 «can't parse entities», если встретит одиночный «<»."""
    out, i, n = [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "&":
            m = re.match(r"&(amp|lt|gt|quot|#\d+);", text[i:])
            out.append(text[i:i + m.end()] if m else "&amp;")
            i += m.end() if m else 1
            continue
        if ch == "<":
            m = re.match(r"</?([a-zA-Z0-9]+)(\s[^<>]*)?/?>", text[i:])
            if m and m.group(1).lower() in _HTML_OK:
                out.append(text[i:i + m.end()])
                i += m.end()
                continue
            out.append("&lt;")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)

def _balance_html(text: str) -> str:
    """Закрывает теги, оставшиеся открытыми в куске (при разбиении длинного
    сообщения на части Telegram ломается на непарных тегах)."""
    text = _escape_stray(text)
    for tag in ("b", "i", "code", "pre", "u", "s"):
        opened = text.count(f"<{tag}>") - text.count(f"</{tag}>")
        if opened > 0:
            text += f"</{tag}>" * opened
        elif opened < 0:
            text = f"<{tag}>" * (-opened) + text
    return text

def send_blocks(blocks: list, limit: int = 3800):
    """Telegram режет сообщения > 4096 символов — собираем блоки в пачки."""
    buf = ""
    for b in blocks:
        while len(b) > limit:
            cut = b.rfind(" ", 0, limit) or limit
            if buf:
                send_telegram(_balance_html(buf))
                buf = ""
            send_telegram(_balance_html(b[:cut]))
            b = b[cut:].lstrip()
        if buf and len(buf) + len(b) + 1 > limit:
            send_telegram(_balance_html(buf))
            buf = ""
        buf = f"{buf}\n{b}" if buf else b
    if buf:
        send_telegram(_balance_html(buf))

# ─── СВЕЧИ ────────────────────────────────────────────────────────────────────

def parse_candles(raw) -> list:
    out = []
    if not isinstance(raw, list):
        return out
    for c in raw:
        try:
            out.append({"t": int(c["t"]), "o": float(c["o"]), "h": float(c["h"]),
                        "l": float(c["l"]), "c": float(c["c"]), "v": fnum(c.get("v", 0))})
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda x: x["t"])
    return out

def split_closed(candles: list, interval_sec: int, now_ts: float):
    """Закрытые свечи определяем по времени, а не по позиции [-2]."""
    closed = [c for c in candles if c["t"] + interval_sec <= now_ts]
    forming = candles[-1] if candles and candles[-1]["t"] + interval_sec > now_ts else None
    price = candles[-1]["c"] if candles else 0.0
    return closed, forming, price

def get_candles(symbol: str, interval: str, limit: int) -> list:
    return parse_candles(api_get("candlesticks", {"contract": f"{symbol}_USDT",
                                                  "interval": interval, "limit": limit}))

# ─── BTC VWAP (справочная команда /btc — не используется стратегией) ────────

def vwap_rolling(candles: list):
    """VWAP по последним VWAP_WINDOW_BARS закрытым 15м свечам (≈ сутки)."""
    seg = candles[-VWAP_WINDOW_BARS:]
    vol = sum(c["v"] for c in seg)
    if not seg or vol <= 0:
        return None
    return sum((c["h"] + c["l"] + c["c"]) / 3 * c["v"] for c in seg) / vol

def btc_bias(now_ts=None) -> dict:
    """Уклон BTC: цена последней ЗАКРЫТОЙ 15м свечи относительно своего VWAP.
    Чисто информационная справка для /btc — реальный режим, который
    использует стратегия donchian, это BTC SMA(50) на дневных свечах."""
    now_ts = now_ts or time.time()
    raw = get_candles("BTC", "15m", VWAP_WINDOW_BARS + 8)
    closed, _, price = split_closed(raw, 900, now_ts)
    if len(closed) < 40:
        return {"bias": None, "dev": 0.0, "price": price, "vwap": None}
    vw = vwap_rolling(closed)
    if not vw:
        return {"bias": None, "dev": 0.0, "price": price, "vwap": None}
    dev = (closed[-1]["c"] - vw) / vw * 100
    m = BTC_VWAP_MARGIN_PCT
    bias = "long" if dev > m else "short" if dev < -m else "neutral"
    return {"bias": bias, "dev": dev, "price": price, "vwap": vw}

def btc_line(b: dict) -> str:
    if not b or b.get("bias") is None:
        return "₿ BTC: данных нет"
    ico = {"long": "🟢 лонг", "short": "🔴 шорт", "neutral": "⚪ нейтраль"}[b["bias"]]
    side = "выше" if b["dev"] >= 0 else "ниже"
    return (f"₿ BTC VWAP-уклон: {ico} ({abs(b['dev']):.2f}% {side} VWAP, "
            f"запас {BTC_VWAP_MARGIN_PCT}%)")

# ─── CSV ──────────────────────────────────────────────────────────────────────

def _append_csv(path: str, row: dict):
    try:
        header = ",".join(row.keys())
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                first = f.readline().strip()
            if first != header:   # колонки изменились (новая версия) — старый файл в архив
                os.replace(path, f"{path}.old-{int(time.time())}")
        new = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(row.keys()))
            if new:
                w.writeheader()
            w.writerow(row)
    except Exception as e:
        print(f"[CSV ERROR] {path}: {e}")

# ─── АВТО-СЛОЙ UPSCALE ────────────────────────────────────────────────────────
# EXECUTOR — общий исполнитель ордеров на Upscale (upscale_exec.py). Единственный
# источник сигналов для него теперь — donchian_regime_v42 (exec_donchian_regime_v42.py),
# которая передаёт риск/лимит позиции прямо в каждом сигнале (compound sizing от
# текущего equity), поэтому RISK_USD/MAX_POS_USD ниже используются только как
# запасной дефолт конструктора.
EXECUTOR = upscale_exec.Executor(send_telegram, lambda row: _append_csv(EXEC_CSV, row),
                                 RISK_USD, MAX_POS_USD, UPSCALE_PAIRS + ["BTC"])

# ─── КОМАНДЫ В ЧАТЕ ───────────────────────────────────────────────────────────

TG_READ = {"fails": 0, "quiet_until": 0.0, "conflicts": 0}

def tg_drop_webhook():
    """Если у бота стоит webhook, getUpdates не работает (ошибка 409).
    Снимаем его один раз при старте — команды в чате должны читаться."""
    if not TELEGRAM_TOKEN:
        return
    try:
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook",
                         params={"drop_pending_updates": "false"}, timeout=10)
        if r.ok and r.json().get("ok"):
            print("[TG] webhook снят (если был) — команды читаются через getUpdates")
    except Exception as e:
        print(f"[TG] не удалось снять webhook: {e}")

def tg_updates():
    """Новые сообщения из чата (короткий опрос, никаких доп. настроек не нужно)."""
    if not TELEGRAM_TOKEN or time.time() < TG_READ["quiet_until"]:
        return []
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    try:
        r = requests.get(url, params={"offset": TG_OFFSET[0] + 1, "timeout": 0, "limit": 20}, timeout=10)
        r.raise_for_status()
        data = r.json()
        TG_READ["fails"] = 0
        TG_READ["conflicts"] = 0
        out = []
        for u in data.get("result", []) if data.get("ok") else []:
            TG_OFFSET[0] = max(TG_OFFSET[0], u.get("update_id", 0))
            msg = u.get("message") or u.get("channel_post") or {}
            if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
                continue
            text = (msg.get("text") or "").strip()
            if text:
                out.append(text)
        return out
    except requests.HTTPError as e:
        # 409 = сообщения уже читает другой экземпляр бота. При деплое Render
        # ненадолго держит два контейнера — это нормально и проходит само.
        if getattr(e.response, "status_code", 0) == 409:
            TG_READ["conflicts"] += 1
            TG_READ["quiet_until"] = time.time() + 60
            if TG_READ["conflicts"] in (1, 5, 20):
                print(f"[TG] 409: команды читает другой экземпляр бота "
                      f"(обычно старый контейнер при деплое). Попытка {TG_READ['conflicts']}.")
            if TG_READ["conflicts"] == 20:
                send_telegram("⚠️ Команды в чате не читаются уже 20 минут: похоже, "
                              "запущено два экземпляра бота. Проверь, что в Render "
                              "работает только один сервис.")
        return []
    except Exception as e:
        TG_READ["fails"] += 1
        if TG_READ["fails"] <= 3:
            print(f"[TG READ ERROR] {e}")
        else:
            TG_READ["quiet_until"] = time.time() + 600
            TG_READ["fails"] = 0
            print("[TG READ] команды временно отключены на 10 мин (ошибки чтения)")
        return []

HELP_TEXT = (
    "<b>📊 СТАТИСТИКА С БИРЖИ</b>\n"
    "/hist — результаты закрытых сделок: по дням, по монетам, в единицах риска\n"
    "        <i>/hist 30 — за 30 дней (по умолчанию 7). Эти данные не теряются при перезапуске</i>\n"
    "/up — версия, режим, эквити, открытые позиции, статус donchian_regime_v42\n"
    "/risk — баланс, просадка, до лимитов\n"
    "/riskraw — то же сырым ответом API (для разбора проблем)\n"
    "/btc — уклон биткоина по VWAP (справочно; стратегия сама считает режим по SMA(50))\n"
    "\n<b>⚙️ УПРАВЛЕНИЕ</b>\n"
    "/halt — пауза: новых входов не будет, открытые позиции и их ордера остаются\n"
    "/resume — снять паузу\n"
    "/closeall — закрыть все позиции на демо\n"
    "/uptest — тест связи: открыть и сразу закрыть BTC на демо\n"
    "\n<b>📒 ЖУРНАЛЫ</b>\n"
    "/log — прислать журнал авто-слоя прямо сейчас\n"
    "\nСтратегия: <b>donchian_regime_v42</b> — полностью автоматическая, без "
    "ручных команд входа/выхода. Сканирует раз в сутки после закрытия дневной "
    f"свечи (~{DR42_CHECK_HOUR_UTC:02d}:{DR42_CHECK_MIN_UTC:02d} UTC).")

def handle_command(text: str) -> str:
    parts = text.replace(",", ".").split()
    if not parts or not parts[0].startswith("/"):
        return ""
    cmd = parts[0].lower().split("@")[0]
    arg = parts[1].upper() if len(parts) > 1 else ""

    if cmd == "/up":
        return EXECUTOR.status() + "\n" + DR42.status_report()
    if cmd == "/btc":
        b = btc_bias()
        return btc_line(b) + (f"\nVWAP {b['vwap']:.2f}, цена {b['price']:.2f}" if b.get("vwap") else "")
    if cmd in ("/hist", "/history", "/trades"):
        days = int(arg) if arg.isdigit() else 7      # /hist 30 — за 30 дней
        return EXECUTOR.history(max(1, min(60, days)))
    if cmd == "/risk":
        return EXECUTOR.risk_summary()
    if cmd == "/riskraw":
        return EXECUTOR.risk_dump()
    if cmd == "/uptest":
        return EXECUTOR.selftest()
    if cmd == "/closeall":
        return EXECUTOR.closeall()
    if cmd == "/halt":
        return EXECUTOR.halt()
    if cmd == "/resume":
        return EXECUTOR.resume()
    if cmd in ("/log", "/files", "/journal"):
        today = datetime.now(MSK).strftime("%Y-%m-%d")
        sent = 0
        if os.path.exists(EXEC_CSV) and os.path.getsize(EXEC_CSV) > 0:
            send_document(EXEC_CSV, f"{today} — авто-слой")
            sent += 1
        return f"📒 Отправлено файлов: {sent}" if sent else "📒 Журналы пока пустые."
    if cmd == "/dr42":
        return DR42.status_report()
    if cmd in ("/help", "/start"):
        return HELP_TEXT
    return ""

def poll_commands():
    for text in tg_updates():
        try:
            answer = handle_command(text)
        except Exception as e:
            traceback.print_exc()
            answer = f"Ошибка команды: {esc(str(e))}"
        if answer:
            print(f"[CMD] {text}")
            send_telegram(answer)

# ─── ЖУРНАЛЫ ПЕРЕД ПЕРЕЗАПУСКОМ ───────────────────────────────────────────────

def send_logs(caption_prefix: str):
    """Отправляет журнал авто-слоя в Telegram. Вызывается ПЕРЕД перезапуском —
    на Render файлы стираются при каждом деплое, иначе статистика теряется."""
    today = datetime.now(MSK).strftime("%Y-%m-%d %H:%M")
    send_document(EXEC_CSV, f"{caption_prefix} {today} — авто-слой")

def _on_shutdown(signum, frame):
    """Render присылает SIGTERM перед перезапуском — успеваем сохранить статистику."""
    print(f"[SHUTDOWN] сигнал {signum}: отправляю журналы перед остановкой")
    try:
        send_telegram("♻️ <b>Бот перезапускается</b> — отправляю журналы, "
                      "чтобы статистика не потерялась при деплое.")
        send_logs("перед перезапуском")
    except Exception as e:
        print(f"[SHUTDOWN ERROR] {e}")
    raise SystemExit(0)

# ─── ГЛАВНЫЙ ЦИКЛ ─────────────────────────────────────────────────────────────

def main():
    tg_drop_webhook()
    _signal.signal(_signal.SIGTERM, _on_shutdown)
    _signal.signal(_signal.SIGINT, _on_shutdown)
    # Разовый прогон бэктеста: в Render добавить переменную окружения RUN_BACKTEST=<mode>,
    # дождаться результатов в Telegram, затем убрать переменную (иначе он будет
    # запускаться при каждом перезапуске). После бэктеста бот продолжает работать как обычно.
    bt_mode = (os.environ.get("RUN_BACKTEST") or "").strip().lower()
    print(f"[BACKTEST] RUN_BACKTEST={bt_mode!r} → " +
          ("DONCHIAN REGIME v4.5 (bt_donchian_regime_v45.py)" if bt_mode in ("donchian_regime_v45", "28")
           else "DONCHIAN REGIME v4.4, 60д (bt_donchian_regime_v44_60d.py)" if bt_mode in ("donchian_regime_v44_60d", "27")
           else "DONCHIAN REGIME v4.4 (bt_donchian_regime_v44.py)" if bt_mode in ("donchian_regime_v44", "26")
           else "DONCHIAN REGIME v4.2 (bt_donchian_regime_v42.py)" if bt_mode in ("donchian_regime_v42", "25")
           else "DONCHIAN REGIME v4.1 (bt_donchian_regime_v41.py)" if bt_mode in ("donchian_regime_v41", "24")
           else "DONCHIAN REGIME v4 (bt_donchian_regime.py)" if bt_mode in ("donchian_regime", "23")
           else "DAILY TREND-FOLLOWING ATR (bt_daily_trend.py)" if bt_mode in ("daily_trend", "22")
           else "сравнение стратегий (bt_compare.py)" if bt_mode in ("compare", "2", "cmp")
           else "MULTI-REGIME ALL пул (bt_multiregime_all.py)" if bt_mode in ("multiregime_all", "21")
           else "DRAFT+FLOW финальный тест (bt_draft3.py)" if bt_mode in ("draft3", "20")
           else "DRAFT Day Range Fade (bt_draft.py)" if bt_mode in ("draft", "19")
           else "ETH VOLATILITY BREAKOUT (bt_breakout.py)" if bt_mode in ("breakout", "18")
           else "РАЗБОР ПО ПАРАМ (bt_pairs.py)" if bt_mode in ("pairs", "17")
           else "ФИЛЬТР ПО BTC (bt_btc.py)" if bt_mode in ("btc", "16")
           else "ПРОБОЙ VWAP (bt_vbreak.py)" if bt_mode in ("vbreak", "15")
           else "VWAP КАК МАГНИТ (bt_vwap.py)" if bt_mode in ("vwap", "14")
           else "СТОП И ПОДТЯЖКА (bt_stop.py)" if bt_mode in ("stop", "13")
           else "ТАЙМФРЕЙМЫ ЗАРЯДА (bt_tf.py)" if bt_mode in ("tf", "12")
           else "АУДИТ ДОПУЩЕНИЙ (bt_audit.py)" if bt_mode in ("audit", "11")
           else "СХЕМА ВХОДА (bt_entry.py)" if bt_mode in ("entry", "10")
           else "ОСЛАБЛЕНИЯ (bt_loose.py)" if bt_mode in ("loose", "9")
           else "ДИАГНОСТИКА зарядов (bt_why.py)" if bt_mode in ("why", "8")
           else "ДЛИННЫЙ бэктест (bt_long.py)" if bt_mode in ("long", "7")
           else "ПЕРЕБОР параметров (bt_sweep.py)" if bt_mode in ("sweep2", "6", "params")
           else "СДЕЛКИ по мелким свечам (bt_trades2.py)" if bt_mode in ("trades2", "5")
           else "СДЕЛКИ (bt_trades.py)" if bt_mode in ("trades", "4", "trade")
           else "потолок диапазона (bt_range.py)" if bt_mode in ("range", "3", "rng")
           else "перебор настроек (backtest.py)" if bt_mode in ("1", "true", "yes", "on", "sweep")
           else "не запускаю"))
    if bt_mode in ("1", "true", "yes", "on", "sweep", "compare", "2", "cmp", "range", "3", "rng", "trades", "4", "trade", "trades2", "5", "sweep2", "6", "params", "long", "7", "why", "8", "loose", "9", "entry", "10", "audit", "11", "tf", "12", "stop", "13", "vwap", "14", "vbreak", "15", "btc", "16", "pairs", "17", "breakout", "18", "draft", "19", "draft3", "20", "multiregime_all", "21", "daily_trend", "22", "donchian_regime", "23", "donchian_regime_v41", "24", "donchian_regime_v42", "25", "donchian_regime_v44", "26", "donchian_regime_v44_60d", "27", "donchian_regime_v45", "28"):
        # Защита от повторов: если контейнер перезапустится (нехватка памяти, сбой,
        # деплой), бэктест не начнётся заново — метка о запуске лежит рядом с логами.
        mark = os.path.join(LOG_DIR, "backtest_done.txt")
        today = datetime.now(MSK).strftime("%Y-%m-%d")
        done = ""
        try:
            with open(mark, encoding="utf-8") as f:
                done = f.read().strip()
        except Exception:
            pass
        if done == today:
            print(f"[BACKTEST] сегодня уже запускался ({done}) — пропуск")
            send_telegram("ℹ️ Бэктест сегодня уже отрабатывал — повторно не запускаю.\n"
                          "Если нужен ещё один прогон, убери и снова добавь RUN_BACKTEST.")
        else:
            try:
                with open(mark, "w", encoding="utf-8") as f:
                    f.write(today)
            except Exception:
                pass
            try:
                if bt_mode in ("donchian_regime_v45", "28"):
                    import bt_donchian_regime_v45
                    bt_donchian_regime_v45.main()  # v4.4 без PerSide cap (MAX_PER_SIDE_CAP=6, PER_SIDE_BUDGET=999999)
                elif bt_mode in ("donchian_regime_v44_60d", "27"):
                    import bt_donchian_regime_v44_60d
                    bt_donchian_regime_v44_60d.main()  # то же v4.4, но окно всего 60 дней (без BT_START)
                elif bt_mode in ("donchian_regime_v44", "26"):
                    import bt_donchian_regime_v44
                    bt_donchian_regime_v44.main()  # Donchian(20) + BTC + DMI + compound + per-side cap + daily stop -$400
                elif bt_mode in ("donchian_regime_v42", "25"):
                    import bt_donchian_regime_v42
                    bt_donchian_regime_v42.main()  # Donchian(20) + BTC SMA(50) + DMI + DD brake (no ADX)
                elif bt_mode in ("donchian_regime_v41", "24"):
                    import bt_donchian_regime_v41
                    bt_donchian_regime_v41.main()  # Donchian(20) + BTC SMA(50) + DMI + ADX>20 + DD brake
                elif bt_mode in ("donchian_regime", "23"):
                    import bt_donchian_regime
                    bt_donchian_regime.main()  # Donchian(20) + BTC SMA(50) + DMI + Trailing Stop
                elif bt_mode in ("daily_trend", "22"):
                    import bt_daily_trend
                    bt_daily_trend.main()      # Daily Trend-Following с ATR-выходами
                elif bt_mode in ("multiregime_all", "21"):
                    import bt_multiregime_all
                    bt_multiregime_all.main()  # MULTI-REGIME на всём пуле 103 пары
                elif bt_mode in ("draft3", "20"):
                    import bt_draft3
                    bt_draft3.main()       # DRAFT+FLOW финальный тест серии (F1+F2 по потоку)
                elif bt_mode in ("draft", "19"):
                    import bt_draft
                    bt_draft.main()        # DRAFT Day Range Fade (лимитки от рамки дня)
                elif bt_mode in ("breakout", "18"):
                    import bt_breakout
                    bt_breakout.main()     # ETHVolatilityBreakoutPro на всём пуле пар
                elif bt_mode in ("pairs", "17"):
                    import bt_pairs
                    bt_pairs.main()        # разбор по парам и группам
                elif bt_mode in ("btc", "16"):
                    import bt_btc
                    bt_btc.main()          # фильтр по уклону BTC
                elif bt_mode in ("vbreak", "15"):
                    import bt_vbreak
                    bt_vbreak.main()       # сигнал по пробою VWAP
                elif bt_mode in ("vwap", "14"):
                    import bt_vwap
                    bt_vwap.main()         # VWAP как магнит
                elif bt_mode in ("stop", "13"):
                    import bt_stop
                    bt_stop.main()         # шаг 1: стоп и подтяжка
                elif bt_mode in ("tf", "12"):
                    import bt_tf
                    bt_tf.main()           # на каком ТФ искать заряд
                elif bt_mode in ("audit", "11"):
                    import bt_audit
                    bt_audit.main()        # аудит допущений бэктеста
                elif bt_mode in ("entry", "10"):
                    import bt_entry
                    bt_entry.main()        # новая схема входа с подтверждением
                elif bt_mode in ("loose", "9"):
                    import bt_loose
                    bt_loose.main()        # что даст ослабление условий заряда
                elif bt_mode in ("why", "8"):
                    import bt_why
                    bt_why.main()          # почему монета не стала зарядом
                elif bt_mode in ("long", "7"):
                    import bt_long
                    bt_long.main()         # длинная история, только 1h
                elif bt_mode in ("sweep2", "6", "params"):
                    import bt_sweep
                    bt_sweep.main()        # перебор параметров (пункты 4-7)
                elif bt_mode in ("trades2", "5"):
                    import bt_trades2
                    bt_trades2.main()      # v3: сделка по мелким свечам
                elif bt_mode in ("trades", "4", "trade"):
                    import bt_trades
                    bt_trades.main()       # v8.8: полноценный бэктест сделок
                elif bt_mode in ("range", "3", "rng"):
                    import bt_range
                    bt_range.main()        # v8.8: проверка адаптивного потолка диапазона
                elif bt_mode in ("compare", "2", "cmp"):
                    import bt_compare
                    bt_compare.main()      # сравнение стратегий
                else:
                    import backtest
                    backtest.main()        # перебор настроек нашей стратегии
            except MemoryError:
                traceback.print_exc()
                send_telegram("⚠️ Бэктесту не хватило памяти. Уменьши BT_PAIRS (например 30) "
                              "или BT_DAYS.\nБот работает в обычном режиме.")
            except Exception as e:
                traceback.print_exc()
                send_telegram(f"⚠️ Бэктест не отработал: {esc(str(e))}\nБот продолжает работу в обычном режиме.")

    start_lines = [
        f"🚀 <b>Upscale Bot {BOT_VERSION}</b> | режим: <b>{EXECUTOR.mode}</b>"
        + {"dry": " (только сообщения)", "demo": " (ордера на ДЕМО-счёт)"}.get(EXECUTOR.mode, ""),
        "",
        "📐 <b>Стратегия: donchian_regime_v42 (логика v4.4)</b> — полностью автоматическая",
        "   Donchian(20) пробой + BTC SMA(50) режим + DMI + ATR-фильтр",
        "   Compound sizing: риск 0.8% equity (старт $80, макс $200), DD-brake ×0.5 при DD > $1200",
        "   Вход с плечом (EXEC_LEVERAGE, по умолчанию 5×)",
        f"   Выход: trailing-стоп 2×ATR / разворот Donchian / {DR42.MAX_HOLD_DAYS}д (без TP) | "
        f"Лимит в сторону: ${DR42.SAME_SIDE_RISK_ANCHOR_USD:.0f}/риск (до {DR42.MAX_SAME_SIDE_CEILING}, "
        f"итого до {DR42.MAX_CONCURRENT_CEILING}) | новых в день: {DR42.MAX_NEW_PER_DAY}",
        f"   Дневной стоп: убыток дня ≥ ${abs(DR42.DAILY_STOP_LOSS):.0f} → новых входов нет до завтра (UTC)",
        f"   Cooldown: {DR42.CONSEC_LOSS_LIMIT} убытка подряд → блок {DR42.COOLDOWN_DAYS} дней",
        f"   Exclude: {sorted(DR42.EXCLUDE_PAIRS)}",
        f"   Скан раз в сутки, ~{DR42_CHECK_HOUR_UTC:02d}:{DR42_CHECK_MIN_UTC:02d} UTC",
        "   Аварийный стоп (счёт целиком): дневной убыток ≥90% дневного лимита → закрыть всё, пауза до след. дня UTC",
        "",
    ]
    if EXCLUDE_SYMBOLS:
        start_lines.append(f"🚫 Не торгуем (глобально): {', '.join(sorted(EXCLUDE_SYMBOLS))}")
    start_lines += [
        f"📊 Пар: {len(UPSCALE_PAIRS)}",
        "💬 /help — все команды | /up статус | /halt пауза",
    ]
    send_telegram("\n".join(start_lines))

    # Баланс Upscale нужен donchian для compound sizing — обновляется в фоне
    # каждые 5 минут и принудительно перед каждым сканом сигналов.
    DR42.start_balance_updater()

    # Команды в чате — в отдельном потоке, не дожидаясь скана. Скан ~100 пар
    # с лимитом 12 запросов/сек может идти ощутимо долго; раньше poll_commands()
    # вызывался в том же цикле ПЕРЕД сканом, и /halt, отправленный во время
    # скана, читался только после его завершения.
    def _command_loop():
        while True:
            try:
                poll_commands()
            except Exception as e:
                print(f"[CMD LOOP ERROR] {e}")
                traceback.print_exc()
            time.sleep(2)
    threading.Thread(target=_command_loop, daemon=True, name="tg-commands").start()

    # Дата последнего скана DR42 — ПЕРСИСТЕНТНО на диске (тот же паттерн, что
    # и у backtest_done.txt выше). Без этого: Render перезапустит контейнер
    # (деплой/OOM/сбой) после времени скана в тот же день → last_dr42_date
    # в памяти сотрётся → check_signals() выполнится повторно за те же сутки,
    # и может открыть больше входов, чем задумано MAX_NEW_PER_DAY (если часть
    # утренних позиций уже закрылась стопом к моменту рестарта).
    dr42_mark = os.path.join(LOG_DIR, "dr42_scan_date.txt")
    try:
        with open(dr42_mark, encoding="utf-8") as f:
            last_dr42_date = f.read().strip()
    except Exception:
        last_dr42_date = None

    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            today_utc = now_utc.strftime("%Y-%m-%d")
            if ((now_utc.hour, now_utc.minute) >= (DR42_CHECK_HOUR_UTC, DR42_CHECK_MIN_UTC)
                    and last_dr42_date != today_utc):
                last_dr42_date = today_utc
                try:
                    with open(dr42_mark, "w", encoding="utf-8") as f:
                        f.write(today_utc)
                except Exception as e:
                    print(f"[DR42] не удалось сохранить метку скана: {e}")
                try:
                    DR42.check_signals()
                except Exception as e:
                    print(f"[DR42 ERROR] {e}")
                    traceback.print_exc()
                    send_telegram(f"⚠️ donchian_regime_v42: ошибка скана — {esc(str(e))}")

        except Exception as e:
            print(f"[LOOP ERROR] {e}")
            traceback.print_exc()
        time.sleep(2)

if __name__ == "__main__":
    main()
