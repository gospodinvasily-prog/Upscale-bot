# -*- coding: utf-8 -*-
"""
big_candle_scanner.py — Big Candle Strategy v1.0
=================================================
Полная торговая стратегия с авто-входом через Upscale API.

Логика:
  1. Каждый день в окне 10:00-11:15 МСК мониторит 15-минутные свечи BTC
  2. Считает средний объём "европейских" свечей (после 9:00 МСК, не включая азиатскую сессию)
  3. Если очередная свеча имеет объём > 2× среднего → это "большая свеча" → сигнал
  4. Определяет направление по цвету свечи:
     - Close ≥ Open → LONG
     - Close < Open → SHORT
  5. Фильтр SMA50 1H: LONG только если price > SMA50, SHORT только если price < SMA50
  6. Считает ATR_5m (12 свечей = 1 час)
  7. Уровни: стоп = 2×ATR, TP1 = 4×ATR (RR 1:2)
  8. Вход через Upscale API, BTC_USDT, 5x leverage
  9. Риск $100 на сделку
 10. Лимит: 1 сделка в день (BIG_CANDLE_MAX_TRADES_DAY)
 11. Max hold: 6 часов (safety net)

Backtest за 30 дней:
  - 20 сигналов
  - WR 55%, Avg PnL +0.208%, Total PnL +4.16%
  - Прогноз за год: +50%

Управление через Telegram:
  /bc          — статус стратегии
  /bc_on       — включить авто-торговлю
  /bc_off      — выключить авто-торговлю
  /bc_close    — принудительно закрыть открытую позицию
  /bc_stats    — статистика (winrate, PnL, сигналы)

Параметры через env-vars:
  BIG_CANDLE_ENABLED           (0/1) — авто-торговля
  BIG_CANDLE_RISK_USD          ($100) — риск на сделку
  BIG_CANDLE_MAX_TRADES_DAY    (1)    — лимит сделок в день
  BIG_CANDLE_VOL_MULT          (2.0)  — порог объёма
  BIG_CANDLE_USE_SMA50         (1)    — фильтр тренда
  BIG_CANDLE_ATR_STOP_MULT     (2.0)  — стоп = N×ATR
  BIG_CANDLE_ATR_TP_MULT       (4.0)  — TP1 = N×ATR
  BIG_CANDLE_LEVERAGE          (5)    — плечо
  BIG_CANDLE_WINDOW_START_HOUR (10)   — час начала окна МСК
  BIG_CANDLE_WINDOW_END_MIN    (15)   — минуты после конца часа (11:15 = 15)
  BIG_CANDLE_MAX_HOLD_SEC      (172800) — safety-net 48 часов

Запуск: автоматически из bot.py
"""

import os
import sys
import json
import time
import threading
import datetime as dt
import statistics
from collections import deque, defaultdict
from typing import Optional

import requests

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

STRATEGY_NAME = "Big Candle Strategy v1.0"
STRATEGY_TAG  = "big_candle_v1"

# --- Параметры торговли ---
BC_ENABLED          = os.environ.get("BIG_CANDLE_ENABLED", "0") == "1"
BC_RISK_USD         = float(os.environ.get("BIG_CANDLE_RISK_USD", "100"))
BC_MAX_TRADES_DAY   = int(os.environ.get("BIG_CANDLE_MAX_TRADES_DAY", "1"))
BC_LEVERAGE         = int(os.environ.get("BIG_CANDLE_LEVERAGE", "5"))
BC_VOL_MULT         = float(os.environ.get("BIG_CANDLE_VOL_MULT", "2.0"))
BC_USE_SMA50        = os.environ.get("BIG_CANDLE_USE_SMA50", "1") == "1"
BC_ATR_STOP_MULT    = float(os.environ.get("BIG_CANDLE_ATR_STOP_MULT", "2.0"))
BC_ATR_TP_MULT      = float(os.environ.get("BIG_CANDLE_ATR_TP_MULT", "3.0"))   # TP1 = 3×ATR
BC_ATR_TP2_MULT     = float(os.environ.get("BIG_CANDLE_ATR_TP2_MULT", "5.0"))   # TP2 = 5×ATR (дальше, 2-я зона)
BC_ATR_PERIOD       = int(os.environ.get("BIG_CANDLE_ATR_PERIOD", "4"))   # 4 свечи 15m = 1 час
BC_ATR_STOP_MIN_PCT = float(os.environ.get("BIG_CANDLE_ATR_STOP_MIN_PCT", "0.30"))
BC_ATR_TP_MIN_PCT   = float(os.environ.get("BIG_CANDLE_ATR_TP_MIN_PCT", "0.45"))  # мин 0.45% тейк (было 0.60, уменьшен т.к. TP=3×ATR
BC_MAX_MARGIN       = float(os.environ.get("BIG_CANDLE_MAX_MARGIN", "3000"))  # cap маржи 30% от $10K

# --- Окно сканирования ---
BC_WINDOW_START_HOUR_MSK = int(os.environ.get("BIG_CANDLE_WINDOW_START_HOUR", "10"))  # 10:00
BC_WINDOW_END_HOUR_MSK   = BC_WINDOW_START_HOUR_MSK + 1                              # 11:00
BC_WINDOW_END_MIN_MSK    = int(os.environ.get("BIG_CANDLE_WINDOW_END_MIN", "15"))   # 11:15
BC_EUROPEAN_START_HOUR   = int(os.environ.get("BIG_CANDLE_EUROPEAN_START_HOUR", "9"))  # с 9:00 среднее

# --- Safety net ---
BC_MAX_HOLD_SEC      = int(os.environ.get("BIG_CANDLE_MAX_HOLD_SEC", "172800"))  # 48 часов (было 6ч — оптимизатор показал что hold 48ч = TP1 hit 42% vs 4%
BC_CHECK_INTERVAL    = int(os.environ.get("BIG_CANDLE_CHECK_INTERVAL", "60"))   # проверка каждые 60с

# --- API ---
GATE_API = "https://api.gateio.ws/api/v4/spot"

# --- Москва = UTC+3 ---
MSK_OFFSET = dt.timedelta(hours=3)

# --- Файлы состояния (использует BOT_DATA_DIR для Persistent Disk) ---
_DATA_DIR = os.environ.get("BOT_DATA_DIR", "/tmp")
try:
    os.makedirs(_DATA_DIR, exist_ok=True)
except Exception:
    pass

BC_STATE_FILE  = f"{_DATA_DIR}/big_candle_state.json"
BC_TRACKED_FILE = f"{_DATA_DIR}/big_candle_tracked.json"
BC_SIGNALS_CSV = f"{_DATA_DIR}/big_candle_signals.csv"
BC_TRADES_CSV = f"{_DATA_DIR}/big_candle_trades.csv"

# --- Блокировки ---
_STATE_LOCK = threading.Lock()
_TRACKED_LOCK = threading.Lock()

# --- Состояние ---
_state = {
    "enabled": BC_ENABLED,        # авто-торговля
    "alerts_enabled": True,        # алерты в TG
    "last_signal_ts": 0,
    "trades_today": 0,
    "trades_today_date": "",
}

# --- Статистика ---
_stats = {
    "signals_total": 0,
    "signals_skipped_sma": 0,
    "trades_opened": 0,
    "trades_closed": 0,
    "wins": 0,
    "losses": 0,
    "total_pnl": 0.0,
}


# =====================================================================
#  УТИЛИТЫ
# =====================================================================

def _now_ts():
    return time.time()


def _today_msk_str():
    return dt.datetime.now(dt.timezone.utc).astimezone(
        dt.timezone(MSK_OFFSET)
    ).strftime("%Y-%m-%d")


def _to_msk(ts):
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).astimezone(
        dt.timezone(MSK_OFFSET)
    )


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


def _reset_counters():
    today = _today_msk_str()
    if _state.get("trades_today_date") != today:
        _state["trades_today"] = 0
        _state["trades_today_date"] = today


# =====================================================================
#  STATE MANAGEMENT
# =====================================================================

def _save_state():
    with _STATE_LOCK:
        data = {
            "enabled": _state["enabled"],
            "alerts_enabled": _state["alerts_enabled"],
            "trades_today": _state.get("trades_today", 0),
            "trades_today_date": _state.get("trades_today_date", ""),
        }
        _atomic_write_json(BC_STATE_FILE, data)


def _load_state():
    global _state
    with _STATE_LOCK:
        saved = _load_json(BC_STATE_FILE, {})
        if saved:
            _state["enabled"] = saved.get("enabled", BC_ENABLED)
            _state["alerts_enabled"] = saved.get("alerts_enabled", True)
            _state["trades_today"] = saved.get("trades_today", 0)
            _state["trades_today_date"] = saved.get("trades_today_date", _today_msk_str())
        else:
            _state["trades_today_date"] = _today_msk_str()


# =====================================================================
#  TRACKED POSITIONS
# =====================================================================

def _load_tracked():
    return _load_json(BC_TRACKED_FILE, {})


def _save_tracked(d):
    _atomic_write_json(BC_TRACKED_FILE, d)


def _add_tracked(pos_id, sym, side, entry, stop, tp, opened_ts, atr):
    with _TRACKED_LOCK:
        tracked = _load_tracked()
        tracked[pos_id] = {
            "sym": sym, "side": side, "entry": entry,
            "stop": stop, "tp": tp, "opened_ts": opened_ts,
            "initial_stop": stop, "atr": atr,
            "strategy": STRATEGY_TAG, "status": "open",
        }
        _save_tracked(tracked)


def _get_open_count():
    tracked = _load_tracked()
    return sum(1 for v in tracked.values() if v.get("status") == "open")


# =====================================================================
#  GATE.IO API — получение свечей
# =====================================================================

def _fetch_candles(contract="BTC_USDT", interval="15m", limit=100):
    """Получить последние N свечей с Gate.io Spot API."""
    try:
        params = {
            "currency_pair": contract,
            "interval": interval,
            "limit": str(limit),
        }
        r = requests.get(f"{GATE_API}/candlesticks", params=params, timeout=15)
        if not r.ok:
            return []
        batch = r.json()
        if not isinstance(batch, list):
            return []
        candles = []
        for c in batch:
            if len(c) < 6:
                continue
            try:
                candles.append({
                    "ts": int(c[0]),
                    "vol": float(c[1]),
                    "close": float(c[2]),
                    "high": float(c[3]),
                    "low": float(c[4]),
                    "open": float(c[5]),
                })
            except (ValueError, TypeError):
                continue
        # Сортируем по времени (от старых к новым)
        candles.sort(key=lambda x: x["ts"])
        return candles
    except Exception as e:
        print(f"[bc] fetch_candles error: {e}")
        return []


def _fetch_sma_1h(contract="BTC_USDT", period=50):
    """Получить SMA50 1H (часовые свечи, среднее close за 50)."""
    try:
        candles = _fetch_candles(contract, interval="1h", limit=period + 5)
        if len(candles) < period:
            return None
        # Берём последние 50
        recent = candles[-period:]
        sma = statistics.mean([c["close"] for c in recent])
        return sma
    except Exception as e:
        print(f"[bc] fetch_sma error: {e}")
        return None


def _calc_atr(candles, period=4):
    """Считает ATR за period свечей 15m (по умолчанию 4 = 1 час)."""
    if len(candles) < 2:
        return 0
    trs = []
    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i-1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    if not trs:
        return 0
    return statistics.mean(trs[-period:]) if len(trs) >= period else statistics.mean(trs)


# =====================================================================
#  ЛОГИКА СТРАТЕГИИ
# =====================================================================

def _in_scan_window():
    """Сейчас окно 10:00-11:15 МСК?"""
    now_msk = _to_msk(_now_ts())
    if now_msk.hour == BC_WINDOW_START_HOUR_MSK:
        return True
    if now_msk.hour == BC_WINDOW_END_HOUR_MSK and now_msk.minute < BC_WINDOW_END_MIN_MSK:
        return True
    return False


def _check_signal():
    """Проверяет — есть ли сейчас большая свеча BTC в окне.
    Возвращает dict с сигналом или None."""
    # Получаем последние 15m свечи (нужно для расчёта среднего за сегодняшний день)
    candles_15m = _fetch_candles("BTC_USDT", interval="15m", limit=100)
    if len(candles_15m) < 5:
        return None

    # Фильтруем только сегодняшние свечи после 9:00 МСК
    today_msk = _to_msk(_now_ts())
    today_str = today_msk.strftime("%Y-%m-%d")

    today_european = []
    for c in candles_15m:
        msk = _to_msk(c["ts"])
        if msk.strftime("%Y-%m-%d") == today_str and msk.hour >= BC_EUROPEAN_START_HOUR:
            today_european.append(c)

    if len(today_european) < 3:
        return None  # мало данных для среднего

    # Ищем большую свечу в окне 10:00-11:15
    for big in today_european:
        msk = _to_msk(big["ts"])
        # Только свечи в окне сканирования
        if not (msk.hour == BC_WINDOW_START_HOUR_MSK or
                (msk.hour == BC_WINDOW_END_HOUR_MSK and msk.minute < BC_WINDOW_END_MIN_MSK)):
            continue

        # Европейские свечи ДО этой (не включая её)
        pre_european = [c for c in today_european if c["ts"] < big["ts"]]
        if len(pre_european) < 3:
            continue  # мало данных

        avg_vol = statistics.mean([c["vol"] for c in pre_european])
        if avg_vol <= 0:
            continue

        vol_ratio = big["vol"] / avg_vol
        if vol_ratio < BC_VOL_MULT:
            continue  # не большая свеча

        # Это BIG CANDLE!
        is_long = big["close"] >= big["open"]
        direction = "long" if is_long else "short"
        entry_price = big["close"]

        # Фильтр SMA50 1H
        sma_value = None
        if BC_USE_SMA50:
            sma_value = _fetch_sma_1h()
            if sma_value is None:
                _stats["signals_skipped_sma"] += 1
                return None
            if is_long and entry_price < sma_value:
                _stats["signals_skipped_sma"] += 1
                if B and _state["alerts_enabled"]:
                    B.send_telegram(
                        f"🚨 <b>BIG CANDLE BTC</b> {'🟢 ЛОНГ' if is_long else '🔴 ШОРТ'} (×{vol_ratio:.2f})\n"
                        f"⚠️ SMA50 1H фильтр: цена ${entry_price:.0f} < SMA ${sma_value:.0f} → пропуск"
                    )
                return None
            if not is_long and entry_price > sma_value:
                _stats["signals_skipped_sma"] += 1
                if B and _state["alerts_enabled"]:
                    B.send_telegram(
                        f"🚨 <b>BIG CANDLE BTC</b> {'🟢 ЛОНГ' if is_long else '🔴 ШОРТ'} (×{vol_ratio:.2f})\n"
                        f"⚠️ SMA50 1H фильтр: цена ${entry_price:.0f} > SMA ${sma_value:.0f} → пропуск"
                    )
                return None

        # Считаем ATR по 15m свечам (последние 5 = 4 TR = 1 час)
        pre_candles = candles_15m[-5:]  # 5 свечей для расчёта 4 TR
        atr_value = _calc_atr(pre_candles, period=BC_ATR_PERIOD)

        if atr_value <= 0:
            return None

        # Считаем уровни стопа и тейка
        atr_pct = (atr_value / entry_price) * 100
        stop_pct = max(BC_ATR_STOP_MIN_PCT, BC_ATR_STOP_MULT * atr_pct)
        tp1_pct = max(BC_ATR_TP_MIN_PCT, BC_ATR_TP_MULT * atr_pct)   # TP1 = 3×ATR
        tp2_pct = max(BC_ATR_TP_MIN_PCT * 1.5, BC_ATR_TP2_MULT * atr_pct)  # TP2 = 5×ATR

        # Гарантируем RR ≥ 1.5 для TP1
        if tp1_pct < stop_pct * 1.5:
            tp1_pct = stop_pct * 1.5
        # TP2 должен быть дальше TP1
        if tp2_pct <= tp1_pct:
            tp2_pct = tp1_pct * 1.5

        if is_long:
            stop_price = entry_price * (1 - stop_pct / 100)
            tp1_price = entry_price * (1 + tp1_pct / 100)
            tp2_price = entry_price * (1 + tp2_pct / 100)
        else:
            stop_price = entry_price * (1 + stop_pct / 100)
            tp1_price = entry_price * (1 - tp1_pct / 100)
            tp2_price = entry_price * (1 - tp2_pct / 100)

        signal = {
            "sym": "BTC",
            "direction": direction,
            "is_long": is_long,
            "entry_price": entry_price,
            "vol_ratio": vol_ratio,
            "atr": atr_value,
            "atr_pct": atr_pct,
            "stop_price": stop_price,
            "tp_price": tp1_price,       # обратная совместимость
            "tp1_price": tp1_price,      # TP1 = 3×ATR (50% позиции)
            "tp2_price": tp2_price,      # TP2 = 5×ATR (остаток 50%)
            "stop_pct": stop_pct,
            "tp_pct": tp1_pct,           # обратная совместимость
            "tp1_pct": tp1_pct,
            "tp2_pct": tp2_pct,
            "sma_value": sma_value,
            "signal_ts": big["ts"],
            "signal_time_msk": _to_msk(big["ts"]).strftime("%H:%M"),
        }
        return signal

    return None


def _execute_signal(signal):
    """Отправляет ордер через Upscale API."""
    if not B or not hasattr(B, "EXECUTOR"):
        return False
    executor = B.EXECUTOR
    if not executor:
        return False

    sym = signal["sym"]
    side = signal["direction"]  # "long" или "short"
    entry = signal["entry_price"]
    stop = signal["stop_price"]
    tp = signal["tp_price"]
    stop_pct = signal["stop_pct"]

    # === RISK MANAGEMENT как в 4H ===
    # risk = $100, плечо 5x
    # position_size = risk_usd / stop_pct × 100
    # margin = position_size / leverage
    # Cap маржи BC_MAX_MARGIN ($3000 = 30% от $10K депозита)
    position_size = BC_RISK_USD / stop_pct * 100
    margin = position_size / BC_LEVERAGE
    if margin > BC_MAX_MARGIN:
        margin = BC_MAX_MARGIN
        position_size = margin * BC_LEVERAGE

    sig = {
        "symbol": sym,
        "side": side,
        "price": entry,
        "stop": stop,
        "stop_pct": stop_pct,
        "tp1_price": signal.get("tp1_price", signal["tp_price"]),   # TP1 = 3×ATR
        "tp2_price": signal.get("tp2_price", signal["tp_price"] * 1.5),  # TP2 = 5×ATR
        "exit_mode": "tp_ladder",
        "risk_usd": BC_RISK_USD,
        "max_pos_usd": margin * BC_LEVERAGE,
        "max_same_side": 1,  # только 1 сделка Big Candle одновременно
        "strategy": STRATEGY_TAG,
        "leverage_override": BC_LEVERAGE,
        "ext_atr": float(signal["atr"]),
    }

    try:
        score = int(signal["vol_ratio"] * 10)
        executor.on_signal(sig, score)

        size_str = f"${margin:.0f} маржа × {BC_LEVERAGE}x"
        rr1 = signal.get("tp1_pct", signal["tp_pct"]) / signal["stop_pct"]
        rr2 = signal.get("tp2_pct", signal["tp_pct"] * 1.5) / signal["stop_pct"]
        direction_emoji = "🟢 LONG" if signal["is_long"] else "🔴 SHORT"
        tp1 = signal.get("tp1_price", signal["tp_price"])
        tp2 = signal.get("tp2_price", signal["tp_price"] * 1.5)
        if B:
            B.send_telegram(
                f"🚨 <b>BIG CANDLE BTC {direction_emoji}</b>\n"
                f"Вход: ${entry:.2f}\n"
                f"Стоп: ${stop:.2f} ({stop_pct:.3f}%)\n"
                f"TP1: ${tp1:.2f} ({signal.get('tp1_pct', signal['tp_pct']):.3f}%, RR 1:{rr1:.1f}) — 50% позиции\n"
                f"TP2: ${tp2:.2f} ({signal.get('tp2_pct', signal['tp_pct']*1.5):.3f}%, RR 1:{rr2:.1f}) — остаток 50%\n"
                f"📈 ATR: ${signal['atr']:.2f}\n"
                f"Маржа: {size_str} (риск ${BC_RISK_USD})\n"
                f"Vol×{signal['vol_ratio']:.2f} | SMA50 1H: ${signal['sma_value']:.0f}"
            )

        _state["trades_today"] += 1
        _stats["trades_opened"] += 1
        _save_state()
        # Записываем в tracked (pending — пока позиция не появилась в API)
        _add_tracked(f"pending_{sym}_{int(_now_ts())}", sym, side, entry, stop, tp, _now_ts(), signal["atr"])
        return True
    except Exception as e:
        if B:
            B.send_telegram(f"⚠️ BC {sym}: ошибка входа — {e}")
        return False


def _log_signal(signal, skipped=False, skip_reason=""):
    try:
        import csv
        row = {
            "ts": int(_now_ts()),
            "sym": signal["sym"],
            "direction": signal["direction"],
            "entry": round(signal["entry_price"], 2),
            "vol_ratio": round(signal["vol_ratio"], 2),
            "atr": round(signal["atr"], 2),
            "stop_pct": round(signal["stop_pct"], 4),
            "tp_pct": round(signal["tp_pct"], 4),
            "sma_1h": round(signal["sma_value"], 2) if signal.get("sma_value") else "",
            "skipped": "YES" if skipped else "NO",
            "skip_reason": skip_reason,
        }
        header = ",".join(row.keys())
        if os.path.exists(BC_SIGNALS_CSV):
            with open(BC_SIGNALS_CSV, encoding="utf-8") as f:
                first = f.readline().strip()
            if first != header:
                os.replace(BC_SIGNALS_CSV, f"{BC_SIGNALS_CSV}.old-{int(time.time())}")
        new = not os.path.exists(BC_SIGNALS_CSV)
        with open(BC_SIGNALS_CSV, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(row.keys()))
            if new:
                w.writeheader()
            w.writerow(row)
    except Exception as e:
        print(f"[bc] CSV signal error: {e}")


def _log_trade(sym, side, entry, exit_price, pnl, reason, hold_sec):
    try:
        import csv
        row = {
            "ts": int(_now_ts()),
            "sym": sym,
            "side": side,
            "entry": round(entry, 2),
            "exit": round(exit_price, 2),
            "pnl": round(pnl, 2),
            "reason": reason,
            "hold_sec": hold_sec,
        }
        header = ",".join(row.keys())
        if os.path.exists(BC_TRADES_CSV):
            with open(BC_TRADES_CSV, encoding="utf-8") as f:
                first = f.readline().strip()
            if first != header:
                os.replace(BC_TRADES_CSV, f"{BC_TRADES_CSV}.old-{int(time.time())}")
        new = not os.path.exists(BC_TRADES_CSV)
        with open(BC_TRADES_CSV, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(row.keys()))
            if new:
                w.writeheader()
            w.writerow(row)
    except Exception as e:
        print(f"[bc] CSV trade error: {e}")


# =====================================================================
#  POSITION MANAGEMENT
# =====================================================================

def _manage_positions():
    """Управляет открытыми Big Candle позициями."""
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
        print(f"[bc] manage_positions error: {e}")
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
                lp_mid = UE._pos_market(lp)
                lp_sym = None
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
                    print(f"[bc] {sym_match}: pending → {real_id}")
                    found_real = True
                    break
            if not found_real:
                if now - rec.get("opened_ts", now) > 30:
                    print(f"[bc] {sym_match}: pending истёк — удаляем")
                    if _state.get("trades_today", 0) > 0:
                        _state["trades_today"] -= 1
                    if _stats.get("trades_opened", 0) > 0:
                        _stats["trades_opened"] -= 1
                    _save_state()
                    del tracked[pos_id]
                    changed = True
                    if B:
                        B.send_telegram(
                            f"⚠️ BC {sym_match}: позиция не открылась (отклонена Upscale)\n"
                            f"Счётчик trades_today откатан"
                        )
            continue

        # Позиция закрылась (TP/стоп/time-stop)
        if pos_id not in live_ids:
            entry = rec.get("entry", 0)
            exit_price = rec.get("tp", entry)
            hold_sec = int(now - rec.get("opened_ts", now))
            # Получаем PnL через API истории ордеров
            pnl = _fetch_realized_pnl(executor, rec["sym"], rec.get("opened_ts", now))

            _log_trade(rec["sym"], rec["side"], entry, exit_price, pnl,
                       "TP/SL/TIMEOUT", hold_sec)

            _stats["trades_closed"] += 1
            _stats["total_pnl"] += pnl
            if pnl > 0:
                _stats["wins"] += 1
            else:
                _stats["losses"] += 1

            del tracked[pos_id]
            changed = True
        else:
            # Safety net: T+BC_MAX_HOLD_SEC (6 часов)
            hold_sec = now - rec.get("opened_ts", now)
            if hold_sec >= BC_MAX_HOLD_SEC:
                pos = next((p for p in live_positions if UE._pos_id(p) == pos_id), None)
                if pos:
                    mid = UE._pos_market(pos)
                    if mid:
                        info = {"id": pos_id, "mid": mid, "dir": rec.get("side", "long")}
                        try:
                            res = executor.close_position(info, reason=f"BC_TIMEOUT_{BC_MAX_HOLD_SEC}s")
                            if B:
                                B.send_telegram(
                                    f"⏱️ BC {rec['sym']}: safety-net выход T+{int(hold_sec)}с ({res})"
                                )
                        except Exception as e:
                            print(f"[bc] timeout close error: {e}")

    if changed:
        with _TRACKED_LOCK:
            _save_tracked(tracked)


def _fetch_realized_pnl(executor, sym, since_ts):
    """Получает PnL реализованный по символу с since_ts."""
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
#  MAIN LOOP
# =====================================================================

_scanner_thread = None
_manager_thread = None


def _scanner_loop():
    """Главный цикл: проверяет окно 10:00-11:15 МСК и ищет большие свечи."""
    print(f"[bc] Scanner запущен: окно {BC_WINDOW_START_HOUR_MSK}:00-{BC_WINDOW_END_HOUR_MSK}:{BC_WINDOW_END_MIN_MSK:02d} МСК")
    while True:
        try:
            _reset_counters()

            # Проверяем окно
            if not _in_scan_window():
                time.sleep(60)
                continue

            # Лимит сделок в день
            if _state.get("trades_today", 0) >= BC_MAX_TRADES_DAY:
                time.sleep(60)
                continue

            # Не отправляем сигнал по одной свече дважды
            # Минимальный интервал — 15 минут (длина свечи)
            if _now_ts() - _state.get("last_signal_ts", 0) < 900:
                time.sleep(60)
                continue

            # Уже есть открытая Big Candle позиция?
            if _get_open_count() > 0:
                time.sleep(60)
                continue

            # Проверяем сигнал
            signal = _check_signal()
            if signal:
                _state["last_signal_ts"] = _now_ts()
                _stats["signals_total"] += 1
                _log_signal(signal)

                # Алерт
                if B and _state["alerts_enabled"]:
                    direction_emoji = "🟢 ЛОНГ" if signal["is_long"] else "🔴 ШОРТ"
                    B.send_telegram(
                        f"🚨 <b>BIG CANDLE BTC {direction_emoji}</b>\n"
                        f"Цена входа: ${signal['entry_price']:.2f}\n"
                        f"Vol×{signal['vol_ratio']:.2f} (порог ×{BC_VOL_MULT})\n"
                        f"ATR: ${signal['atr']:.2f} ({signal['atr_pct']:.3f}%)\n"
                        f"Stop: ${signal['stop_price']:.2f} ({signal['stop_pct']:.3f}%)\n"
                        f"TP: ${signal['tp_price']:.2f} ({signal['tp_pct']:.3f}%, RR 1:{signal['tp_pct']/signal['stop_pct']:.1f})"
                        + (f"\nSMA50 1H: ${signal['sma_value']:.0f}" if signal.get("sma_value") else "")
                    )

                # Авто-торговля
                if _state["enabled"]:
                    _execute_signal(signal)
                else:
                    if B:
                        B.send_telegram("ℹ️ Авто-торговля выключена (/bc_on чтобы включить)")

            time.sleep(BC_CHECK_INTERVAL)
        except Exception as e:
            print(f"[bc] scanner loop error: {e}")
            time.sleep(60)


def _manager_loop():
    """Цикл управления позициями: проверяет каждые 30с."""
    print(f"[bc] Manager запущен: интервал 30с")
    while True:
        try:
            _manage_positions()
        except Exception as e:
            print(f"[bc] manager error: {e}")
        time.sleep(30)


# =====================================================================
#  TELEGRAM COMMANDS
# =====================================================================

def status_report():
    lines = ["", "━━━ Big Candle Strategy ━━━"]
    enabled = _state.get("enabled", False)
    alerts = _state.get("alerts_enabled", True)
    lines.append(f"Авто-торговля: {'✅ ВКЛ' if enabled else '🔴 ВЫКЛ'}")
    lines.append(f"Алерты: {'✅ ВКЛ' if alerts else '🔴 ВЫКЛ'}")
    lines.append(f"Окно: {BC_WINDOW_START_HOUR_MSK}:00-{BC_WINDOW_END_HOUR_MSK}:{BC_WINDOW_END_MIN_MSK:02d} МСК")
    lines.append(f"Порог: ×{BC_VOL_MULT} от среднего (после {BC_EUROPEAN_START_HOUR}:00 МСК)")
    lines.append(f"Фильтр SMA50 1H: {'✅' if BC_USE_SMA50 else '🔴'}")
    lines.append(f"Уровни: стоп {BC_ATR_STOP_MULT}×ATR (мин {BC_ATR_STOP_MIN_PCT}%), TP {BC_ATR_TP_MULT}×ATR (мин {BC_ATR_TP_MIN_PCT}%)")
    lines.append(f"Риск: ${BC_RISK_USD} | Плечо: {BC_LEVERAGE}x | Cap маржи: ${BC_MAX_MARGIN:.0f} | Лимит: {BC_MAX_TRADES_DAY}/день")
    lines.append(f"Сделок сегодня: {_state.get('trades_today', 0)}/{BC_MAX_TRADES_DAY}")

    tracked = _load_tracked()
    open_count = sum(1 for v in tracked.values() if v.get("status") == "open")
    lines.append(f"Открыто позиций: {open_count}")

    lines.append(f"Всего сигналов: {_stats['signals_total']} (пропущено по SMA: {_stats['signals_skipped_sma']})")
    lines.append(f"Сделок: {_stats['trades_opened']} открыто / {_stats['trades_closed']} закрыто")
    if _stats['trades_closed'] > 0:
        wr = _stats['wins'] / _stats['trades_closed'] * 100
        lines.append(f"Winrate: {wr:.0f}% ({_stats['wins']}/{_stats['trades_closed']})")
        lines.append(f"PnL: ${_stats['total_pnl']:+.2f}")
    return "\n".join(lines)


def cmd_on():
    _state["enabled"] = True
    _save_state()
    return "✅ Big Candle авто-торговля включена."


def cmd_off():
    _state["enabled"] = False
    _save_state()
    return "🔴 Big Candle авто-торговля выключена. Алерты продолжают работать."


def cmd_alerts_toggle():
    _state["alerts_enabled"] = not _state["alerts_enabled"]
    _save_state()
    state_str = "ВКЛ" if _state["alerts_enabled"] else "ВЫКЛ"
    return f"🔔 Алерты {state_str}"


def cmd_close():
    """Принудительно закрыть открытую Big Candle позицию."""
    if not B or not hasattr(B, "EXECUTOR"):
        return "⚠️ Executor недоступен"
    executor = B.EXECUTOR
    if not executor or not executor.client:
        return "⚠️ Нет клиента Upscale"

    tracked = _load_tracked()
    open_recs = [(pid, v) for pid, v in tracked.items() if v.get("status") == "open" and not pid.startswith("pending_")]
    if not open_recs:
        return "📭 Нет открытых Big Candle позиций"

    closed = 0
    for pos_id, rec in open_recs:
        try:
            executor._ensure_account()
            live_positions = UE._as_list(executor.client.positions(executor.account_id))
            pos = next((p for p in live_positions if UE._pos_id(p) == pos_id), None)
            if pos:
                mid = UE._pos_market(pos)
                if mid:
                    info = {"id": pos_id, "mid": mid, "dir": rec.get("side", "long")}
                    res = executor.close_position(info, reason="BC_MANUAL_CLOSE")
                    # Помечаем как закрытую в tracked
                    with _TRACKED_LOCK:
                        tracked = _load_tracked()
                        if pos_id in tracked:
                            tracked[pos_id]["status"] = "closed"
                            _save_tracked(tracked)
                    closed += 1
                    if B:
                        B.send_telegram(f"🛑 BC {rec['sym']}: принудительное закрытие ({res})")
        except Exception as e:
            print(f"[bc] close error: {e}")
    return f"🛑 Закрыто позиций: {closed}"


def cmd_stats():
    lines = ["", "━━━ Big Candle Statistics ━━━"]
    lines.append(f"Сигналов всего: {_stats['signals_total']}")
    lines.append(f"Пропущено по SMA50: {_stats['signals_skipped_sma']}")
    lines.append(f"Сделок открыто: {_stats['trades_opened']}")
    lines.append(f"Сделок закрыто: {_stats['trades_closed']}")
    if _stats['trades_closed'] > 0:
        wr = _stats['wins'] / _stats['trades_closed'] * 100
        lines.append(f"Winrate: {wr:.1f}%")
        lines.append(f"Wins: {_stats['wins']} | Losses: {_stats['losses']}")
        lines.append(f"PnL: ${_stats['total_pnl']:+.2f}")
    return "\n".join(lines)


def cmd_log():
    if not B:
        return "⚠️ bot.py недоступен"
    sent = 0
    for path, caption in [(BC_SIGNALS_CSV, "BC signals"), (BC_TRADES_CSV, "BC trades")]:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            try:
                B.send_document(path, caption)
                sent += 1
            except Exception as e:
                print(f"[bc] send doc error: {e}")
    if sent == 0:
        return "📒 Журналы пока пустые."
    return f"📒 Отправлено файлов: {sent}"


# =====================================================================
#  ИНИЦИАЛИЗАЦИЯ И ЗАПУСК
# =====================================================================

def start():
    """Запуск стратегии в отдельном потоке."""
    global _scanner_thread, _manager_thread

    _load_state()

    # Запускаем scanner (ищет сигналы)
    _scanner_thread = threading.Thread(target=_scanner_loop, daemon=True, name="bc-scanner")
    _scanner_thread.start()

    # Запускаем manager (управляет позициями)
    _manager_thread = threading.Thread(target=_manager_loop, daemon=True, name="bc-manager")
    _manager_thread.start()

    print(f"[bc] {STRATEGY_NAME} старт: окно {BC_WINDOW_START_HOUR_MSK}:00-{BC_WINDOW_END_HOUR_MSK}:{BC_WINDOW_END_MIN_MSK:02d} МСК")
    if B:
        sma_line = f"✅ SMA50 1H фильтр" if BC_USE_SMA50 else "🔴 SMA50 фильтр ВЫКЛ"
        B.send_telegram(
            f"🚨 <b>{STRATEGY_NAME} запущен</b>\n"
            f"Пар: BTC only\n"
            f"Маржа: ${BC_RISK_USD} риск × {BC_LEVERAGE}x плечо (cap ${BC_MAX_MARGIN:.0f})\n"
            f"Окно: {BC_WINDOW_START_HOUR_MSK}:00-{BC_WINDOW_END_HOUR_MSK}:{BC_WINDOW_END_MIN_MSK:02d} МСК\n"
            f"Порог: ×{BC_VOL_MULT} (среднее после {BC_EUROPEAN_START_HOUR}:00 МСК)\n"
            f"{sma_line}\n"
            f"ATR_15m: {BC_ATR_PERIOD} свечи (1 час)\n"
            f"Стоп: {BC_ATR_STOP_MULT}×ATR (мин {BC_ATR_STOP_MIN_PCT}%) | TP: {BC_ATR_TP_MULT}×ATR (мин {BC_ATR_TP_MIN_PCT}%)\n"
            f"Max hold: {BC_MAX_HOLD_SEC//3600}ч safety-net | Лимит: {BC_MAX_TRADES_DAY}/день\n"
            f"Авто-торговля: {'✅ ВКЛ' if _state['enabled'] else '🔴 ВЫКЛ'}\n"
            f"Команды: /bc /bc_on /bc_off /bc_close /bc_stats /bc_log"
        )


if __name__ == "__main__":
    print(f"{STRATEGY_NAME} — standalone test")
    start()
    while True:
        time.sleep(60)
