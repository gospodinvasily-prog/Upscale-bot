# -*- coding: utf-8 -*-
"""
exec_orderflow.py — Скальперский бот OrderFlow (Iceberg Detector)
================================================================================
Ловит "айсберги" (крупные игроки, дробящие заявки) через WebSocket Gate.io.
При обнаружении — открывает микро-сделку на демо Upscale (через общий EXECUTOR).

Работает ПОЛНОСТЬЮ автономно, параллельно с 4H-ботом.
Изолирован через strategy_registry (тег "orderflow_v1").

Параметры сделок:
  - Маржа: $50 (notional $250-500 при 5-10x)
  - Стоп: 0.5% (сразу со входом)
  - Тейк: 0.5% (через 65 сек по правилу Upscale)
  - Force close: через 300 сек (5 мин), если ни стоп, ни тейк не сработали

Управление через Telegram:
  /of         — статус
  /of_on      — включить исполнитель (бот открывает сделки)
  /of_off     — выключить исполнитель (только алерты)
  /of_alerts  — вкл/выкл алерты
  /of_pairs   — список пар и последние сигналы
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
    import websocket  # websocket-client
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

STRATEGY_NAME = "OrderFlow v1"
STRATEGY_TAG  = "orderflow_v1"

# --- Список пар (из env или дефолт 30 пар) ───────────────────────────────────
OF_PAIRS_DEFAULT = [
    "BTC", "ETH", "SOL", "XRP", "LINK", "TRX", "DOT", "LTC", "BCH",
    "AVAX", "NEAR", "UNI", "APT", "FIL", "ATOM", "ARB", "OP", "INJ",
    "AAVE", "ALGO", "ETC", "XLM", "HBAR", "ICP", "STX", "SUI", "SEI",
    "TIA", "RUNE", "ENA"
]
OF_PAIRS = [p.strip() for p in os.environ.get("OF_PAIRS", ",".join(OF_PAIRS_DEFAULT)).split(",") if p.strip()]

# --- Параметры сделок ────────────────────────────────────────────────────────
OF_MARGIN_USD    = float(os.environ.get("OF_MARGIN_USD", "50"))
OF_LEVERAGE      = int(os.environ.get("OF_LEVERAGE", "10"))
OF_STOP_PCT      = float(os.environ.get("OF_STOP_PCT", "0.5"))   # 0.5%
OF_TP_PCT        = float(os.environ.get("OF_TP_PCT", "0.5"))     # 0.5%
OF_MAX_OPEN      = int(os.environ.get("OF_MAX_OPEN", "5"))
OF_MAX_SAME_SIDE = int(os.environ.get("OF_MAX_SAME_SIDE", "3"))
OF_MAX_NEW_PER_DAY = int(os.environ.get("OF_MAX_NEW_PER_DAY", "20"))
OF_FORCE_CLOSE_SEC = int(os.environ.get("OF_FORCE_CLOSE_SEC", "300"))
OF_COOLDOWN_LOSSES = int(os.environ.get("OF_COOLDOWN_LOSSES", "4"))
OF_COOLDOWN_MIN    = int(os.environ.get("OF_COOLDOWN_MIN", "60"))

# --- Параметры детектора айсбергов ──────────────────────────────────────────
DETECTOR_WINDOW_SEC          = 15
DETECTOR_MIN_TRADES          = 4
DETECTOR_SIZE_TOLERANCE_PCT  = 15.0
DETECTOR_MIN_TOTAL_NOTIONAL = 20000.0
DETECTOR_MIN_IMBALANCE_RATIO = 3.0
DETECTOR_COOLDOWN_PER_SYMBOL = 120
DETECTOR_MAX_SIGNALS_PER_MIN = 3
DETECTOR_MIN_NOTIONAL_ABS    = 500.0
DETECTOR_PERCENTILE          = 95
DETECTOR_PERCENTILE_WINDOW   = 1800  # 30 мин

# --- WebSocket Gate.io ───────────────────────────────────────────────────────
GATE_WS_URL = "wss://fx-ws.gateio.ws/v4/ws/usdt"
GATE_REST   = "https://api.gateio.ws/api/v4/futures/usdt"

# --- Файлы состояния ────────────────────────────────────────────────────────
OF_STATE_FILE   = "/tmp/orderflow_state.json"
OF_TRACKED_FILE = "/tmp/orderflow_tracked.json"
OF_SIGNALS_CSV  = "/tmp/orderflow_signals.csv"
OF_TRADES_CSV  = "/tmp/orderflow_trades.csv"

# --- Блокировки ─────────────────────────────────────────────────────────────
_STATE_LOCK   = threading.Lock()
_TRACKED_LOCK = threading.Lock()
_SIGNALS_LOCK = threading.Lock()

# --- Глобальное состояние ────────────────────────────────────────────────────
_ws_thread = None
_ws_client = None
_state = {
    "enabled": False,         # исполнитель вкл/выкл
    "alerts_enabled": True,  # алерты вкл/выкл
    "ws_connected": False,
    "last_signal_ts": {},
    "signals_today": 0,
    "signals_today_date": "",
    "new_today": 0,
    "new_today_date": "",
    "consec_losses": 0,
    "cooldown_until": 0,
}

# --- Буферы сделок (in-memory) ─────────────────────────────────────────────
_trade_buffer = defaultdict(lambda: deque(maxlen=200))
_quanto_cache = {}

# --- Статистика ─────────────────────────────────────────────────────────────
_stats = {
    "signals_total": 0,
    "signals_suppressed": 0,
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

def _percentile(values, p):
    if not values: return 0.0
    s = sorted(values)
    k = int(len(s) * p / 100)
    if k >= len(s): k = len(s) - 1
    return s[k]


# =====================================================================
#  STATE MANAGEMENT
# =====================================================================

def _save_state():
    with _STATE_LOCK:
        data = {
            "enabled": _state["enabled"],
            "alerts_enabled": _state["alerts_enabled"],
            "cooldown_until": _state["cooldown_until"],
            "consec_losses": _stats["consec_losses"],  # из _stats, не из _state
            "signals_today": _state.get("signals_today", 0),
            "new_today": _state.get("new_today", 0),
            "signals_today_date": _state.get("signals_today_date", ""),
            "new_today_date": _state.get("new_today_date", ""),
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
            _stats["consec_losses"] = saved.get("consec_losses", 0)  # в _stats, не в _state
            _state["signals_today"] = saved.get("signals_today", 0)
            _state["new_today"] = saved.get("new_today", 0)
            _state["signals_today_date"] = saved.get("signals_today_date", _today_utc_str())
            _state["new_today_date"] = saved.get("new_today_date", _today_utc_str())
        else:
            _state["signals_today_date"] = _today_utc_str()
            _state["new_today_date"] = _today_utc_str()

def _reset_daily_counters():
    today = _today_utc_str()
    if _state.get("signals_today_date") != today:
        _state["signals_today"] = 0
        _state["signals_today_date"] = today
    if _state.get("new_today_date") != today:
        _state["new_today"] = 0
        _state["new_today_date"] = today


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
            "strategy": STRATEGY_TAG, "status": "open",
        }
        _save_tracked(tracked)

def _remove_tracked(pos_id):
    with _TRACKED_LOCK:
        tracked = _load_tracked()
        if pos_id in tracked:
            del tracked[pos_id]
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
#  QUANTO MULTIPLIER (перевод size контрактов в USD)
# =====================================================================

def _fetch_quanto_multipliers():
    """Загружает quanto_multiplier для всех пар с Gate.io REST API."""
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
    """Переводит размер в контрактах в USD notional."""
    qm = _quanto_cache.get(sym, 1.0)
    return abs(float(size_contracts)) * qm * float(price)

def _get_side(size):
    """В Gate.io futures: size > 0 = buy (long), size < 0 = sell (short)."""
    return "buy" if float(size) > 0 else "sell"


# =====================================================================
#  ICEBERG DETECTOR
# =====================================================================

def _detect_iceberg(sym, side, size_usd, ts):
    """Проверяет, является ли новая сделка частью айсберга.
    Возвращает dict с сигналом или None."""
    # Жёсткая конвертация
    try:
        ts = float(ts)
        size_usd = float(size_usd)
    except (ValueError, TypeError):
        return None

    buf = _trade_buffer[sym]

    # Чистим старые
    while buf and float(buf[0].get("ts", 0)) < ts - DETECTOR_WINDOW_SEC:
        buf.popleft()

    # Разделяем на buy/sell
    buys = [t for t in buf if t.get("side") == "buy"]
    sells = [t for t in buf if t.get("side") == "sell"]
    buy_vol = sum(float(t.get("size", 0)) for t in buys)
    sell_vol = sum(float(t.get("size", 0)) for t in sells)

    # Определяем доминирующую сторону
    if buy_vol >= sell_vol * DETECTOR_MIN_IMBALANCE_RATIO:
        dom_side, dom_trades = "buy", buys
    elif sell_vol >= buy_vol * DETECTOR_MIN_IMBALANCE_RATIO:
        dom_side, dom_trades = "sell", sells
    else:
        return None

    if len(dom_trades) < DETECTOR_MIN_TRADES:
        return None

    # Кластеризация по размеру
    sizes = sorted([float(t.get("size", 0)) for t in dom_trades])
    for i in range(len(sizes) - DETECTOR_MIN_TRADES + 1):
        window = sizes[i:i + DETECTOR_MIN_TRADES]
        med = _median(window)
        if med <= 0:
            continue
        if all(abs(float(s) - med) / med <= DETECTOR_SIZE_TOLERANCE_PCT / 100 for s in window):
            cluster_total = sum(window)
            if cluster_total >= DETECTOR_MIN_TOTAL_NOTIONAL:
                return {
                    "sym": sym,
                    "side": dom_side,
                    "total_usd": cluster_total,
                    "avg_size": med,
                    "num_trades": len(window),
                    "ts": ts,
                }
    return None

def _should_emit_signal(sym):
    """Проверяет cooldown и флуд-лимиты."""
    ts = _now_ts()

    # Cooldown по символу
    last = _state["last_signal_ts"].get(sym, 0)
    if ts - last < DETECTOR_COOLDOWN_PER_SYMBOL:
        return False

    # Флуд-лимит: max 3 сигнала в минуту
    now_min = int(ts / 60)
    signals_this_min = _state.get("signals_per_min", {}).get(str(now_min), 0)
    if signals_this_min >= DETECTOR_MAX_SIGNALS_PER_MIN:
        return False

    return True

def _record_signal(sym):
    ts = _now_ts()
    _state["last_signal_ts"][sym] = ts
    now_min = str(int(ts / 60))
    if "signals_per_min" not in _state:
        _state["signals_per_min"] = {}
    _state["signals_per_min"][now_min] = _state["signals_per_min"].get(now_min, 0) + 1
    # Чистим старые минуты
    for k in list(_state["signals_per_min"].keys()):
        if int(k) < now_min - 2:
            del _state["signals_per_min"][k]


# =====================================================================
#  SIGNAL HANDLING
# =====================================================================

def _process_trade(sym, size_contracts, price, ts):
    """Обрабатывает одну сделку из WebSocket."""
    global _state

    # Жёсткая конвертация всех входных данных в числа
    try:
        size_contracts = float(size_contracts)
        price = float(price)
        ts = float(ts)
    except (ValueError, TypeError):
        return

    size_usd = _size_to_usd(sym, size_contracts, price)
    if size_usd < DETECTOR_MIN_NOTIONAL_ABS:
        return

    side = _get_side(size_contracts)

    # Добавляем в буфер
    _trade_buffer[sym].append({
        "ts": ts,
        "side": side,
        "size": size_usd,
    })

    # Проверяем детектор
    signal = _detect_iceberg(sym, side, size_usd, ts)
    if not signal:
        return

    # Проверяем cooldown и флуд-лимиты
    if not _should_emit_signal(sym):
        _stats["signals_suppressed"] += 1
        return

    _record_signal(sym)
    _stats["signals_total"] += 1
    _state["signals_today"] += 1

    # Записываем в CSV
    _log_signal(signal)

    # Алерт в Telegram
    if _state["alerts_enabled"]:
        side_emoji = "🟢 BUY" if signal["side"] == "buy" else "🔴 SELL"
        sym_str = signal["sym"]
        if B:
            B.send_telegram(
                f"🧊 <b>ICEBERG {sym_str} {side_emoji}</b>\n"
                f"За {DETECTOR_WINDOW_SEC}с: {signal['num_trades']} сделок по ~${signal['avg_size']:.0f}\n"
                f"Суммарно: ${signal['total_usd']:.0f}\n"
                f"Цена: {price}"
            )

    # Если исполнитель выключен — не торгуем
    if not _state["enabled"]:
        return

    # Проверяем cooldown после серии убытков
    if _now_ts() < _state.get("cooldown_until", 0):
        if B:
            B.send_telegram(f"⏸️ OrderFlow cooldown (consec losses). Пропуск сигнала {sym_str}.")
        return

    # Проверяем, что 4H-бот не в Daily Stop
    if B and hasattr(B, "EXECUTOR"):
        if B.EXECUTOR.halted:
            return

    # Проверяем изоляцию: если по символу уже есть позиция (любая стратегия)
    if _has_position_for_sym(sym):
        return

    # Проверяем лимиты
    if _get_open_count() >= OF_MAX_OPEN:
        return
    signal_side_str = "long" if signal["side"] == "buy" else "short"
    if _get_same_side_count(signal_side_str) >= OF_MAX_SAME_SIDE:
        return

    # Проверяем дневной лимит
    _reset_daily_counters()
    if _state["new_today"] >= OF_MAX_NEW_PER_DAY:
        return

    # Открываем сделку
    _execute_signal(signal, price)

def _execute_signal(signal, price):
    """Открывает сделку на Upscale."""
    if not B or not hasattr(B, "EXECUTOR"):
        return
    executor = B.EXECUTOR
    if not executor:
        return

    sym = signal["sym"]
    side_str = "long" if signal["side"] == "buy" else "short"
    s = 1 if side_str == "long" else -1

    entry = float(price)
    stop = entry * (1 - s * OF_STOP_PCT / 100)
    tp = entry * (1 + s * OF_TP_PCT / 100)

    # Формируем сигнал для EXECUTOR
    # risk_usd = то, что мы готовы потерять = маржа × стоп% = $50 × 0.5% = $0.25
    # max_pos_usd = максимальный notional = маржа × плечо = $50 × 10 = $500
    sig = {
        "symbol": sym,
        "side": side_str,
        "price": entry,
        "stop": stop,
        "stop_pct": OF_STOP_PCT,
        "tp1_price": tp,
        "exit_mode": "tp_ladder",
        "risk_usd": OF_MARGIN_USD * OF_STOP_PCT / 100,   # $0.25 — риск по стопу
        "max_pos_usd": OF_MARGIN_USD * OF_LEVERAGE,        # $500 — потолок notional
        "max_same_side": OF_MAX_SAME_SIDE,
        "strategy": STRATEGY_TAG,
        "leverage_override": OF_LEVERAGE,
        "ext_atr": 0.0,
    }

    try:
        score = int(signal["total_usd"] / 1000)
        executor.on_signal(sig, score)

        if B:
            B.send_telegram(
                f"🧊 <b>OF ВХОД {sym} {side_str.upper()}</b>\n"
                f"Вход: ${entry:.6g} | Стоп: ${stop:.6g} ({OF_STOP_PCT}%)\n"
                f"Тейк (через 65с): ${tp:.6g} ({OF_TP_PCT}%)\n"
                f"Маржа: ${OF_MARGIN_USD} × {OF_LEVERAGE}x"
            )

        # Инкрементируем счётчики ПОСЛЕ on_signal (ордер уйдёт асинхронно)
        # Если ордер отклонён — _handle() запишет skip в CSV, но счётчик уже инкрементирован.
        # Это приемлемый компромисс: 1-2 ложных инкремента не сломают лимит 20/день.
        _state["new_today"] += 1
        _stats["trades_opened"] += 1
        _save_state()

        # Записываем в tracked. pos_id обновится в _manage_positions,
        # когда найдём реальную позицию на Upscale.
        _add_tracked(f"pending_{sym}_{int(_now_ts())}", sym, side_str, entry, stop, tp, _now_ts())

    except Exception as e:
        if B:
            B.send_telegram(f"⚠️ OF {sym}: ошибка входа — {e}")

def _log_signal(signal):
    """Записывает сигнал в CSV."""
    try:
        import csv
        row = {
            "ts": int(_now_ts()),
            "sym": signal["sym"],
            "side": signal["side"],
            "total_usd": round(signal["total_usd"], 2),
            "avg_size": round(signal["avg_size"], 2),
            "num_trades": signal["num_trades"],
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
    """Записывает закрытую сделку в CSV."""
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
#  POSITION MANAGEMENT (проверка закрытий и force-close)
# =====================================================================

def _manage_positions():
    """Периодически проверяет открытые OF-позиции:
    - Если исчезла → записать PnL, удалить из tracked
    - Если открыта > FORCE_CLOSE_SEC → закрыть по рынку"""
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

        if pos_id not in live_ids:
            # Позиция закрылась (стоп, тейк, или force-close)
            entry = rec.get("entry", 0)
            exit_price = rec.get("tp", entry)
            hold_sec = int(now - rec.get("opened_ts", now))
            pnl = _fetch_realized_pnl(executor, rec["sym"], rec.get("opened_ts", now))

            _log_trade(rec["sym"], rec["side"], entry, exit_price, pnl,
                       "TP/SL/END", hold_sec)

            _stats["trades_closed"] += 1
            _stats["total_pnl"] += pnl
            if pnl > 0:
                _stats["wins"] += 1
                _stats["consec_losses"] = 0
            else:
                _stats["losses"] += 1
                _stats["consec_losses"] += 1

            if _stats["consec_losses"] >= OF_COOLDOWN_LOSSES:
                _state["cooldown_until"] = now + OF_COOLDOWN_MIN * 60
                _stats["consec_losses"] = 0
                _save_state()
                if B:
                    B.send_telegram(
                        f"⏸️ OrderFlow cooldown: {OF_COOLDOWN_LOSSES} убытка подряд → "
                        f"пауза {OF_COOLDOWN_MIN} мин. PnL: ${pnl:.2f}"
                    )

            del tracked[pos_id]
            changed = True
        else:
            # Проверяем force-close
            if now - rec.get("opened_ts", now) > OF_FORCE_CLOSE_SEC:
                # Закрываем по рынку
                pos = next((p for p in live_positions if UE._pos_id(p) == pos_id), None)
                if pos:
                    info = {"id": pos_id, "mid": UE._pos_market(pos), "dir": UE._pos_dir(pos)}
                    try:
                        res = executor.close_position(info, reason=f"OF_TIMEOUT_{OF_FORCE_CLOSE_SEC}s")
                        if B:
                            B.send_telegram(f"⏱️ OF {rec['sym']}: закрыт по таймауту ({res})")
                    except Exception as e:
                        print(f"[exec_of] force close {pos_id}: {e}")

    if changed:
        with _TRACKED_LOCK:
            _save_tracked(tracked)

def _fetch_realized_pnl(executor, sym, since_ts):
    """Берёт realizedPnl из orders_history."""
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
                            v = float(Decimal(t))
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
#  WEBSOCKET CLIENT (Gate.io)
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
                # Gate.io присылает size/price/ts как строки — строго конвертируем в числа
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
        # Подписка на сделки
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

            # Exponential backoff
            print(f"[exec_of] Reconnecting in {self.reconnect_delay}s...")
            time.sleep(self.reconnect_delay)
            self.reconnect_delay = min(self.reconnect_delay * 2, self.max_delay)

    def stop(self):
        self.running = False
        if self.ws:
            self.ws.close()


# =====================================================================
#  WATCHDOG (мониторинг и управление)
# =====================================================================

def _watchdog_loop():
    """Проверяет здоровье WS и управляет позициями."""
    while True:
        try:
            # Проверяем, что WS жив (last_msg < 120 сек назад)
            if _state["ws_connected"] and _now_ts() - _ws_client.last_msg_ts > 120:
                print("[exec_of] WS stale (no msgs 120s), forcing reconnect...")
                _state["ws_connected"] = False
                if _ws_client.ws:
                    _ws_client.ws.close()

            # Управление позициями
            _manage_positions()

            # Сброс дневных счётчиков
            _reset_daily_counters()

        except Exception as e:
            print(f"[exec_of] watchdog error: {e}")
        time.sleep(15)


# =====================================================================
#  TELEGRAM COMMANDS
# =====================================================================

def status_report():
    lines = ["", "━━━ OrderFlow v1 ━━━"]

    # Состояние
    enabled = _state.get("enabled", False)
    alerts = _state.get("alerts_enabled", True)
    ws_ok = _state.get("ws_connected", False)
    lines.append(f"Исполнитель: {'✅ ВКЛ' if enabled else '🔴 ВЫКЛ'}")
    lines.append(f"Алерты: {'✅ ВКЛ' if alerts else '🔴 ВЫКЛ'}")
    lines.append(f"WebSocket: {'✅ подключён' if ws_ok else '🔴 отключён'}")

    # Cooldown
    cd_until = _state.get("cooldown_until", 0)
    if _now_ts() < cd_until:
        remaining = int((cd_until - _now_ts()) / 60)
        lines.append(f"Cooldown: 🔴 ещё {remaining} мин")

    # Пары
    lines.append(f"Пар: {len(OF_PAIRS)}")
    lines.append(f"Сделок сегодня: {_state.get('new_today', 0)}/{OF_MAX_NEW_PER_DAY}")
    lines.append(f"Сигналов сегодня: {_state.get('signals_today', 0)}")

    # Открытые позиции
    tracked = _load_tracked()
    open_count = sum(1 for v in tracked.values() if v.get("status") == "open")
    lines.append(f"Открыто: {open_count}/{OF_MAX_OPEN}")

    # Статистика
    lines.append(f"Всего сигналов: {_stats['signals_total']} (подавлено: {_stats['signals_suppressed']})")
    lines.append(f"Сделок: {_stats['trades_opened']} открыто / {_stats['trades_closed']} закрыто")
    if _stats['trades_closed'] > 0:
        wr = _stats['wins'] / _stats['trades_closed'] * 100
        lines.append(f"Winrate: {wr:.0f}% ({_stats['wins']}/{_stats['trades_closed']})")
        lines.append(f"PnL: ${_stats['total_pnl']:+.2f}")
        lines.append(f"Убытков подряд: {_stats['consec_losses']}")

    return "\n".join(lines)

def cmd_on():
    _state["enabled"] = True
    _save_state()
    return "✅ OrderFlow исполнитель включён. Бот будет открывать сделки при сигналах."

def cmd_off():
    _state["enabled"] = False
    _save_state()
    return "🔴 OrderFlow исполнитель выключен. Алерты продолжают работать."

def cmd_alerts_toggle():
    _state["alerts_enabled"] = not _state["alerts_enabled"]
    _save_state()
    state_str = "ВКЛ" if _state["alerts_enabled"] else "ВЫКЛ"
    return f"🔔 Алерты {state_str}"

def cmd_pairs():
    lines = ["", "━━━ OF Пары ━━━"]
    for p in OF_PAIRS:
        buf = _trade_buffer.get(p, deque())
        lines.append(f"  {p}: {len(buf)} сделок в буфере")
    return "\n".join(lines)

def cmd_log():
    """Присылает CSV файл."""
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
    lines = ["", "━━━ OF Статистика ━━━"]
    lines.append(f"Сигналов всего: {_stats['signals_total']}")
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
    """Запускает OrderFlow сканер (WebSocket + watchdog)."""
    global _ws_thread, _ws_client

    if websocket is None:
        print("[exec_of] websocket-client не установлен, сканер не запущен")
        return

    _load_state()

    # Загружаем quanto multipliers
    _fetch_quanto_multipliers()

    # Запускаем WS-клиент в отдельном потоке
    _ws_client = GateWSClient()
    _ws_thread = threading.Thread(target=_ws_client.run, daemon=True, name="of-ws")
    _ws_thread.start()

    # Запускаем watchdog
    wd = threading.Thread(target=_watchdog_loop, daemon=True, name="of-watchdog")
    wd.start()

    print(f"[exec_of] Старт: {len(OF_PAIRS)} пар, WS={GATE_WS_URL}")
    if B:
        B.send_telegram(
            f"🧊 <b>OrderFlow v1 запущен</b>\n"
            f"Пар: {len(OF_PAIRS)}\n"
            f"Маржа: ${OF_MARGIN_USD} × {OF_LEVERAGE}x\n"
            f"Стоп/Тейк: {OF_STOP_PCT}%/{OF_TP_PCT}%\n"
            f"Исполнитель: {'✅ ВКЛ' if _state['enabled'] else '🔴 ВЫКЛ'}\n"
            f"Алерты: {'✅ ВКЛ' if _state['alerts_enabled'] else '🔴 ВЫКЛ'}"
        )


# =====================================================================
#  ТОЧКА ВХОДА
# =====================================================================

if __name__ == "__main__":
    print("exec_orderflow.py — модуль скальперского бота OrderFlow")
    print()
    print("Функции для bot.py:")
    print("  start()                — запустить WS + watchdog")
    print("  status_report()        — статус для /of")
    print("  cmd_on() / cmd_off()   — вкл/выкл исполнитель")
    print("  cmd_alerts_toggle()    — вкл/выкл алерты")
    print("  cmd_pairs()            — список пар")
    print("  cmd_log()              — прислать CSV журналы")
    print("  cmd_stats()             — статистика winrate/pnl")
