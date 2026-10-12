# -*- coding: utf-8 -*-
"""
exec_orderflow.py — Скальперский бот OrderFlow v2 (Iceberg Detector + OI Filter)
================================================================================
v2: Добавлен фильтр по Open Interest (OI) и time-stop 5 минут.

Логика v2 + CVD-aware exit:
  1. Ловит "айсберги" через WebSocket (4+ одинаковых сделки за 15с, $100K+)
  2. Проверяет OI (Open Interest) через REST API каждые 10с
     - OI растёт → реальные новые long-позиции → ВХОДИМ ✅
     - OI падает → закрытие шортов → ПРОПУСКАЕМ ❌
  3. Time-stop CVD-aware (v2.2 — без жёсткого FORCE CLOSE):
     - T+60с: подтянуть стоп в безубыток
     - T+240с: CVD-чек (повторяется каждые 90с, пока позиция жива):
       RISING  → продлить таймер +90с И поднять TP вверх (трейлинг)
       NEUTRAL → продлить таймер +90с (без trail TP)
       FALLING → выходим (продавцы давят)
       STALE   → выходим (мало данных, безопасный вариант)
     - T+3600с: safety net (60 мин) — закрываем принудительно
       (только если CVD слишком долго не может дать сигнал)
  4. Cooldown 10 мин по символу, 3 мин глобальный, 3 сигнала/час
  5. Динамический стоп = max(0.3%, 1.5×ATR_1min), тейк = 2×стоп

  CVD (Cumulative Volume Delta) = sum(buy_vol) - sum(sell_vol) за окно.
  Если CVD падает → продавцы давят → выходим.
  Если CVD растёт/нейтрально → даём позиции ещё шанс.

Управление через Telegram:
  /of         — статус
  /of_on      — включить исполнитель
  /of_off     — выключить исполнитель
  /of_alerts  — вкл/выкл алерты
  /of_pairs   — список пар и OI
  /of_log     — прислать CSV журнал
  /of_stats   — winrate, avg R, consec losses
================================================================================
"""

import os
import sys
import json
import time
import math
import threading
import datetime as dt
from collections import deque, defaultdict
from typing import Optional

try:
    import websocket
except ImportError:
    websocket = None
    print("[exec_of] websocket-client не установлен. pip install websocket-client")

try:
    import bot as B
except Exception as e:
    B = None
    _BOT_IMPORT_ERR = e
else:
    _BOT_IMPORT_ERR = None

try:
    import upscale_exec as UE
except Exception as e:
    UE = None
    _UE_IMPORT_ERR = e
else:
    _UE_IMPORT_ERR = None


# =====================================================================
#  КОНСТАНТЫ
# =====================================================================

STRATEGY_NAME = "OrderFlow v2"
STRATEGY_TAG  = "orderflow_v2"

# --- Список пар ─────────────────────────────────────────────────────────────
OF_PAIRS_DEFAULT = [
    "BTC", "ETH", "SOL", "XRP", "LINK", "TRX", "DOT", "LTC", "BCH",
    "AVAX", "NEAR", "UNI", "APT", "FIL", "ATOM", "ARB", "OP", "INJ",
    "AAVE", "ALGO", "ETC", "XLM", "HBAR", "ICP", "STX", "SUI", "SEI",
    "TIA", "RUNE", "ENA"
]
OF_PAIRS = [p.strip() for p in os.environ.get("OF_PAIRS", ",".join(OF_PAIRS_DEFAULT)).split(",") if p.strip()]

# --- Параметры сделок ────────────────────────────────────────────────────────
OF_MARGIN_USD    = float(os.environ.get("OF_MARGIN_USD", "50"))
OF_LEVERAGE      = int(os.environ.get("OF_LEVERAGE", "5"))
OF_STOP_PCT_MIN  = float(os.environ.get("OF_STOP_PCT_MIN", "0.3"))   # мин стоп 0.3%
OF_TP_MULT       = float(os.environ.get("OF_TP_MULT", "2.0"))        # тейк = 2× стоп
OF_MAX_OPEN      = int(os.environ.get("OF_MAX_OPEN", "5"))
OF_MAX_SAME_SIDE = int(os.environ.get("OF_MAX_SAME_SIDE", "3"))
OF_MAX_NEW_PER_DAY = int(os.environ.get("OF_MAX_NEW_PER_DAY", "20"))
# (v2.2: жёсткий FORCE_CLOSE убран — теперь CVD сам управляет выходом;
# safety net = OF_CVD_HARD_LIMIT_SEC, см. ниже в секции CVD)
OF_BREAKEVEN_SEC = int(os.environ.get("OF_BREAKEVEN_SEC", "60"))       # 60с → безубыток
OF_EXIT_SEC      = int(os.environ.get("OF_EXIT_SEC", "240"))           # 240с → первый CVD-чек (4 мин)
OF_COOLDOWN_LOSSES = int(os.environ.get("OF_COOLDOWN_LOSSES", "4"))
OF_COOLDOWN_MIN    = int(os.environ.get("OF_COOLDOWN_MIN", "20"))     # 20 мин после убытков

# --- Параметры детектора айсбергов ──────────────────────────────────────────
DETECTOR_WINDOW_SEC          = 15
DETECTOR_MIN_TRADES          = 4
DETECTOR_SIZE_TOLERANCE_PCT  = 15.0
DETECTOR_MIN_TOTAL_NOTIONAL = 100000.0   # $100K мин
DETECTOR_MIN_IMBALANCE_RATIO = 3.0
DETECTOR_COOLDOWN_PER_SYMBOL = 600         # 10 минут (было 120)
DETECTOR_GLOBAL_COOLDOWN     = 180         # 3 минуты между любыми сигналами
DETECTOR_MAX_PER_HOUR        = 3           # максимум 3 сигнала в час
DETECTOR_MAX_NOTIONAL_ABS    = 500.0

# --- OI-фильтр ──────────────────────────────────────────────────────────────
OI_POLL_SEC         = 10     # опрос каждые 10 секунд
OI_WINDOW_SEC       = 30     # окно сравнения 30 секунд
OI_MIN_DELTA_PCT    = 0.20   # OI должен вырасти минимум на 0.2%
OI_MIN_DELTA_USD    = 25000  # OI должен вырасти минимум на $25K
OI_STALE_SEC        = 40     # если OI не обновлялся >40с → skip

# --- CVD (Cumulative Volume Delta) для умного выхода по времени ──────────────
# CVD = sum(buy_vol) - sum(sell_vol) за окно OF_CVD_WINDOW_SEC.
# При срабатывании T+OF_EXIT_SEC проверяем CVD каждые OF_CVD_RESET_SEC:
#   RISING  → покупатели давят, продлеваем таймер +90с И поднимаем TP (trail)
#   NEUTRAL → дисбаланс слабый, продлеваем (без trail TP)
#   FALLING → продавцы давят, выходим
#   STALE   → нет данных, выходим (безопасный вариант)
# Safety net OF_CVD_HARD_LIMIT_SEC — если позиция прожила дольше этого
# времени, закрываем принудительно (safety net, не «жёсткий» таймер).
OF_CVD_WINDOW_SEC       = int(os.environ.get("OF_CVD_WINDOW_SEC", "90"))      # окно CVD (для выхода по time-stop)
OF_CVD_FALL_THRESHOLD   = float(os.environ.get("OF_CVD_FALL_THRESHOLD", "0.35"))  # 35% дисбаланс = FALLING (было 20% — слишком чувствительно)
OF_CVD_MIN_VOL_USD      = float(os.environ.get("OF_CVD_MIN_VOL_USD", "50000"))   # мин объём за окно (для time-stop CVD)
OF_CVD_RESET_SEC        = int(os.environ.get("OF_CVD_RESET_SEC", "90"))         # +90с за продление (было 60)
OF_CVD_HARD_LIMIT_SEC   = int(os.environ.get("OF_CVD_HARD_LIMIT_SEC", "3600"))  # safety net 60 мин (было 30)
OF_CVD_TRAIL_TP_PCT     = float(os.environ.get("OF_CVD_TRAIL_TP_PCT", "0.30"))  # +0.3% над ценой при трейлинге

# --- 15m CVD momentum фильтр (для ВХОДА в сделку) ─────────────────────────────
# Идея: если CVD за 15 минут растёт (был -100 → стал 0, или был 0 → стал +50)
# значит моментум в нашу сторону → можно входить.
# Если CVD падает (был +50 → стал -20) → продавцы перехватили → пропускаем.
# Это НЕ заменяет 90s CVD для time-stop, это отдельный фильтр на вход.
OF_CVD_15M_FILTER_ENABLED = os.environ.get("OF_CVD_15M_FILTER_ENABLED", "1") == "1"
OF_CVD_15M_WINDOW_SEC      = int(os.environ.get("OF_CVD_15M_WINDOW_SEC", "900"))   # 15 минут
OF_CVD_15M_MIN_MOMENTUM    = float(os.environ.get("OF_CVD_15M_MIN_MOMENTUM", "0"))  # 0 = любой рост CVD (даже +$1)
OF_CVD_15M_MIN_VOL_USD     = float(os.environ.get("OF_CVD_15M_MIN_VOL_USD", "100000"))  # мин объём за 15 минут
OF_CVD_15M_MIN_DATA_SEC    = int(os.environ.get("OF_CVD_15M_MIN_DATA_SEC", "300"))  # мин 5 минут данных, иначе STALE

# --- WebSocket Gate.io ───────────────────────────────────────────────────────
GATE_WS_URL = "wss://fx-ws.gateio.ws/v4/ws/usdt"
GATE_REST   = "https://api.gateio.ws/api/v4/futures/usdt"

# --- Файлы состояния ────────────────────────────────────────────────────────
# На Render — монтируем Persistent Disk и через env-var BOT_DATA_DIR=/var/data
# Локально — дефолт /tmp (старое поведение, ничего не ломаем)
_DATA_DIR = os.environ.get("BOT_DATA_DIR", "/tmp")
# Создаём папку если её нет (на Render диск пустой при первом запуске)
try:
    os.makedirs(_DATA_DIR, exist_ok=True)
except Exception:
    pass

OF_STATE_FILE   = f"{_DATA_DIR}/orderflow_state.json"
OF_TRACKED_FILE = f"{_DATA_DIR}/orderflow_tracked.json"
OF_SIGNALS_CSV  = f"{_DATA_DIR}/orderflow_signals.csv"
OF_TRADES_CSV   = f"{_DATA_DIR}/orderflow_trades.csv"

# --- Блокировки ─────────────────────────────────────────────────────────────
_STATE_LOCK   = threading.Lock()
_TRACKED_LOCK = threading.Lock()
_OI_LOCK      = threading.Lock()

# --- Глобальное состояние ────────────────────────────────────────────────────
_ws_thread = None
_ws_client = None
_oi_thread = None
_state = {
    "enabled": False,
    "alerts_enabled": True,
    "ws_connected": False,
    "last_signal_ts": {},
    "last_global_signal_ts": 0,
    "signals_this_hour": 0,
    "signals_hour_str": "",
    "signals_today": 0,
    "signals_today_date": "",
    "new_today": 0,
    "new_today_date": "",
    "cooldown_until": 0,
    "cooldown_symbol_until": {},  # {sym: ts} — кулдаун по символу после убытка
}

# --- Буферы ─────────────────────────────────────────────────────────────────
_trade_buffer = defaultdict(lambda: deque(maxlen=200))
_used_trade_ids = set()
_quanto_cache = {}
_oi_cache = defaultdict(lambda: deque(maxlen=6))   # {sym: [(ts, oi_usd), ...]}
_cvd_buffer  = defaultdict(lambda: deque(maxlen=3000))  # {sym: [{ts, side, size}, ...]} — CVD (15m окно при ~3 сделки/сек)
_cvd_lock    = threading.Lock()

# --- Статистика ─────────────────────────────────────────────────────────────
_stats = {
    "signals_total": 0,
    "signals_suppressed": 0,
    "signals_oi_rejected": 0,
    "trades_opened": 0,
    "trades_closed": 0,
    "wins": 0,
    "losses": 0,
    "total_pnl": 0.0,
    "consec_losses": 0,
}


# =====================================================================
#  УТИЛИТЫ
# =====================================================================

def _today_utc_str():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

def _hour_str():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d-%H")

def _now_ts():
    return time.time()

def _atomic_write_json(path, data):
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)

def _load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return default

def _median(values):
    if not values: return 0.0
    s = sorted(values)
    n = len(s)
    if n % 2 == 0:
        return (s[n//2 - 1] + s[n//2]) / 2
    return s[n//2]


# =====================================================================
#  STATE MANAGEMENT
# =====================================================================

def _save_state():
    with _STATE_LOCK:
        data = {
            "enabled": _state["enabled"],
            "alerts_enabled": _state["alerts_enabled"],
            "cooldown_until": _state["cooldown_until"],
            "consec_losses": _stats["consec_losses"],
            "signals_today": _state.get("signals_today", 0),
            "new_today": _state.get("new_today", 0),
            "signals_today_date": _state.get("signals_today_date", ""),
            "new_today_date": _state.get("new_today_date", ""),
            "cooldown_symbol_until": _state.get("cooldown_symbol_until", {}),
        }
        _atomic_write_json(OF_STATE_FILE, data)

def _load_state():
    global _state
    with _STATE_LOCK:
        saved = _load_json(OF_STATE_FILE, {})
        if saved:
            _state["enabled"] = saved.get("enabled", False)
            _state["alerts_enabled"] = saved.get("alerts_enabled", True)
            _state["cooldown_until"] = saved.get("cooldown_until", 0)
            _stats["consec_losses"] = saved.get("consec_losses", 0)
            _state["signals_today"] = saved.get("signals_today", 0)
            _state["new_today"] = saved.get("new_today", 0)
            _state["signals_today_date"] = saved.get("signals_today_date", _today_utc_str())
            _state["new_today_date"] = saved.get("new_today_date", _today_utc_str())
            _state["cooldown_symbol_until"] = saved.get("cooldown_symbol_until", {})
        else:
            _state["signals_today_date"] = _today_utc_str()
            _state["new_today_date"] = _today_utc_str()
            _state["signals_hour_str"] = _hour_str()

def _reset_counters():
    today = _today_utc_str()
    if _state.get("signals_today_date") != today:
        _state["signals_today"] = 0
        _state["signals_today_date"] = today
    if _state.get("new_today_date") != today:
        _state["new_today"] = 0
        _state["new_today_date"] = today
    hour = _hour_str()
    if _state.get("signals_hour_str") != hour:
        _state["signals_this_hour"] = 0
        _state["signals_hour_str"] = hour


# =====================================================================
#  TRACKED POSITIONS
# =====================================================================

def _load_tracked():
    return _load_json(OF_TRACKED_FILE, {})

def _save_tracked(d):
    _atomic_write_json(OF_TRACKED_FILE, d)

def _add_tracked(pos_id, sym, side, entry, stop, tp, opened_ts):
    with _TRACKED_LOCK:
        tracked = _load_tracked()
        tracked[pos_id] = {
            "sym": sym, "side": side, "entry": entry,
            "stop": stop, "tp": tp, "opened_ts": opened_ts,
            "initial_stop": stop,  # для проверки breakeven
            "be_done": False,       # флаг: подтянут ли в безубыток
            "strategy": STRATEGY_TAG, "status": "open",
        }
        _save_tracked(tracked)

def _get_open_count():
    tracked = _load_tracked()
    return sum(1 for v in tracked.values() if v.get("status") == "open")

def _get_same_side_count(side):
    tracked = _load_tracked()
    return sum(1 for v in tracked.values()
               if v.get("status") == "open" and v.get("side") == side)

def _has_position_for_sym(sym):
    tracked = _load_tracked()
    return any(v.get("sym") == sym and v.get("status") == "open" for v in tracked.values())


# =====================================================================
#  QUANTO MULTIPLIER
# =====================================================================

def _fetch_quanto_multipliers():
    global _quanto_cache
    if not B:
        return
    try:
        contracts = B.api_get("contracts", {})
        if not isinstance(contracts, list):
            return
        for c in contracts:
            name = c.get("name", "")
            if name.endswith("_USDT"):
                sym = name[:-5]
                qm = float(c.get("quanto_multiplier", "1"))
                _quanto_cache[sym] = qm
        print(f"[exec_of] Загружено quanto_multiplier для {len(_quanto_cache)} пар")
    except Exception as e:
        print(f"[exec_of] Ошибка загрузки quanto: {e}")

def _size_to_usd(sym, size_contracts, price):
    qm = _quanto_cache.get(sym, 1.0)
    return abs(float(size_contracts)) * qm * float(price)

def _get_side(size):
    return "buy" if float(size) > 0 else "sell"


# =====================================================================
#  OI POLLER (Open Interest — REST каждые 10 сек)
# =====================================================================

def _oi_loop():
    """Фоновый поток: опрашивает OI по всем парам каждые 10 секунд."""
    print("[exec_of] OI poller запущен (каждые 10с)")
    while True:
        try:
            if not B:
                time.sleep(OI_POLL_SEC)
                continue
            # Если quanto_cache пустой — переподнимаем
            if not _quanto_cache:
                _fetch_quanto_multipliers()
            # Запрашиваем тикеры (включает total_size = OI в контрактах)
            tickers = B.api_get("tickers", {})
            if not isinstance(tickers, list):
                time.sleep(OI_POLL_SEC)
                continue
            now = _now_ts()
            for t in tickers:
                contract = t.get("contract", "")
                if not contract.endswith("_USDT"):
                    continue
                sym = contract[:-5]
                if sym not in OF_PAIRS:
                    continue
                total_size = float(t.get("total_size", 0))
                price = float(t.get("last", 0))
                qm = _quanto_cache.get(sym, 1.0)
                oi_usd = total_size * qm * price
                with _OI_LOCK:
                    _oi_cache[sym].append((now, oi_usd))
        except Exception as e:
            print(f"[exec_of] OI poll error: {e}")
        time.sleep(OI_POLL_SEC)

def _check_oi_filter(sym, iceberg_notional):
    """Проверяет: растёт ли OI по паре за последние 30 секунд?
    Возвращает: 'GREEN' (расти), 'RED' (падает), 'YELLOW' (нейтрально), 'STALE' (нет данных)."""
    with _OI_LOCK:
        buf = list(_oi_cache.get(sym, deque()))

    if len(buf) < 2:
        return "STALE"

    now = _now_ts()
    latest_ts, latest_oi = buf[-1]

    # Проверка на устаревшие данные
    if now - latest_ts > OI_STALE_SEC:
        return "STALE"

    # Ищем значение ~30 секунд назад
    old_oi = None
    for ts, oi in reversed(buf):
        if now - ts >= OI_WINDOW_SEC:
            old_oi = oi
            break
    if old_oi is None:
        # Нет записи старше 30 сек — данных недостаточно для сравнения
        return "STALE"

    if old_oi <= 0:
        return "STALE"

    delta_usd = latest_oi - old_oi
    delta_pct = (delta_usd / old_oi) * 100

    if delta_usd < 0:
        return "RED"
    if delta_pct >= OI_MIN_DELTA_PCT and delta_usd >= OI_MIN_DELTA_USD:
        return "GREEN"
    if delta_pct >= OI_MIN_DELTA_PCT * 0.5:  # половина порога → YELLOW
        return "YELLOW"
    return "RED"


# =====================================================================
#  CVD (Cumulative Volume Delta) — для умного time-stop
# =====================================================================

def _update_cvd_buffer(sym, side, size_usd, ts):
    """Обновляет буфер CVD. Вызывается для КАЖДОЙ сделки (buy+sell), до фильтра по side."""
    try:
        with _cvd_lock:
            _cvd_buffer[sym].append({
                "ts": float(ts),
                "side": side,        # 'buy' или 'sell'
                "size": float(size_usd),
            })
    except Exception:
        pass

def _check_cvd_for_exit(sym):
    """Решает: продлевать таймер выхода или закрывать позицию.

    Считаем суммарный CVD за всё окно OF_CVD_WINDOW_SEC:
      total_cvd = sum(buy_vol) - sum(sell_vol)
      total_vol = sum(buy_vol) + sum(sell_vol)

    Логика:
      - total_cvd > 0 → покупатели доминировали за окно → RISING → ПРОДЛЕВАЕМ
      - total_cvd < 0, |total_cvd|/total_vol < OF_CVD_FALL_THRESHOLD → NEUTRAL → ПРОДЛЕВАЕМ
      - total_cvd < 0, |total_cvd|/total_vol >= OF_CVD_FALL_THRESHOLD → FALLING → ВЫХОДИМ
      - Недостаточно данных / мало объёма → STALE → ВЫХОДИМ (безопасный)
    """
    with _cvd_lock:
        buf = list(_cvd_buffer.get(sym, deque()))

    if len(buf) < 5:
        return "STALE"

    now = _now_ts()
    recent = [t for t in buf if now - t["ts"] <= OF_CVD_WINDOW_SEC]
    if len(recent) < 5:
        return "STALE"

    # Суммарный CVD за всё окно (направление, не acceleration)
    total_cvd = sum(t["size"] if t["side"] == "buy"  else -t["size"] for t in recent)
    total_vol = sum(t["size"] for t in recent)

    if total_vol < OF_CVD_MIN_VOL_USD:
        return "STALE"

    if total_cvd > 0:
        return "RISING"

    # total_cvd <= 0 → sellers dominate. Насколько сильно?
    ratio = abs(total_cvd) / total_vol if total_vol > 0 else 0
    if ratio < OF_CVD_FALL_THRESHOLD:
        return "NEUTRAL"  # слабый дисбаланс в сторону sells (даже немного просел → продлеваем)
    return "FALLING"


# =====================================================================
#  15m CVD MOMENTUM (для фильтра ВХОДА в сделку)
# =====================================================================

def _check_cvd_15m_momentum(sym):
    """Фильтр входа: растёт ли CVD за последние 15 минут?

    Логика:
      momentum_15m = CVD_сейчас - CVD_15m_назад

      Если momentum_15m > OF_CVD_15M_MIN_MOMENTUM → RISING (можно входить)
      Если momentum_15m ≤ 0 → FALLING (CVD падает, пропускаем)
      Если данных мало / мало объёма → STALE (пропускаем, чтобы не входить без подтверждения)

    Возвращает словарь:
      {
        "status": "RISING"|"FALLING"|"STALE",
        "momentum": float,   # CVD_сейчас - CVD_15m_назад (USD)
        "cvd_now": float,    # текущий CVD за 15m окно
        "cvd_15m_ago": float, # CVD 15 минут назад (точнее: CVD с начала окна)
        "total_vol": float,  # суммарный объём за 15 минут
      }
    """
    result = {"status": "STALE", "momentum": 0.0, "cvd_now": 0.0, "cvd_15m_ago": 0.0, "total_vol": 0.0}

    with _cvd_lock:
        buf = list(_cvd_buffer.get(sym, deque()))

    if len(buf) < 5:
        return result

    now = _now_ts()
    # Данные за последние 15 минут
    recent_15m = [t for t in buf if now - t["ts"] <= OF_CVD_15M_WINDOW_SEC]
    # Данные за последние 30 минут (чтобы взять "старую половину")
    recent_30m = [t for t in buf if now - t["ts"] <= OF_CVD_15M_WINDOW_SEC * 2]

    if len(recent_15m) < 5:
        return result

    # Проверяем что данных достаточно по времени
    oldest_ts = recent_15m[0]["ts"]
    if now - oldest_ts < OF_CVD_15M_MIN_DATA_SEC:
        return result  # меньше 5 минут данных → STALE

    # Делим окно 30 минут на две половины по 15 минут
    # "15m назад" = сделки в окне [30m назад, 15m назад]
    # "сейчас" = сделки в окне [15m назад, сейчас]
    cutoff_15m = now - OF_CVD_15M_WINDOW_SEC

    cvd_first_half  = sum(t["size"] if t["side"] == "buy"  else -t["size"] for t in recent_30m if t["ts"] <= cutoff_15m)
    cvd_second_half = sum(t["size"] if t["side"] == "buy"  else -t["size"] for t in recent_15m)

    total_vol_15m = sum(t["size"] for t in recent_15m)
    result["cvd_now"] = cvd_second_half
    result["cvd_15m_ago"] = cvd_first_half
    result["momentum"] = cvd_second_half - cvd_first_half
    result["total_vol"] = total_vol_15m

    # Проверка на минимальный объём
    if total_vol_15m < OF_CVD_15M_MIN_VOL_USD:
        return result  # STALE — мало объёма для значимой оценки

    # Проверка momentum
    if result["momentum"] > OF_CVD_15M_MIN_MOMENTUM:
        result["status"] = "RISING"
    else:
        result["status"] = "FALLING"

    return result


# =====================================================================
#  ICEBERG DETECTOR
# =====================================================================

def _detect_iceberg(sym, side, size_usd, ts):
    try:
        ts = float(ts)
        size_usd = float(size_usd)
    except (ValueError, TypeError):
        return None

    buf = _trade_buffer[sym]
    while buf and float(buf[0].get("ts", 0)) < ts - DETECTOR_WINDOW_SEC:
        buf.popleft()

    buys = [t for t in buf if t.get("side") == "buy" and t.get("id") not in _used_trade_ids]
    sells = [t for t in buf if t.get("side") == "sell" and t.get("id") not in _used_trade_ids]
    buy_vol = sum(float(t.get("size", 0)) for t in buys)
    sell_vol = sum(float(t.get("size", 0)) for t in sells)

    if buy_vol >= sell_vol * DETECTOR_MIN_IMBALANCE_RATIO:
        dom_side, dom_trades = "buy", buys
    elif sell_vol >= buy_vol * DETECTOR_MIN_IMBALANCE_RATIO:
        dom_side, dom_trades = "sell", sells
    else:
        return None

    if len(dom_trades) < DETECTOR_MIN_TRADES:
        return None

    trade_objs = sorted(dom_trades, key=lambda t: float(t.get("size", 0)))
    sizes = [float(t.get("size", 0)) for t in trade_objs]
    for i in range(len(sizes) - DETECTOR_MIN_TRADES + 1):
        window_sizes = sizes[i:i + DETECTOR_MIN_TRADES]
        window_objs = trade_objs[i:i + DETECTOR_MIN_TRADES]
        med = _median(window_sizes)
        if med <= 0:
            continue
        if all(abs(float(s) - med) / med <= DETECTOR_SIZE_TOLERANCE_PCT / 100 for s in window_sizes):
            cluster_total = sum(window_sizes)
            if cluster_total >= DETECTOR_MIN_TOTAL_NOTIONAL:
                for obj in window_objs:
                    trade_id = obj.get("id")
                    if trade_id:
                        _used_trade_ids.add(trade_id)
                if len(_used_trade_ids) > 1000:
                    _used_trade_ids.clear()
                return {
                    "sym": sym,
                    "side": dom_side,
                    "total_usd": cluster_total,
                    "avg_size": med,
                    "num_trades": len(window_sizes),
                    "ts": ts,
                }
    return None

def _should_emit_signal(sym):
    ts = _now_ts()
    # Cooldown по символу
    last = _state["last_signal_ts"].get(sym, 0)
    if ts - last < DETECTOR_COOLDOWN_PER_SYMBOL:
        return False
    # Глобальный cooldown
    if ts - _state.get("last_global_signal_ts", 0) < DETECTOR_GLOBAL_COOLDOWN:
        return False
    # Лимит сигналов в час
    _reset_counters()
    if _state.get("signals_this_hour", 0) >= DETECTOR_MAX_PER_HOUR:
        return False
    # Cooldown по символу после убытка
    cd_sym = _state.get("cooldown_symbol_until", {}).get(sym, 0)
    if ts < cd_sym:
        return False
    return True

def _record_signal(sym):
    ts = _now_ts()
    _state["last_signal_ts"][sym] = ts
    _state["last_global_signal_ts"] = ts
    _state["signals_this_hour"] = _state.get("signals_this_hour", 0) + 1


# =====================================================================
#  SIGNAL HANDLING
# =====================================================================

def _process_trade(sym, size_contracts, price, ts):
    global _state
    try:
        size_contracts = float(size_contracts)
        price = float(price)
        ts = float(ts)
    except (ValueError, TypeError):
        return

    size_usd = _size_to_usd(sym, size_contracts, price)
    if size_usd < DETECTOR_MAX_NOTIONAL_ABS:
        return

    side = _get_side(size_contracts)

    # === CVD: обновляем для КАЖДОЙ сделки (buy+sell) ДО фильтра по side ===
    # Это нужно, чтобы CVD-буфер собирал и buys и sells.
    _update_cvd_buffer(sym, side, size_usd, ts)

    # Дальше — только long-сигналы (детектор айсбергов заточен под long)
    if side != "buy":
        return

    _trade_buffer[sym].append({
        "ts": ts,
        "side": side,
        "size": size_usd,
        "id": f"{ts}_{size_contracts}_{price}",
    })

    signal = _detect_iceberg(sym, side, size_usd, ts)
    if not signal:
        return

    if not _should_emit_signal(sym):
        _stats["signals_suppressed"] += 1
        return

    # === OI-ФИЛЬТР (v2.2: YELLOW → только алерт, без входа) ===
    oi_status = _check_oi_filter(sym, signal["total_usd"])

    if oi_status == "RED":
        _stats["signals_oi_rejected"] += 1
        _stats["signals_suppressed"] += 1
        print(f"[exec_of] {sym}: OI RED — пропуск (закрытие шорта)")
        return
    if oi_status == "STALE":
        _stats["signals_oi_rejected"] += 1
        _stats["signals_suppressed"] += 1
        print(f"[exec_of] {sym}: OI STALE — пропуск (нет данных)")
        return

    # v2.2: YELLOW → больше не входит, только алерт.
    # Если хочешь возвращать half_size вход — поменяй env OF_ALLOW_YELLOW=1
    _allow_yellow = os.environ.get("OF_ALLOW_YELLOW", "0") == "1"
    if oi_status == "YELLOW" and not _allow_yellow:
        _stats["signals_oi_rejected"] += 1
        _stats["signals_suppressed"] += 1
        print(f"[exec_of] {sym}: OI YELLOW — пропуск (только алерт, без входа)")
        # Алерт всё равно отправится ниже, но без исполнения
        half_size = True
        _record_signal(sym)
        _stats["signals_total"] += 1
        _state["signals_today"] += 1
        _log_signal(signal, oi_status)
        if _state["alerts_enabled"] and B:
            oi_emoji = "⚠️ OI~"
            B.send_telegram(
                f"🧊 <b>ICEBERG {sym} 🟢 BUY</b> {oi_emoji}\n"
                f"За {DETECTOR_WINDOW_SEC}с: {signal['num_trades']} сделок по ~${signal['avg_size']:.0f}\n"
                f"Суммарно: ${signal['total_usd']:.0f}\n"
                f"Цена: {price}\n"
                f"OI: {oi_status} — ⚠️ без входа (только GREEN)"
            )
        return

    # GREEN → полный размер
    half_size = (oi_status == "YELLOW")  # сработает только если OF_ALLOW_YELLOW=1

    _record_signal(sym)
    _stats["signals_total"] += 1
    _state["signals_today"] += 1

    _log_signal(signal, oi_status)

    if _state["alerts_enabled"]:
        side_emoji = "🟢 BUY"
        oi_emoji = "✅ OI↑" if oi_status == "GREEN" else "⚠️ OI~"
        if B:
            B.send_telegram(
                f"🧊 <b>ICEBERG {sym} {side_emoji}</b> {oi_emoji}\n"
                f"За {DETECTOR_WINDOW_SEC}с: {signal['num_trades']} сделок по ~${signal['avg_size']:.0f}\n"
                f"Суммарно: ${signal['total_usd']:.0f}\n"
                f"Цена: {price}\n"
                f"OI: {oi_status}"
            )

    if not _state["enabled"]:
        return

    if _now_ts() < _state.get("cooldown_until", 0):
        return

    if B and hasattr(B, "EXECUTOR"):
        if B.EXECUTOR.halted:
            return

    if _has_position_for_sym(sym):
        return

    _reset_counters()
    if _state["new_today"] >= OF_MAX_NEW_PER_DAY:
        return
    if _get_open_count() >= OF_MAX_OPEN:
        return
    if _get_same_side_count("long") >= OF_MAX_SAME_SIDE:
        return

    # === 15m CVD MOMENTUM ФИЛЬТР (для входа) ===
    # Проверяем что CVD за 15 минут растёт (был -100 → стал 0, или был 0 → стал +50)
    # Если CVD падает (был +50 → стал -20) → пропускаем, не входить против тренда
    cvd_15m_info = None
    if OF_CVD_15M_FILTER_ENABLED:
        cvd_15m_info = _check_cvd_15m_momentum(sym)
        cvd_15m_status = cvd_15m_info["status"]
        if cvd_15m_status == "FALLING":
            _stats["signals_suppressed"] += 1
            print(f"[exec_of] {sym}: CVD 15m FALLING (momentum=${cvd_15m_info['momentum']:.0f}) — пропуск входа")
            if _state["alerts_enabled"] and B:
                B.send_telegram(
                    f"🧊 <b>ICEBERG {sym} 🟢 BUY</b> ✅ OI↑\n"
                    f"⚠️ CVD 15m: FALLING (momentum ${cvd_15m_info['momentum']:.0f})\n"
                    f"Текущий CVD: ${cvd_15m_info['cvd_now']:.0f} | 15m назад: ${cvd_15m_info['cvd_15m_ago']:.0f}\n"
                    f"→ пропускаем вход (CVD 15m падает, не входить против тренда)"
                )
            return
        # STALE и RISING → пропускаем дальше (STALE = мало данных, не блокируем)
        if cvd_15m_status == "RISING":
            print(f"[exec_of] {sym}: CVD 15m RISING (momentum=${cvd_15m_info['momentum']:.0f}) — OK для входа")

    _execute_signal(signal, price, half_size, cvd_15m_info)

def _execute_signal(signal, price, half_size=False, cvd_15m_info=None):
    if not B or not hasattr(B, "EXECUTOR"):
        return
    executor = B.EXECUTOR
    if not executor:
        return

    sym = signal["sym"]
    entry = float(price)
    s = 1  # long only

    # Динамический стоп: max(0.3%, 1.5×ATR_1min)
    # Упрощённо: используем 0.3% (ATR расчёт можно добавить позже)
    stop_pct = OF_STOP_PCT_MIN
    tp_pct = stop_pct * OF_TP_MULT

    # Если YELLOW — половинный размер
    margin = OF_MARGIN_USD * (0.5 if half_size else 1.0)

    stop = entry * (1 - s * stop_pct / 100)
    tp = entry * (1 + s * tp_pct / 100)

    sig = {
        "symbol": sym,
        "side": "long",
        "price": entry,
        "stop": stop,
        "stop_pct": stop_pct,
        "tp1_price": tp,
        "exit_mode": "tp_ladder",
        "risk_usd": margin * stop_pct / 100,
        "max_pos_usd": margin * OF_LEVERAGE,
        "max_same_side": OF_MAX_SAME_SIDE,
        "strategy": STRATEGY_TAG,
        "leverage_override": OF_LEVERAGE,
        "ext_atr": 0.0,
    }

    try:
        score = int(signal["total_usd"] / 1000)
        executor.on_signal(sig, score)

        size_str = f"${margin:.0f}" + (" (½)" if half_size else "")
        # Формируем строку с 15m CVD если есть данные
        cvd_15m_line = ""
        if cvd_15m_info and cvd_15m_info.get("status") == "RISING":
            cvd_15m_line = (
                f"\n📊 <b>CVD 15m: RISING</b> momentum ${cvd_15m_info['momentum']:.0f}\n"
                f"   сейчас: ${cvd_15m_info['cvd_now']:.0f} | 15m назад: ${cvd_15m_info['cvd_15m_ago']:.0f}"
            )
        elif cvd_15m_info and cvd_15m_info.get("status") == "STALE":
            cvd_15m_line = "\n📊 CVD 15m: STALE (мало данных, фильтр пропущен)"
        if B:
            B.send_telegram(
                f"🧊 <b>OF ВХОД {sym} LONG</b>\n"
                f"Вход: ${entry:.6g} | Стоп: ${stop:.6g} ({stop_pct}%)\n"
                f"Тейк: ${tp:.6g} ({tp_pct:.1f}%)\n"
                f"Маржа: {size_str} × {OF_LEVERAGE}x"
                f"{cvd_15m_line}\n"
                f"Time-stop: 60с→BE, 240с→CVD-чек (каждые 90с)\n"
                f"  Safety net: {OF_CVD_HARD_LIMIT_SEC}с ({OF_CVD_HARD_LIMIT_SEC//60} мин)"
            )

        _state["new_today"] += 1
        _stats["trades_opened"] += 1
        _save_state()
        _add_tracked(f"pending_{sym}_{int(_now_ts())}", sym, "long", entry, stop, tp, _now_ts())

    except Exception as e:
        if B:
            B.send_telegram(f"⚠️ OF {sym}: ошибка входа — {e}")

def _log_signal(signal, oi_status=""):
    try:
        import csv
        row = {
            "ts": int(_now_ts()),
            "sym": signal["sym"],
            "side": signal["side"],
            "total_usd": round(signal["total_usd"], 2),
            "avg_size": round(signal["avg_size"], 2),
            "num_trades": signal["num_trades"],
            "oi": oi_status,
            "executed": _state["enabled"],
        }
        header = ",".join(row.keys())
        if os.path.exists(OF_SIGNALS_CSV):
            with open(OF_SIGNALS_CSV, encoding="utf-8") as f:
                first = f.readline().strip()
            if first != header:
                os.replace(OF_SIGNALS_CSV, f"{OF_SIGNALS_CSV}.old-{int(time.time())}")
        new = not os.path.exists(OF_SIGNALS_CSV)
        with open(OF_SIGNALS_CSV, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(row.keys()))
            if new:
                w.writeheader()
            w.writerow(row)
    except Exception as e:
        print(f"[exec_of] CSV error: {e}")

def _log_trade(sym, side, entry, exit_price, pnl, reason, hold_sec):
    try:
        import csv
        row = {
            "ts": int(_now_ts()),
            "sym": sym,
            "side": side,
            "entry": round(entry, 6),
            "exit": round(exit_price, 6),
            "pnl": round(pnl, 2),
            "reason": reason,
            "hold_sec": hold_sec,
        }
        header = ",".join(row.keys())
        if os.path.exists(OF_TRADES_CSV):
            with open(OF_TRADES_CSV, encoding="utf-8") as f:
                first = f.readline().strip()
            if first != header:
                os.replace(OF_TRADES_CSV, f"{OF_TRADES_CSV}.old-{int(time.time())}")
        new = not os.path.exists(OF_TRADES_CSV)
        with open(OF_TRADES_CSV, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(row.keys()))
            if new:
                w.writeheader()
            w.writerow(row)
    except Exception as e:
        print(f"[exec_of] trades CSV error: {e}")


# =====================================================================
#  POSITION MANAGEMENT (с time-stop)
# =====================================================================

def _manage_positions():
    if not B or not hasattr(B, "EXECUTOR"):
        return
    executor = B.EXECUTOR
    if not executor or not executor.client:
        return

    with _TRACKED_LOCK:
        tracked = _load_tracked()

    if not tracked:
        return

    try:
        executor._ensure_account()
        if not executor.account_id:
            return
        live_positions = UE._as_list(executor.client.positions(executor.account_id))
        live_ids = {UE._pos_id(p) for p in live_positions if isinstance(p, dict)}
    except Exception as e:
        print(f"[exec_of] manage_positions error: {e}")
        return

    now = _now_ts()
    changed = False

    for pos_id in list(tracked.keys()):
        rec = tracked[pos_id]
        if rec.get("status") != "open":
            continue

        # Pending → real ID matching
        if pos_id.startswith("pending_"):
            sym_match = rec.get("sym")
            found_real = False
            for lp in live_positions:
                if not isinstance(lp, dict):
                    continue
                lp_sym = None
                lp_mid = UE._pos_market(lp)
                if executor._mk:
                    for s, m in executor._mk.items():
                        if str(m.get("id", "")) == lp_mid:
                            lp_sym = s
                            break
                if lp_sym == sym_match:
                    real_id = UE._pos_id(lp)
                    tracked[real_id] = rec
                    del tracked[pos_id]
                    executor._registry_set(real_id, STRATEGY_TAG)
                    changed = True
                    print(f"[exec_of] {sym_match}: pending → {real_id}")
                    found_real = True
                    break
            if not found_real:
                if now - rec.get("opened_ts", now) > 120:
                    print(f"[exec_of] {sym_match}: pending истёк — удаляем")
                    del tracked[pos_id]
                    changed = True
            continue

        # Позиция закрылась (стоп, тейк, или time-stop)
        if pos_id not in live_ids:
            entry = rec.get("entry", 0)
            exit_price = rec.get("tp", entry)
            hold_sec = int(now - rec.get("opened_ts", now))
            pnl = _fetch_realized_pnl(executor, rec["sym"], rec.get("opened_ts", now))

            _log_trade(rec["sym"], rec["side"], entry, exit_price, pnl,
                       "TP/SL/TIMEOUT", hold_sec)

            _stats["trades_closed"] += 1
            _stats["total_pnl"] += pnl
            if pnl > 0:
                _stats["wins"] += 1
                _stats["consec_losses"] = 0
            elif pnl < 0:
                _stats["losses"] += 1
                _stats["consec_losses"] += 1
                # Cooldown по символу после убытка
                if "cooldown_symbol_until" not in _state:
                    _state["cooldown_symbol_until"] = {}
                _state["cooldown_symbol_until"][rec["sym"]] = now + 1200  # 20 мин

            if _stats["consec_losses"] >= OF_COOLDOWN_LOSSES:
                _state["cooldown_until"] = now + OF_COOLDOWN_MIN * 60
                _stats["consec_losses"] = 0
                _save_state()
                if B:
                    B.send_telegram(
                        f"⏸️ OF cooldown: {OF_COOLDOWN_LOSSES} убытка подряд → "
                        f"пауза {OF_COOLDOWN_MIN} мин. PnL: ${pnl:.2f}"
                    )

            del tracked[pos_id]
            changed = True
        else:
            # === TIME-STOP ЛОГИКА (CVD-aware v2.2 — без жёсткого FORCE CLOSE) ===
            hold_sec = now - rec.get("opened_ts", now)
            exit_extension = rec.get("exit_extension_sec", 0)
            cvd_resets     = rec.get("cvd_resets", 0)

            # T+60с: подтянуть стоп в безубыток (если ещё не сделано)
            if hold_sec >= OF_BREAKEVEN_SEC and not rec.get("be_done", False):
                pos = next((p for p in live_positions if UE._pos_id(p) == pos_id), None)
                if pos:
                    mid = UE._pos_market(pos)
                    if mid:
                        entry = rec.get("entry", 0)
                        be_price = entry * 1.0001  # +0.01% — покрыть комиссию
                        info = {"id": pos_id, "mid": mid, "dir": "long"}
                        try:
                            executor.move_stop(info, be_price)
                            rec["be_done"] = True
                            rec["stop"] = be_price
                            changed = True
                            print(f"[exec_of] {rec['sym']}: BE move @ T+{int(hold_sec)}с")
                        except Exception as e:
                            print(f"[exec_of] BE move error: {e}")

            # Safety net: T+OF_CVD_HARD_LIMIT_SEC (30 мин) — закрываем принудительно
            # (safety net, не «жёсткий» таймер — срабатывает только если CVD
            # слишком долго не может дать сигнал)
            if hold_sec >= OF_CVD_HARD_LIMIT_SEC and not rec.get("exit_done", False):
                pos = next((p for p in live_positions if UE._pos_id(p) == pos_id), None)
                if pos:
                    mid = UE._pos_market(pos)
                    if mid:
                        info = {"id": pos_id, "mid": mid, "dir": "long"}
                        try:
                            res = executor.close_position(info, reason=f"OF_HARD_LIMIT_{OF_CVD_HARD_LIMIT_SEC}s")
                            rec["exit_done"] = True
                            changed = True
                            if B:
                                B.send_telegram(
                                    f"⏱️ OF {rec['sym']}: safety-net выход T+{int(hold_sec)}с\n"
                                    f"Лимит времени {OF_CVD_HARD_LIMIT_SEC}с ({res})"
                                )
                        except Exception as e:
                            print(f"[exec_of] hard-limit close error: {e}")

            # CVD-чек: T+(OF_EXIT_SEC + exit_extension) — повторяется каждые 60с
            # пока позиция жива. Без max_resets — пусть продлевается сколько нужно.
            next_exit_at = OF_EXIT_SEC + exit_extension
            if hold_sec >= next_exit_at and not rec.get("exit_done", False):
                cvd_status = _check_cvd_for_exit(rec["sym"])

                if cvd_status == "RISING":
                    # ✅ Продлеваем таймер И поднимаем TP (трейлинг) — молча, без TG
                    pos = next((p for p in live_positions if UE._pos_id(p) == pos_id), None)
                    if pos:
                        mid = UE._pos_market(pos)
                        if mid:
                            info = {"id": pos_id, "mid": mid, "dir": "long"}
                            # Получаем текущую цену из tickers
                            try:
                                tickers = B.api_get("tickers", []) if B else []
                                cur_price = None
                                for t in tickers:
                                    if isinstance(t, dict) and t.get("contract") == f"{rec['sym']}_USDT":
                                        cur_price = float(t.get("last", 0))
                                        break
                                if cur_price and cur_price > 0:
                                    new_tp = cur_price * (1 + OF_CVD_TRAIL_TP_PCT / 100)
                                    old_tp = rec.get("tp", 0)
                                    if new_tp > old_tp:
                                        try:
                                            tp_res = executor.move_tp(info, new_tp)
                                            rec["tp"] = new_tp
                                            rec["tp_trail_count"] = rec.get("tp_trail_count", 0) + 1
                                            rec["cvd_last_status"] = cvd_status
                                            changed = True
                                            print(f"[exec_of] {rec['sym']}: TP trail @ T+{int(hold_sec)}с: ${old_tp:.6g}→${new_tp:.6g}")
                                        except Exception as e:
                                            print(f"[exec_of] move_tp error: {e}")
                            except Exception as e:
                                print(f"[exec_of] price fetch for trail error: {e}")

                    # Продлеваем таймер (молча, без Telegram-сообщения)
                    rec["exit_extension_sec"] = exit_extension + OF_CVD_RESET_SEC
                    rec["cvd_resets"]         = cvd_resets + 1
                    rec["cvd_last_status"]    = cvd_status
                    changed = True
                    new_total = rec["exit_extension_sec"]
                    print(f"[exec_of] {rec['sym']}: CVD=RISING → reset {cvd_resets+1} (+{OF_CVD_RESET_SEC}с, всего +{new_total}с)")

                elif cvd_status == "NEUTRAL":
                    # ⚠️ Продлеваем таймер (молча, без trail TP и без TG)
                    rec["exit_extension_sec"] = exit_extension + OF_CVD_RESET_SEC
                    rec["cvd_resets"]         = cvd_resets + 1
                    rec["cvd_last_status"]    = cvd_status
                    changed = True
                    new_total = rec["exit_extension_sec"]
                    print(f"[exec_of] {rec['sym']}: CVD=NEUTRAL → reset {cvd_resets+1} (+{OF_CVD_RESET_SEC}с, всего +{new_total}с)")

                else:
                    # ❌ CVD FALLING или STALE → выходим (с сообщением в TG)
                    pos = next((p for p in live_positions if UE._pos_id(p) == pos_id), None)
                    if pos:
                        mid = UE._pos_market(pos)
                        if mid:
                            info = {"id": pos_id, "mid": mid, "dir": "long"}
                            try:
                                res = executor.close_position(info, reason=f"OF_EXIT_CVD_{cvd_status}")
                                rec["exit_done"] = True
                                changed = True
                                trail_count = rec.get("tp_trail_count", 0)
                                final_tp = rec.get("tp", 0)
                                if B:
                                    B.send_telegram(
                                        f"⏱️ OF {rec['sym']}: выход T+{int(hold_sec)}с\n"
                                        f"CVD={cvd_status} | продлений: {cvd_resets} | TP trail ×{trail_count} ({res})"
                                    )
                            except Exception as e:
                                print(f"[exec_of] exit close error: {e}")

    if changed:
        with _TRACKED_LOCK:
            _save_tracked(tracked)

def _fetch_realized_pnl(executor, sym, since_ts):
    try:
        m = executor._mk.get(sym) if executor._mk else None
        if not m:
            return 0.0
        asset = str(m.get("id", "")) or sym
        data = executor.client.orders_history(executor.account_id, asset, 100)
    except Exception:
        return 0.0
    total = 0.0
    for o in UE._as_list(data):
        if not isinstance(o, dict):
            continue
        ts_str = o.get("closedAt") or o.get("filledAt") or o.get("updatedAt") or o.get("createdAt")
        if not ts_str:
            continue
        try:
            if isinstance(ts_str, str) and "T" in ts_str:
                ots = dt.datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
            elif float(ts_str) > 1e11:
                ots = float(ts_str) / 1000
            else:
                ots = float(ts_str)
            if ots < since_ts - 60:
                continue
            v = None
            pv = o.get("realizedPnl")
            if pv is not None:
                t = str(pv).strip()
                if t and t not in ("None", "null"):
                    try:
                        if "." in t or "e" in t.lower():
                            v = float(t)
                        else:
                            v = float(UE.from_fp9(t))
                    except Exception:
                        pass
            if v is not None:
                total += v
        except Exception:
            continue
    return total


# =====================================================================
#  WEBSOCKET CLIENT
# =====================================================================

class GateWSClient:
    def __init__(self):
        self.ws = None
        self.running = False
        self.reconnect_delay = 1
        self.max_delay = 60
        self.last_msg_ts = 0

    def on_message(self, ws, message):
        self.last_msg_ts = _now_ts()
        try:
            data = json.loads(message)
            if not isinstance(data, dict):
                return
            if data.get("channel") != "futures.trades":
                return
            if data.get("event") != "update":
                return
            result = data.get("result")
            if not isinstance(result, list):
                result = [result]
            for trade in result:
                if not isinstance(trade, dict):
                    continue
                contract = trade.get("contract", "")
                if not contract.endswith("_USDT"):
                    continue
                sym = contract[:-5]
                if sym not in OF_PAIRS:
                    continue
                try:
                    size = float(trade.get("size", 0) or 0)
                    price = float(trade.get("price", 0) or 0)
                    ts_ms = int(float(trade.get("create_time_ms", 0) or 0))
                except (ValueError, TypeError):
                    continue
                ts = ts_ms / 1000 if ts_ms else _now_ts()
                _process_trade(sym, size, price, ts)
        except Exception as e:
            import traceback
            print(f"[exec_of] WS message error: {e}")
            traceback.print_exc()

    def on_error(self, ws, error):
        print(f"[exec_of] WS error: {error}")
        _state["ws_connected"] = False

    def on_close(self, ws, close_status_code, close_msg):
        print(f"[exec_of] WS closed: {close_status_code} {close_msg}")
        _state["ws_connected"] = False

    def on_open(self, ws):
        print("[exec_of] WS connected, subscribing...")
        _state["ws_connected"] = True
        self.reconnect_delay = 1
        payload = [f"{p}_USDT" for p in OF_PAIRS]
        sub_msg = {
            "time": int(_now_ts()),
            "channel": "futures.trades",
            "event": "subscribe",
            "payload": payload
        }
        ws.send(json.dumps(sub_msg))

    def run(self):
        self.running = True
        while self.running:
            try:
                self.ws = websocket.WebSocketApp(
                    GATE_WS_URL,
                    on_open=self.on_open,
                    on_message=self.on_message,
                    on_error=self.on_error,
                    on_close=self.on_close,
                )
                self.ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                print(f"[exec_of] WS exception: {e}")
            _state["ws_connected"] = False
            if not self.running:
                break
            print(f"[exec_of] Reconnecting in {self.reconnect_delay}s...")
            time.sleep(self.reconnect_delay)
            self.reconnect_delay = min(self.reconnect_delay * 2, self.max_delay)

    def stop(self):
        self.running = False
        if self.ws:
            self.ws.close()


# =====================================================================
#  WATCHDOG
# =====================================================================

def _watchdog_loop():
    while True:
        try:
            if _state["ws_connected"] and _now_ts() - _ws_client.last_msg_ts > 120:
                print("[exec_of] WS stale (no msgs 120s), forcing reconnect...")
                _state["ws_connected"] = False
                if _ws_client.ws:
                    _ws_client.ws.close()
            _manage_positions()
            _reset_counters()
        except Exception as e:
            print(f"[exec_of] watchdog error: {e}")
        time.sleep(15)


# =====================================================================
#  TELEGRAM COMMANDS
# =====================================================================

def status_report():
    lines = ["", "━━━ OrderFlow v2 ━━━"]
    enabled = _state.get("enabled", False)
    alerts = _state.get("alerts_enabled", True)
    ws_ok = _state.get("ws_connected", False)
    lines.append(f"Исполнитель: {'✅ ВКЛ' if enabled else '🔴 ВЫКЛ'}")
    lines.append(f"Алерты: {'✅ ВКЛ' if alerts else '🔴 ВЫКЛ'}")
    lines.append(f"WebSocket: {'✅ подключён' if ws_ok else '🔴 отключён'}")

    cd_until = _state.get("cooldown_until", 0)
    if _now_ts() < cd_until:
        remaining = int((cd_until - _now_ts()) / 60)
        lines.append(f"Cooldown: 🔴 ещё {remaining} мин")

    lines.append(f"Пар: {len(OF_PAIRS)}")
    lines.append(f"Сделок сегодня: {_state.get('new_today', 0)}/{OF_MAX_NEW_PER_DAY}")
    lines.append(f"Сигналов сегодня: {_state.get('signals_today', 0)}")
    lines.append(f"Сигналов в час: {_state.get('signals_this_hour', 0)}/{DETECTOR_MAX_PER_HOUR}")

    tracked = _load_tracked()
    open_count = sum(1 for v in tracked.values() if v.get("status") == "open")
    lines.append(f"Открыто: {open_count}/{OF_MAX_OPEN}")

    lines.append(f"Всего сигналов: {_stats['signals_total']} (OI отклонено: {_stats['signals_oi_rejected']}, подавлено: {_stats['signals_suppressed']})")
    lines.append(f"Сделок: {_stats['trades_opened']} открыто / {_stats['trades_closed']} закрыто")

    # CVD-статусы по открытым позициям
    tracked = _load_tracked()
    open_recs = [(pid, v) for pid, v in tracked.items() if v.get("status") == "open" and not pid.startswith("pending_")]
    if open_recs:
        lines.append("")
        lines.append("━━ CVD по открытым ━━")
        for pid, v in open_recs:
            sym = v.get("sym", "?")
            cvd_status = _check_cvd_for_exit(sym)
            resets = v.get("cvd_resets", 0)
            ext = v.get("exit_extension_sec", 0)
            trail_count = v.get("tp_trail_count", 0)
            tp = v.get("tp", 0)
            emoji = {"RISING": "🟢", "NEUTRAL": "🟡", "FALLING": "🔴", "STALE": "⚪"}.get(cvd_status, "?")
            lines.append(f"  {sym}: {emoji} {cvd_status} | ext +{ext}с | trail TP ×{trail_count} | TP ${tp:.6g}")
    if _stats['trades_closed'] > 0:
        wr = _stats['wins'] / _stats['trades_closed'] * 100
        lines.append(f"Winrate: {wr:.0f}% ({_stats['wins']}/{_stats['trades_closed']})")
        lines.append(f"PnL: ${_stats['total_pnl']:+.2f}")
        lines.append(f"Убытков подряд: {_stats['consec_losses']}")
    return "\n".join(lines)

def cmd_on():
    _state["enabled"] = True
    _save_state()
    return "✅ OrderFlow v2 исполнитель включён."

def cmd_off():
    _state["enabled"] = False
    _save_state()
    return "🔴 OrderFlow v2 исполнитель выключен. Алерты продолжают работать."

def cmd_alerts_toggle():
    _state["alerts_enabled"] = not _state["alerts_enabled"]
    _save_state()
    state_str = "ВКЛ" if _state["alerts_enabled"] else "ВЫКЛ"
    return f"🔔 Алерты {state_str}"

def cmd_pairs():
    lines = ["", "━━━ OF Пары + OI ━━━"]
    for p in OF_PAIRS:
        buf = _trade_buffer.get(p, deque())
        with _OI_LOCK:
            oi_buf = list(_oi_cache.get(p, deque()))
        oi_str = "нет данных"
        if len(oi_buf) >= 2:
            latest = oi_buf[-1][1]
            old = oi_buf[0][1]
            if old > 0:
                delta_pct = (latest - old) / old * 100
                oi_str = f"${latest:,.0f} (Δ {delta_pct:+.2f}%)"
        lines.append(f"  {p}: {len(buf)} сделок | OI: {oi_str}")
    return "\n".join(lines)

def cmd_log():
    if not B:
        return "⚠️ bot.py недоступен"
    sent = 0
    for path, caption in [(OF_SIGNALS_CSV, "OF signals"), (OF_TRADES_CSV, "OF trades")]:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            try:
                B.send_document(path, caption)
                sent += 1
            except Exception as e:
                print(f"[exec_of] send doc error: {e}")
    if sent == 0:
        return "📒 Журналы пока пустые."
    return f"📒 Отправлено файлов: {sent}"

def cmd_stats():
    lines = ["", "━━━ OF Статистика v2 ━━━"]
    lines.append(f"Сигналов всего: {_stats['signals_total']}")
    lines.append(f"OI отклонено: {_stats['signals_oi_rejected']}")
    lines.append(f"Подавлено (cooldown/флуд): {_stats['signals_suppressed']}")
    lines.append(f"Сделок открыто: {_stats['trades_opened']}")
    lines.append(f"Сделок закрыто: {_stats['trades_closed']}")
    if _stats['trades_closed'] > 0:
        wr = _stats['wins'] / _stats['trades_closed'] * 100
        lines.append(f"Winrate: {wr:.1f}%")
        lines.append(f"Wins: {_stats['wins']} | Losses: {_stats['losses']}")
        lines.append(f"PnL: ${_stats['total_pnl']:+.2f}")
        lines.append(f"Consec losses: {_stats['consec_losses']}")
    return "\n".join(lines)


# =====================================================================
#  ИНИЦИАЛИЗАЦИЯ И ЗАПУСК
# =====================================================================

def start():
    global _ws_thread, _ws_client, _oi_thread

    if websocket is None:
        print("[exec_of] websocket-client не установлен, сканер не запущен")
        return

    _load_state()
    _fetch_quanto_multipliers()

    # Запускаем OI-поллер (REST каждые 10с)
    _oi_thread = threading.Thread(target=_oi_loop, daemon=True, name="of-oi")
    _oi_thread.start()

    # Запускаем WS-клиент
    _ws_client = GateWSClient()
    _ws_thread = threading.Thread(target=_ws_client.run, daemon=True, name="of-ws")
    _ws_thread.start()

    # Запускаем watchdog
    wd = threading.Thread(target=_watchdog_loop, daemon=True, name="of-watchdog")
    wd.start()

    # Проверяем где храним state (на Render должен быть /var/data)
    _data_dir_actual = os.environ.get("BOT_DATA_DIR", "/tmp (default — НЕ персистентно!)")
    _disk_ok = "✅ Persistent Disk" if _data_dir_actual != "/tmp (default — НЕ персистентно!)" else "⚠️ /tmp (стирается при деплое!)"

    print(f"[exec_of] v2 Старт: {len(OF_PAIRS)} пар, WS={GATE_WS_URL}, OI poll=10с")
    if B:
        # Статус 15m CVD фильтра
        cvd_15m_filter_line = (
            f"✅ ВКЛ (окно {OF_CVD_15M_WINDOW_SEC//60}мин, min momentum ${OF_CVD_15M_MIN_MOMENTUM:.0f})"
            if OF_CVD_15M_FILTER_ENABLED else "🔴 ВЫКЛ"
        )
        B.send_telegram(
            f"🧊 <b>OrderFlow v2.2 запущен</b>\n"
            f"Пар: {len(OF_PAIRS)}\n"
            f"Маржа: ${OF_MARGIN_USD} × {OF_LEVERAGE}x\n"
            f"Стоп: {OF_STOP_PCT_MIN}% | Тейк: {OF_STOP_PCT_MIN * OF_TP_MULT:.1f}% (RR 1:{OF_TP_MULT:.0f})\n"
            f"OI-фильтр: ✅ (poll 10с, окно 30с)\n"
            f"Фильтр входа CVD 15m: {cvd_15m_filter_line}\n"
            f"Time-stop (CVD-aware): 60с→BE, 240с→CVD-чек каждые 90с\n"
            f"  RISING→+90с+trail TP, NEUTRAL→+90с, FALLING/STALE→выход\n"
            f"  Safety net: {OF_CVD_HARD_LIMIT_SEC}с (60 мин)\n"
            f"Cooldown: {DETECTOR_COOLDOWN_PER_SYMBOL}с/символ, {DETECTOR_GLOBAL_COOLDOWN}с глобальный\n"
            f"Лимит: {DETECTOR_MAX_PER_HOUR} сигнала/час\n"
            f"Порог айсберга: ${DETECTOR_MIN_TOTAL_NOTIONAL:,.0f}\n"
            f"State dir: {_disk_ok}\n"
            f"  → {_data_dir_actual}\n"
            f"Исполнитель: {'✅ ВКЛ' if _state['enabled'] else '🔴 ВЫКЛ'}\n"
            f"Алерты: {'✅ ВКЛ' if _state['alerts_enabled'] else '🔴 ВЫКЛ'}"
        )


if __name__ == "__main__":
    print("exec_orderflow.py v2 — OrderFlow + OI Filter")
