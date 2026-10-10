# -*- coding: utf-8 -*-
"""
exec_donchian_4h_v22.py — живое исполнение стратегии Donchian 4H v2.2-FINAL
=============================================================================
Логика ИДЕНТИЧНА бэктесту bt_donchian_4h_v22_final.py:

Сигнал на вход (5 условий):
  - close > Donchian(20)_high (long) ИЛИ close < Donchian(20)_low (short)
  - BTC regime = ±1 (BTC выше/ниже SMA50 на 1D)
  - +DI > -DI (long) ИЛИ -DI > +DI (short)
  - ATR% в диапазоне 0.6%-2.0%
  - |funding| ≤ 0.05%

Риск-менеджмент:
  - Compound sizing: max($100, min($250, equity × 0.8%))
  - DD brake ×0.4 при DD > $900 (восстановление при 85% от пика)
  - Max 10 одновременных (6 в сторону)
  - Max 10 новых в день
  - MAX_LOSERS_PER_SIDE=3: если 3+ убыточные открытые в сторону — новых не открываем
  - Cooldown 14 дней после 4 убытков подряд по паре

Управление позицией:
  - Trailing-стоп 4.5×ATR по CLOSE свечи (НЕ по high/low!)
  - Partial TP +8% favourable → закрыть 50% market-ордером, остаток в breakeven
  - Выход по сигналу: close < текущий Donchian-low (long) или > high (short)
  - Выход по времени: 35 свечей 4H (~5.8 дня)

Два независимых контура защиты:
  1. Стратегийный Daily Stop today-only -$350:
     - Срабатывает, если СУММА P&L позиций, открытых В ТЕКУЩИЙ календарный день
       (реализованные убытки + нереализованные убытки), пробивает -$350
     - Закрывает только сегодня-открытые убыточные (через close_position())
     - Старые позиции НЕ трогает (могут восстановиться)
     - Блокирует новые входы до 00:00 UTC

  2. Upscale аварийка (в upscale_exec.py watchdog, отдельная):
     - DAY_HARD_FRAC=0.8 → $400 при $10k start (close-all + halt до 00:00 UTC)
     - TOT_HARD_FRAC=0.8 → $800 при $10k start (close-all + halt + ручной /resume)

Запуск: из bot.py через DR42.check_signals(), который вызывается по расписанию
каждые 4 часа (после закрытия 4H-свечи + 5 мин буфер).
"""

import os
import sys
import json
import math
import time
import threading
import datetime as dt
from decimal import Decimal
from typing import Optional

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
#  КОНСТАНТЫ — синхронизированы с bt_donchian_4h_v22_final.py v2.2-FINAL
# =====================================================================

# --- Индикаторы (4H-свечи) ---------------------------------------------------
CANDLE_INTERVAL  = "4h"           # 4H, не 1D — главное отличие от v4.4
DONCHIAN_PERIOD  = 20
BTC_REGIME_SMA   = 50              # на 1D свечах
DMI_PERIOD       = 14
ATR_PERIOD       = 14
ATR_PCT_MIN      = 0.006           # 0.6% — фильтр низкого ATR
ATR_PCT_MAX      = 0.020           # 2.0% — фильтр высокого ATR
ATR_STOP_MULT    = 4.5              # множитель ATR для initial/trailing стопа
ADX_THRESHOLD    = 20.0            # для статистики (не используется в фильтре)

# --- Выход -------------------------------------------------------------------
MAX_HOLD_DAYS = 35                  # 35 свечей 4H ≈ 5.8 дня
PARTIAL_TP_PCT      = 0.08          # +8% favourable → частичная фиксация
PARTIAL_TP_FRACTION = 0.50          # фиксируем 50% позиции, остаток → breakeven
STOP_REVERSAL_LOOKFORWARD_DAYS = 15  # только для анализа в отчёте

# --- Compound sizing & DD brake ---------------------------------------------
RISK_FRACTION     = 0.008           # 0.8% от equity
SLOT_RISK_MIN     = 100.0           # $100 floor (стартовый, equity < $12500)
SLOT_RISK_MAX     = 250.0           # $250 cap (equity > $31250)
MAX_POSITION_PCT  = 0.20            # не более 20% equity на одну позицию
DD_BRAKE_THRESHOLD = 900.0          # при DD > $900 — риск × 0.4
DD_BRAKE_FACTOR     = 0.4
DD_BRAKE_RECOVERY   = 0.85          # восстановление: equity >= 85% от пика

# --- Лимиты позиций ---------------------------------------------------------
MAX_CONCURRENT      = 10            # максимум одновременных позиций
MAX_PER_SIDE_CAP     = 6            # максимум в одну сторону (long ИЛИ short)
PER_SIDE_BUDGET      = 2000         # бюджет на сторону (для динамического лимита)
MAX_NEW_PER_DAY      = 10           # максимум новых входов в сутки UTC
MAX_LOSERS_PER_SIDE  = 3            # если 3+ убыточные в сторону — новых не открываем

# --- Daily Stop today-only --------------------------------------------------
DAILY_STOP_LOSS       = -350.0      # порог для обычного дня
DAILY_STOP_LOSS_CONSEC = -350.0     # порог, если вчера закрылись в минус

# --- Фильтры пар -------------------------------------------------------------
# 33 пары в EXCLUDE по результатам PAIRS ANALYSIS (бэктест по годам 2024/2025/2026):
# - 11 "старых" (подтвердились как убыточные/нестабильные)
# - 18 STABLE_LOSS (минус во все 3 года)
# - 4 BROKEN_RECENTLY (сломались в 2026)
# В торговле остаётся 70 пар из 103.
# TRX, RUNE, KAIA возвращены в торговлю (STABLE_PROFIT/MIXED с итоговым плюсом).
EXCLUDE_PAIRS = {
    # ── СТАРЫЕ (подтвердились, 11 пар) ──────────────────────────────────────
    "BNB", "UNI", "LTC", "PENDLE", "HBAR", "STX", "IOTA", "ARB", "GRT", "CRV", "XLM",
    # ── STABLE_LOSS — убыток во все 3 года (18 пар) ──────────────────────────
    "SAND", "LINEA", "SKY", "ETC", "HYPE", "AVAX", "S", "PEPE", "WIF", "OP",
    "INJ", "EIGEN", "BONK", "ENS", "ORDI", "PUMP", "DYDX", "DATA",
    # ── BROKEN_RECENTLY — сломались в 2026 (4 пары) ──────────────────────────
    "CAKE", "XRP", "LDO", "POL",
}

# --- Cooldown ----------------------------------------------------------------
CONSEC_LOSS_LIMIT = 4               # после 4 убытков подряд по паре → кулдаун
COOLDOWN_DAYS     = 14              # 14 дней

# --- Комиссии (для расчётов в live — для отчётов берём реальные из Upscale) -
COMM_TAKER  = 0.00005              # 0.008% Upscale + ~0.04% запас (для совместимости с бэктестом)
SLIPPAGE    = 0.0002               # 0.02% проскальзывание (для совместимости)
FUNDING_TIMES_UTC = (0, 8, 16)    # часы фандинга на perpetual

# --- Доп. --------------------------------------------------------------------
BTC_CONTRACT = "BTC_USDT"

# --- Баланс: как часто обновляем из Upscale API ----------------------------
BALANCE_TTL_SEC   = 300             # 5 минут кеш
BALANCE_FORCE_ON_SCAN = True        # перед каждым check_signals() — принудительное обновление

# --- Файлы персистентного состояния (на диске Render — стираются при деплое)
# --- Но это ОК: tracked_positions восстанавливается при первом скане из
# --- открытых на Upscale позиций; cooldown теряется (сознательный компромисс)-
COOLDOWN_FILE     = "/tmp/cooldown_donchian_4h_v22.json"
TRADE_STATE_FILE  = "/tmp/trade_state_donchian_4h_v22.json"   # consec_losses по парам
OPEN_POS_FILE     = "/tmp/open_positions_donchian_4h_v22.json" # tracked {sym: {opened_ts, side, entry, atr, trail_stop, max_fav, hold_days, partial_taken}}
PEAK_FILE         = "/tmp/peak_equity_donchian_4h_v22.json"   # пик equity (для DD brake)
TODAY_REALIZED_FILE = "/tmp/today_realized_donchian_4h_v22.json"  # {date, realized} — Daily Stop today-only
DAY_STOP_FILE     = "/tmp/day_stop_donchian_4h_v22.json"      # {date, active} — блокировка входов до 00:00 UTC
PREV_DAY_FILE     = "/tmp/prev_day_donchian_4h_v22.json"      # {date, day_start_equity} — для CONSEC
NEW_TODAY_FILE    = "/tmp/new_today_donchian_4h_v22.json"     # {date, count} — счётчик новых входов за сутки (MAX_NEW_PER_DAY)


# =====================================================================
#  ГЛОБАЛЬНЫЕ БЛОКИРОВКИ — для защиты state-файлов от race conditions
#  (параллельный /scan + расписание 4H + balance_updater могут одновременно
#  читать/писать tracked_positions и today_realized)
# =====================================================================

_TRACKED_LOCK        = threading.Lock()   # tracked_positions (load-modify-write)
_TODAY_REALIZED_LOCK = threading.Lock()   # today_realized (load-modify-write)
_NEW_TODAY_LOCK      = threading.Lock()   # new_today (load-modify-write)
_CHECK_LOCK          = threading.Lock()    # вся check_signals целиком (защита от параллельных /scan + 4H)


def _atomic_write_json(path: str, data) -> None:
    """Атомарная запись JSON во временный файл + os.replace() —
    не оставляет частично записанный файл при SIGKILL/OOM."""
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _load_json(path: str, default):
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return default


# =====================================================================
#  КЕШ БАЛАНСА
# =====================================================================

_balance_cache = {
    "equity":     None,   # Decimal
    "peak":       None,
    "day_start":  None,
    "base":       None,
    "day_lim":    None,
    "tot_lim":    None,
    "dd_pct":     None,
    "td_pct":     None,
    "updated_at": 0.0,
    "error":      None,
    "lock":       threading.Lock(),
}


def _load_peak() -> float:
    try:
        if os.path.exists(PEAK_FILE):
            with open(PEAK_FILE) as f:
                return float(json.load(f).get("peak", 0))
    except Exception:
        pass
    return 0.0


def _save_peak(peak: float):
    try:
        _atomic_write_json(PEAK_FILE, {"peak": peak})
    except Exception as e:
        print(f"[exec_dr22] peak save error: {e}")


def refresh_balance(executor=None, force=False) -> bool:
    """Обновляет кеш баланса из Upscale API. force=True — обновить даже если кеш свежий."""
    with _balance_cache["lock"]:
        age = time.time() - _balance_cache["updated_at"]
        if not force and age < BALANCE_TTL_SEC and _balance_cache["equity"] is not None:
            return True

    if executor is None and B is not None:
        executor = getattr(B, "EXECUTOR", None)
    if executor is None:
        with _balance_cache["lock"]:
            _balance_cache["error"] = "EXECUTOR не найден"
        return False

    try:
        executor._ensure_account()
        if not executor.client or not executor.account_id:
            with _balance_cache["lock"]:
                _balance_cache["error"] = "нет ключа Upscale или account_id не определён"
            return False
        snap = executor._snapshot()
        if snap is None:
            with _balance_cache["lock"]:
                _balance_cache["error"] = executor._snap_err or "snapshot вернул None"
            return False

        equity = float(snap["equity"])

        # Пик (не сбрасывается при рестарте — загружаем из файла)
        stored_peak = _load_peak()
        peak = max(equity, stored_peak)
        _save_peak(peak)

        with _balance_cache["lock"]:
            _balance_cache["equity"]     = equity
            _balance_cache["peak"]       = Decimal(str(peak))
            _balance_cache["day_start"]  = snap["day_start"]
            _balance_cache["base"]       = snap["base"]
            _balance_cache["day_lim"]    = snap["day_lim"]
            _balance_cache["tot_lim"]    = snap["tot_lim"]
            _balance_cache["dd_pct"]     = snap["dd_pct"]
            _balance_cache["td_pct"]     = snap["td_pct"]
            _balance_cache["updated_at"] = time.time()
            _balance_cache["error"]      = None
        return True
    except Exception as e:
        with _balance_cache["lock"]:
            _balance_cache["error"] = str(e)[:200]
        print(f"[exec_dr22] refresh_balance error: {e}")
        return False


def get_equity() -> Optional[float]:
    with _balance_cache["lock"]:
        v = _balance_cache["equity"]
    return float(v) if v is not None else None


def get_day_start() -> Optional[float]:
    with _balance_cache["lock"]:
        v = _balance_cache["day_start"]
    return float(v) if v is not None else None


def is_dd_brake_active() -> bool:
    """DD brake активен, если (peak - equity) > DD_BRAKE_THRESHOLD.
    Восстановление: equity >= peak × DD_BRAKE_RECOVERY (85% от пика)."""
    with _balance_cache["lock"]:
        equity = _balance_cache["equity"]
        peak   = _balance_cache["peak"]
    if equity is None or peak is None:
        return False
    dd = float(peak) - float(equity)
    if dd <= DD_BRAKE_THRESHOLD:
        return False
    # Проверяем восстановление: если equity вернулось к 85% от пика → brake off
    if float(equity) >= float(peak) * DD_BRAKE_RECOVERY:
        return False
    return True


def get_day_loss() -> Optional[float]:
    """Реализованный + нереализованный убыток СЕГОДНЯ (UTC) по данным Upscale.
    None, если баланс ещё не прочитан. Используется для Daily Stop today-only
    как верхнеуровневая проверка (но сам триггер считает только
    сегодня-открытые позиции, см. _compute_today_pnl())."""
    with _balance_cache["lock"]:
        eq = _balance_cache["equity"]
        ds = _balance_cache["day_start"]
    if eq is None or ds is None:
        return None
    return max(0.0, float(ds) - float(eq))


def compute_risk_slot(equity: float, dd_brake: bool = False) -> float:
    """Compound sizing: max($100, min($250, equity × 0.8%)).
    DD-brake: ×0.4 при DD > $900."""
    base = max(SLOT_RISK_MIN, min(SLOT_RISK_MAX, equity * RISK_FRACTION))
    if dd_brake:
        base *= DD_BRAKE_FACTOR
    return base


def compute_max_per_side(current_risk: float) -> int:
    """Сколько позиций в одну сторону при текущем risk-слоте.
    = min(MAX_PER_SIDE_CAP, int(PER_SIDE_BUDGET / current_risk)).
    При $100 → min(6, 2000/100=20) → 6.
    При $250 → min(6, 2000/250=8) → 6.
    """
    if current_risk <= 0:
        return 0
    return min(MAX_PER_SIDE_CAP, int(PER_SIDE_BUDGET / current_risk))


def balance_summary() -> str:
    """Форматированная строка текущего баланса для отчётов."""
    with _balance_cache["lock"]:
        eq      = _balance_cache["equity"]
        peak    = _balance_cache["peak"]
        ds      = _balance_cache["day_start"]
        base    = _balance_cache["base"]
        day_lim = _balance_cache["day_lim"]
        tot_lim = _balance_cache["tot_lim"]
        dd_pct  = _balance_cache["dd_pct"]
        td_pct  = _balance_cache["td_pct"]
        err     = _balance_cache["error"]
        upd     = _balance_cache["updated_at"]

    if eq is None:
        return f"⚠️ Баланс Upscale недоступен: {err or 'нет данных'}"

    age = int(time.time() - upd)
    dd_brake = is_dd_brake_active()
    risk_slot = compute_risk_slot(float(eq), dd_brake)
    day_loss = max(0, float(ds) - float(eq)) if ds else 0
    tot_loss = max(0, float(base) - float(eq)) if base else 0
    day_stop_hit = day_loss >= abs(DAILY_STOP_LOSS)

    lines = [
        f"💰 <b>Upscale equity: ${float(eq):,.2f}</b> (данные {age}с назад)",
        f"Начало дня: ${float(ds):,.2f} | Убыток дня: ${day_loss:.2f} / лимит ${float(day_lim):,.0f} ({dd_pct}%)"
        + f" | дневной стоп today-only ${abs(DAILY_STOP_LOSS):.0f}" + (" 🔴 АКТИВЕН (новых входов нет)" if day_stop_hit else ""),
        f"Старт периода: ${float(base):,.2f} | Общ. просадка: ${tot_loss:.2f} / лимит ${float(tot_lim):,.0f} ({td_pct}%)",
        f"Пик эквити: ${float(peak):,.2f} | DD от пика: ${float(peak)-float(eq):.2f}",
        f"Risk/сделку: <b>${risk_slot:.1f}</b>" + (" 🔴 DD-brake ×0.4" if dd_brake else " ✅"),
    ]
    return "\n".join(lines)


# =====================================================================
#  COOLDOWN — сохранение на диск
# =====================================================================

def _load_cooldowns() -> dict:
    raw = _load_json(COOLDOWN_FILE, {})
    now = time.time()
    return {k: v for k, v in raw.items() if v > now}


def _save_cooldowns(cooldowns: dict):
    now = time.time()
    _atomic_write_json(COOLDOWN_FILE, {k: v for k, v in cooldowns.items() if v > now})


# =====================================================================
#  TODAY REALIZED — персистентный (Daily Stop today-only)
# =====================================================================

def _today_utc_str() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def _load_today_realized() -> dict:
    """{date, realized}. Если дата не сегодня — возвращаем пустой словарь."""
    data = _load_json(TODAY_REALIZED_FILE, {})
    if data.get("date") != _today_utc_str():
        return {"date": _today_utc_str(), "realized": 0.0}
    return data


def _save_today_realized(realized: float):
    _atomic_write_json(TODAY_REALIZED_FILE, {"date": _today_utc_str(), "realized": realized})


def add_today_realized(pnl: float):
    """Добавляет PnL закрытой сегодня-открытой позиции в today_realized.
    Используется для триггера Daily Stop today-only.
    Защищено блокировкой от race conditions (параллельный /scan + 4H)."""
    with _TODAY_REALIZED_LOCK:
        data = _load_json(TODAY_REALIZED_FILE, {})
        today = _today_utc_str()
        if data.get("date") != today:
            data = {"date": today, "realized": 0.0}
        data["realized"] = float(data.get("realized", 0.0)) + pnl
        _atomic_write_json(TODAY_REALIZED_FILE, data)


def get_today_realized() -> float:
    """Сумма реализованного PnL закрытых сегодня-открытых позиций.
    Если файл утерян (рестарт Render) — НЕ восстанавливаем (сознательный компромисс:
    лучше пропустить один раз триггер, чем посчитать чужие позиции)."""
    with _TODAY_REALIZED_LOCK:
        data = _load_json(TODAY_REALIZED_FILE, {})
    if data.get("date") != _today_utc_str():
        return 0.0
    return float(data.get("realized", 0.0))


# =====================================================================
#  NEW TODAY — счётчик новых входов за сутки (MAX_NEW_PER_DAY)
# =====================================================================

def _get_new_today_count() -> int:
    """Сколько новых позиций уже открыто за текущие UTC-сутки.
    Сбрасывается в 00:00 UTC автоматически (через проверку даты)."""
    with _NEW_TODAY_LOCK:
        data = _load_json(NEW_TODAY_FILE, {})
    if data.get("date") != _today_utc_str():
        return 0
    return int(data.get("count", 0))


def _inc_new_today():
    """Инкремент счётчика новых входов за сутки."""
    with _NEW_TODAY_LOCK:
        data = _load_json(NEW_TODAY_FILE, {})
        today = _today_utc_str()
        if data.get("date") != today:
            data = {"date": today, "count": 0}
        data["count"] = int(data.get("count", 0)) + 1
        _atomic_write_json(NEW_TODAY_FILE, data)


# =====================================================================
#  DAY STOP ACTIVE — блокировка новых входов до 00:00 UTC
# =====================================================================

def _load_day_stop_active() -> bool:
    data = _load_json(DAY_STOP_FILE, {})
    return data.get("date") == _today_utc_str() and data.get("active", False)


def _save_day_stop_active(active: bool):
    _atomic_write_json(DAY_STOP_FILE, {"date": _today_utc_str(), "active": active})


# =====================================================================
#  PREV DAY PnL — для выбора DAILY_STOP_LOSS_CONSEC
# =====================================================================

def _load_prev_day_state() -> dict:
    """Возвращает {date, prev_day_pnl}.
    Если дата файла — вчера, считаем prev_day_pnl.
    Если дата файла — сегодня, значит мы уже обновляли состояние сегодня — возвращаем сохранённое."""
    return _load_json(PREV_DAY_FILE, {})


def _save_prev_day_state(day_start_equity: float):
    """При смене UTC-дня сохраняет day_start_equity вчерашнего дня как prev_day_start.
    Тогда при следующем скане: prev_day_pnl = today_day_start - prev_day_start."""
    _atomic_write_json(PREV_DAY_FILE, {
        "date": _today_utc_str(),
        "prev_day_start": day_start_equity,
    })


def get_prev_day_pnl() -> float:
    """Возвращает PnL предыдущего дня (positive = прибыль, negative = убыток).
    0.0 если нет данных."""
    data = _load_prev_day_state()
    prev_day_start = data.get("prev_day_start")
    today_day_start = get_day_start()
    if prev_day_start is None or today_day_start is None:
        return 0.0
    return float(today_day_start) - float(prev_day_start)


def _maybe_roll_prev_day():
    """При смене UTC-дня: сохраняем day_start_equity СЕГОДНЯШНЕГО дня как
    prev_day_start для ЗАВТРАШНЕГО дня.
    Алгоритм (C6 fix):
      - Читаем текущий день из файла. Если файл от вчера (или пустой) → roll.
      - При roll: сохраняем ТЕКУЩИЙ day_start_equity (это старт вчерашнего дня!)
        в поле prev_day_start. Тогда завтра: prev_day_pnl = today_day_start - prev_day_start
        = (старт завтра) - (старт сегодня = старт вчера) = PnL вчера.
      - Если день в файле уже сегодня — ничего не делаем (уже отработано)."""
    data = _load_json(PREV_DAY_FILE, {})
    today = _today_utc_str()
    if data.get("date") == today:
        # Сегодня уже роллили — ничего не делаем
        return
    # Файл от вчера (или пустой) — roll
    today_day_start = get_day_start()
    if today_day_start is None:
        # Баланс ещё не загружен — пропустим, вызовется снова при следующем скане
        return
    # Сохраняем: date=сегодня, prev_day_start=сегодняшний day_start_equity
    # Завтра при скане: get_prev_day_pnl() = today_day_start(завтра) - prev_day_start(=сегодняшний старт)
    # = PnL за сегодня. После этого roll ещё раз: prev_day_start = завтрашний day_start.
    _save_prev_day_state(today_day_start)


# =====================================================================
#  АВТО-ОТСЛЕЖИВАНИЕ ЗАКРЫТИЯ ПОЗИЦИЙ (для cooldown без ручных команд)
# =====================================================================

def _load_tracked_positions() -> dict:
    """Чтение tracked_positions (БЕЗ блокировки — для внутренних вызовов,
    уже держащих _TRACKED_LOCK)."""
    return _load_json(OPEN_POS_FILE, {})


def _save_tracked_positions(d: dict):
    """Запись tracked_positions (БЕЗ блокировки — для внутренних вызовов)."""
    _atomic_write_json(OPEN_POS_FILE, d)


def _load_tracked_locked() -> dict:
    """Чтение tracked_positions с блокировкой (для внешних вызовов)."""
    with _TRACKED_LOCK:
        return _load_json(OPEN_POS_FILE, {})


def _save_tracked_locked(d: dict):
    """Запись tracked_positions с блокировкой."""
    with _TRACKED_LOCK:
        _atomic_write_json(OPEN_POS_FILE, d)


def _pnl_money(pv):
    """Парсинг realizedPnl — поле бывает либо обычным числом-строкой, либо fp9."""
    if pv is None:
        return None
    t = str(pv).strip()
    if not t or t in ("None", "null"):
        return None
    try:
        if "." in t or "e" in t.lower():
            return float(Decimal(t))
        return float(UE.from_fp9(t))
    except Exception:
        return None


def _order_ts(o: dict) -> float:
    for k in ("closedAt", "filledAt", "updatedAt", "createdAt"):
        v = o.get(k)
        if not v:
            continue
        try:
            if isinstance(v, str) and "T" in v:
                return dt.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
            return float(v) / (1000 if float(v) > 1e11 else 1)
        except Exception:
            continue
    return 0.0


def _fetch_realized_pnl_since(executor, symbol: str, since_ts: float) -> Optional[float]:
    """Суммарный realizedPnl по символу с момента открытия.
    Возвращает None при ошибке/недоступности API (чтобы вызывающий код НЕ считал
    pnl=0 = успех и НЕ сбрасывал consec_losses). 0.0 — это реальный ноль."""
    try:
        m = executor._mk.get(symbol) if executor._mk else None
        if not m:
            return None   # рынок не найден — НЕ 0.0
        asset = str(m.get("id", "")) or symbol
        data = executor.client.orders_history(executor.account_id, asset, 100)
    except Exception as e:
        print(f"[exec_dr22] pnl history {symbol}: {e}")
        return None   # ошибка API — НЕ 0.0
    if not isinstance(data, list):
        return None
    total = 0.0
    found_any = False
    for o in UE._as_list(data):
        if not isinstance(o, dict):
            continue
        ts = _order_ts(o)
        if ts and ts < since_ts - 60:
            continue
        v = _pnl_money(o.get("realizedPnl"))
        if v is not None:
            total += v
            found_any = True
    # Если не нашли ни одного ордера с PnL — возможно, история ещё не обновилась.
    # Возвращаем None, чтобы не сбросить consec_losses.
    return total if found_any else None


def _detect_closed_positions(executor, open_syms: set, universe: set = None):
    """Сверяет отслеживаемые donchian-позиции с реально открытыми на Upscale.
    Для каждой исчезнувшей — считает PnL и обновляет cooldown через
    on_trade_closed(). Если позиция была открыта сегодня — добавляет PnL
    в today_realized (для триггера Daily Stop today-only).
    Позиции, помеченные `dstop_closed=True` (закрыты Daily Stop-ом в этом скане),
    пропускаем — их PnL уже учтён в today_realized."""
    with _TRACKED_LOCK:
        tracked = _load_json(OPEN_POS_FILE, {})
        changed = False

        for sym in list(tracked.keys()):
            if sym in open_syms:
                continue
            rec = tracked[sym]
            # Пропускаем позиции, уже закрытые Daily Stop в текущем скане
            if rec.get("dstop_closed"):
                # Удаляем из tracked — на Upscale её уже нет, и PnL уже учтён
                del tracked[sym]
                changed = True
                continue
            opened_ts = rec.get("opened_ts", time.time())
            pnl = _fetch_realized_pnl_since(executor, sym, opened_ts)
            if pnl is None:
                # API недоступен или рынок не найден — НЕ сбрасываем cooldown,
                # оставляем позицию в tracked (попробуем в следующий скан)
                print(f"[exec_dr22] {sym}: не удалось получить PnL — оставляю в tracked")
                continue
            print(f"[exec_dr22] {sym}: позиция закрыта, pnl≈${pnl:.2f}")

            # Если позиция была открыта сегодня → учитываем в today_realized
            today_midnight = _today_midnight_ts()
            if opened_ts >= today_midnight:
                add_today_realized(pnl)

            on_trade_closed(sym, pnl)
            del tracked[sym]
            changed = True

        for sym in open_syms:
            if sym not in tracked and (universe is None or sym in universe):
                # C3/C4: восстановление после рестарта (tracked был пустой).
                # Не знаем entry/atr — управлять trailing не можем.
                # opened_ts = time.time() — НЕПРАВИЛЬНО, но мы помечаем recovered=True,
                # чтобы Daily Stop today-only НЕ считал эту позицию "сегодня-открытой".
                tracked[sym] = {
                    "opened_ts": time.time(),
                    "side": "",
                    "hold_days": 0,
                    "partial_taken": False,
                    "price_correction": 1.0,
                    "recovered": True,   # флаг: восстановлена после рестарта
                }
                changed = True

        if changed:
            _atomic_write_json(OPEN_POS_FILE, tracked)


def _today_midnight_ts() -> float:
    """TS начала текущего UTC дня (00:00 UTC)."""
    now = dt.datetime.now(dt.timezone.utc)
    midnight = dt.datetime(now.year, now.month, now.day, tzinfo=dt.timezone.utc)
    return midnight.timestamp()


def _track_new_position(symbol: str, side: str, entry: float = None,
                         atr: float = None, stop: float = None,
                         price_correction: float = 1.0):
    """Регистрирует новую позицию для отслеживания trailing-стопа.
    entry/atr/stop — по свечам Gate; price_correction = up_px/gate_px
    (для перевода уровней trailing-стопа в систему координат Upscale).
    НЕ перезаписывает существующую запись (защита от двойного сигнала)."""
    with _TRACKED_LOCK:
        tracked = _load_json(OPEN_POS_FILE, {})
        if symbol in tracked and tracked[symbol].get("opened_ts", 0) > 0:
            # Уже отслеживается — НЕ перезаписываем (C10: защита от двойного сигнала)
            return
        rec = {
            "opened_ts": time.time(),
            "side": side,
            "hold_days": 0,
            "partial_taken": False,
            "price_correction": price_correction,
        }
        if entry is not None:
            rec["entry"] = entry
        if atr is not None:
            rec["atr"] = atr
        if stop is not None:
            rec["trail_stop"] = stop
            rec["max_favorable"] = entry if entry is not None else stop
        tracked[symbol] = rec
        _atomic_write_json(OPEN_POS_FILE, tracked)
        _inc_new_today()   # C1: счётчик новых входов за сутки


def _recover_position_from_upscale(executor, sym: str, pos: dict) -> bool:
    """Пытается восстановить entry/atr/opened_ts для "сиротской" позиции
    (после рестарта Render, когда tracked_positions был пустой).
    Возвращает True, если удалось восстановить entry/atr — позицию можно вести.
    Возвращает False, если восстановить не удалось — будет только initial_stop."""
    try:
        # entry: из pos.get("entryPrice")/("avgEntry")/("openPrice") — Upscale API
        entry = None
        for k in ("entryPrice", "avgEntry", "openPrice", "entry"):
            v = pos.get(k)
            if v is None:
                continue
            try:
                if str(v).isdigit() and len(str(v)) > 9:
                    entry = float(UE.from_fp9(v))
                else:
                    entry = float(v)
                if entry > 0:
                    break
            except Exception:
                continue
        # opened_ts: из pos.get("openedAt")/("createdAt")
        opened_ts = None
        for k in ("openedAt", "createdAt", "updatedAt"):
            v = pos.get(k)
            if not v:
                continue
            try:
                if isinstance(v, str) and "T" in v:
                    opened_ts = dt.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
                elif float(v) > 1e11:
                    opened_ts = float(v) / 1000
                else:
                    opened_ts = float(v)
                break
            except Exception:
                continue

        # atr: считаем из текущих 4H свечей
        atr = None
        try:
            cds = _fetch_candles(sym, CANDLE_INTERVAL, ATR_PERIOD + 5)
            if len(cds) >= ATR_PERIOD + 1:
                a = _atr_daily(cds[:-1])
                if a is not None and a > 0:
                    atr = a
        except Exception:
            pass

        if entry is None or atr is None:
            return False

        side = UE._pos_dir(pos)
        if side not in ("long", "short"):
            return False
        s = 1 if side == "long" else -1
        trail_stop = entry - s * ATR_STOP_MULT * atr   # = initial_stop (т.к. trailing ещё не двигался)

        with _TRACKED_LOCK:
            tracked = _load_json(OPEN_POS_FILE, {})
            tracked[sym] = {
                "opened_ts": opened_ts if opened_ts else time.time(),
                "side": side,
                "hold_days": 0,   # НО: opened_ts правильный → Daily Stop сегодня не сработает
                "partial_taken": False,
                "price_correction": 1.0,
                "entry": entry,
                "atr": atr,
                "trail_stop": trail_stop,
                "max_favorable": entry,
                "recovered": True,
            }
            _atomic_write_json(OPEN_POS_FILE, tracked)
        return True
    except Exception as e:
        print(f"[exec_dr22] recover {sym}: {e}")
        return False


def _update_price_correction(executor, sym: str, sig_price: float) -> Optional[float]:
    """S16: после реального входа на Upscale сравниваем entryPrice с sig_price,
    вычисляем price_correction = up_entry / sig_price и сохраняем в tracked.
    Без этого trailing-стоп и breakeven смещены на ~0.5%."""
    try:
        cur_pos = next((p for p in executor._positions()
                        if UE._pos_market(p) and
                        any(str(m.get("id","")) == UE._pos_market(p) for m in executor._mk.values()
                            if executor._mk.get(sym) and str(m.get("id","")) == str(executor._mk[sym].get("id","")))),
                       None)
        if cur_pos is None:
            return None
        up_entry = None
        for k in ("entryPrice", "avgEntry", "openPrice"):
            v = cur_pos.get(k)
            if v is None:
                continue
            try:
                if str(v).isdigit() and len(str(v)) > 9:
                    up_entry = float(UE.from_fp9(v))
                else:
                    up_entry = float(v)
                if up_entry > 0:
                    break
            except Exception:
                continue
        if up_entry is None or sig_price <= 0:
            return None
        k_correction = up_entry / sig_price
        # Защита от мусора
        if not (0.9 < k_correction < 1.1):
            return None
        with _TRACKED_LOCK:
            tracked = _load_json(OPEN_POS_FILE, {})
            if sym in tracked:
                tracked[sym]["price_correction"] = k_correction
                _atomic_write_json(OPEN_POS_FILE, tracked)
        return k_correction
    except Exception as e:
        print(f"[exec_dr22] price_correction {sym}: {e}")
        return None


# =====================================================================
#  v2.2: УПРАВЛЕНИЕ ОТКРЫТЫМИ ПОЗИЦИЯМИ
#  - trailing-стоп 4.5×ATR по CLOSE свечи (НЕ по high/low!)
#  - Partial TP +8% favourable → close_position(info, size=half) + move_stop(entry)
#  - выход по развороту сигнала (close < Donchian-low для long)
#  - выход по времени (35 свечей 4H)
#  - проверка правила 60 сек Upscale перед Partial TP
# =====================================================================

def _manage_open_positions(executor, pos_by_sym: dict):
    """Ведёт уже открытые donchian-позиции: trailing, partial TP, выходы.
    pos_by_sym — {symbol: raw_position_dict} ТОЛЬКО по пулу donchian.
    Если позиция помечена recovered=True (после рестарта) — пытается восстановить
    entry/atr/opened_ts из API через _recover_position_from_upscale()."""
    today_midnight = _today_midnight_ts()

    with _TRACKED_LOCK:
        tracked_snapshot = dict(_load_json(OPEN_POS_FILE, {}))

    # ── ВОССТАНОВЛЕНИЕ ПОЗИЦИЙ ПОСЛЕ РЕСТАРТА (C3/C4) ──
    for sym, pos in pos_by_sym.items():
        st = tracked_snapshot.get(sym)
        if st and st.get("recovered") and st.get("entry") is None:
            # Пытаемся восстановить entry/atr/opened_ts из API Upscale
            if _recover_position_from_upscale(executor, sym, pos):
                if B:
                    B.send_telegram(f"♻️ <b>{sym}</b>: восстановил entry/atr/opened_ts из API Upscale (после рестарта)")
                with _TRACKED_LOCK:
                    tracked_snapshot = dict(_load_json(OPEN_POS_FILE, {}))

    # Перечитываем tracked после возможного восстановления
    with _TRACKED_LOCK:
        tracked = _load_json(OPEN_POS_FILE, {})
    changed = False

    for sym, pos in pos_by_sym.items():
        st = tracked.get(sym)
        if not st or st.get("atr") is None or st.get("entry") is None:
            # Нет entry/atr — управлять trailing-стопом нечем.
            # Исходный стоп, поставленный со входом, остаётся в силе.
            continue

        side = st.get("side") or UE._pos_dir(pos)
        if side not in ("long", "short"):
            continue
        s = 1 if side == "long" else -1

        try:
            cds = _fetch_candles(sym, CANDLE_INTERVAL, 300)
        except Exception as e:
            print(f"[exec_dr22] trailing {sym}: свечи недоступны: {e}")
            continue
        if len(cds) < DONCHIAN_PERIOD + 2:
            continue

        last = cds[-1]   # последняя ЗАКРЫТАЯ 4H-свеча
        close = last['c']

        st["hold_days"] = int(st.get("hold_days", 0)) + 1
        atr_entry = float(st["atr"])
        entry = float(st["entry"])
        max_fav = float(st.get("max_favorable", entry))

        # Обновляем max_favorable по максимуму/минимуму свечи (для Partial TP)
        if s == 1:
            max_fav = max(max_fav, last['h'])
        else:
            max_fav = min(max_fav, last['l'])
        st["max_favorable"] = max_fav

        # ── TRAILING-СТОП ПО CLOSE СВЕЧИ (главное отличие от v4.4!) ──
        if s == 1:
            new_stop = close - ATR_STOP_MULT * atr_entry
            old_stop = float(st.get("trail_stop", new_stop))
            tightened = new_stop > old_stop
            trail_stop = new_stop if tightened else old_stop
        else:
            new_stop = close + ATR_STOP_MULT * atr_entry
            old_stop = float(st.get("trail_stop", new_stop))
            tightened = new_stop < old_stop
            trail_stop = new_stop if tightened else old_stop

        st["trail_stop"] = trail_stop
        changed = True

        info = {"id": UE._pos_id(pos), "mid": UE._pos_market(pos), "dir": side}

        # ── 1. PARTIAL TP (если +8% favourable и прошло ≥ 60 сек Upscale rule) ──
        # Перенесён ВЫШЕ TIME/SIG (бэктест делает так: partial TP первым)
        if not st.get("partial_taken", False):
            if s == 1:
                pct_favorable = (max_fav - entry) / entry
            else:
                pct_favorable = (entry - max_fav) / entry

            if pct_favorable >= PARTIAL_TP_PCT:
                opened_ts = float(st.get("opened_ts", 0))
                if (time.time() - opened_ts) >= 60:
                    cur_pos = next((p for p in executor._positions() if UE._pos_id(p) == info["id"]), None)
                    if cur_pos:
                        size_total = UE._pos_size(cur_pos)
                        if size_total > 0:
                            partial_size = size_total // 2
                            if partial_size > 0:
                                # PnL частичного закрытия (через позицию Upscale)
                                up_pnl = _pnl_money(cur_pos.get("pnl"))
                                if up_pnl is None:
                                    # Fallback: по свечам Gate (только для partial_pnl оценки)
                                    price_corr = float(st.get("price_correction", 1.0))
                                    close_price_up = close * price_corr
                                    notional_now = (size_total / UE.FP) * entry * price_corr
                                    up_pnl = s * (close_price_up - entry * price_corr) / (entry * price_corr) * notional_now
                                partial_pnl_estimate = up_pnl * 0.5   # приблизительно половина PnL
                                try:
                                    res = executor.close_position(info, size=partial_size,
                                                                  reason=f"PTP +{pct_favorable*100:.1f}%")
                                    if B:
                                        B.send_telegram(
                                            f"🎯 <b>{sym}</b>: Partial TP +{pct_favorable*100:.1f}% — "
                                            f"закрыл 50% ({res})"
                                        )
                                    st["partial_taken"] = True
                                    # S7: учитываем partial_pnl в today_realized, если позиция открыта сегодня
                                    if opened_ts >= today_midnight:
                                        add_today_realized(partial_pnl_estimate)
                                    # Переводим trail_stop оставшейся половины в breakeven (entry)
                                    price_correction = float(st.get("price_correction", 1.0))
                                    entry_upscale = entry * price_correction
                                    if s == 1:
                                        if entry_upscale > trail_stop:
                                            trail_stop = entry_upscale
                                            st["trail_stop"] = trail_stop
                                    else:
                                        if entry_upscale < trail_stop:
                                            trail_stop = entry_upscale
                                            st["trail_stop"] = trail_stop
                                    try:
                                        executor.move_stop(info, trail_stop)
                                    except Exception as e:
                                        print(f"[exec_dr22] {sym}: move_stop to breakeven failed: {e}")
                                except Exception as e:
                                    if B:
                                        B.send_telegram(f"⚠️ {sym}: Partial TP failed: {e}")
                else:
                    if B:
                        B.send_telegram(f"⏳ {sym}: Partial TP отложен (правило 60 сек Upscale) — попробую на следующем скане")

        # ── 2. ВЫХОД ПО ВРЕМЕНИ (35 свечей 4H) ──
        if st["hold_days"] >= MAX_HOLD_DAYS:
            res = executor.close_position(info, reason=f"TIME {st['hold_days']}св")
            if B:
                B.send_telegram(f"⏱️ <b>{sym}</b>: выход по времени ({st['hold_days']} 4H-свечей) — {res}")
            # S7: PnL учтётся на следующем скане через _detect_closed_positions → add_today_realized
            continue

        # ── 3. ВЫХОД ПО РАЗВОРОТУ СИГНАЛА ──
        dc = _donchian(cds, DONCHIAN_PERIOD)
        if dc is not None:
            dc_high, dc_low = dc
            sig_exit = (s == 1 and close < dc_low) or (s == -1 and close > dc_high)
            if sig_exit:
                res = executor.close_position(info, reason="SIG разворот")
                if B:
                    B.send_telegram(f"🔁 <b>{sym}</b>: выход по сигналу (разворот Donchian) — {res}")
                # S7: PnL учтётся на следующем скане через _detect_closed_positions → add_today_realized
                continue

        # ── 4. ПОДТЯЖКА TRAILING-СТОПА (только если стал теснее) ──
        if tightened:
            price_correction = float(st.get("price_correction", 1.0))
            new_stop_upscale = trail_stop * price_correction
            try:
                res = executor.move_stop(info, new_stop_upscale)
                print(f"[exec_dr22] {sym}: trailing-стоп → {new_stop_upscale:.6g} ({res})")
            except Exception as e:
                print(f"[exec_dr22] {sym}: move_stop failed: {e}")

    if changed:
        with _TRACKED_LOCK:
            _atomic_write_json(OPEN_POS_FILE, tracked)


# =====================================================================
#  ИНДИКАТОРЫ — идентичны бэктесту
# =====================================================================

def _atr_daily(candles, period=ATR_PERIOD):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(-period, 0):
        c, prev = candles[i], candles[i - 1]
        trs.append(max(c['h'] - c['l'],
                       abs(c['h'] - prev['c']),
                       abs(c['l'] - prev['c'])))
    return sum(trs) / period


def _donchian(candles, period=DONCHIAN_PERIOD):
    if len(candles) < period + 1:
        return None
    window = candles[-(period + 1):-1]
    return max(c['h'] for c in window), min(c['l'] for c in window)


def _dmi(candles, period=DMI_PERIOD):
    if len(candles) < period * 2 + 1:
        return None, None, None
    plus_dm, minus_dm, trs = [], [], []
    for i in range(-period * 2, 0):
        c, prev = candles[i], candles[i - 1]
        up   = c['h'] - prev['h']
        down = prev['l'] - c['l']
        plus_dm.append(up   if (up   > down and up   > 0) else 0)
        minus_dm.append(down if (down > up   and down > 0) else 0)
        trs.append(max(c['h'] - c['l'],
                       abs(c['h'] - prev['c']),
                       abs(c['l'] - prev['c'])))
    if len(trs) < period or sum(trs[-period:]) == 0:
        return None, None, None
    atr_v    = sum(trs[-period:]) / period
    plus_d   = sum(plus_dm[-period:]) / period
    minus_d  = sum(minus_dm[-period:]) / period
    if atr_v <= 0:
        return None, None, None
    plus_di  = 100 * plus_d  / atr_v
    minus_di = 100 * minus_d / atr_v
    dx_values = []
    for j in range(period, period * 2 + 1):
        sub_tr    = trs[j - period:j]
        sub_plus  = plus_dm[j - period:j]
        sub_minus = minus_dm[j - period:j]
        if sum(sub_tr) == 0:
            continue
        a = sum(sub_tr) / period
        if a <= 0:
            continue
        pdi = 100 * sum(sub_plus)  / period / a
        mdi = 100 * sum(sub_minus) / period / a
        if pdi + mdi > 0:
            dx_values.append(100 * abs(pdi - mdi) / (pdi + mdi))
    adx = sum(dx_values) / len(dx_values) if dx_values else 0
    return plus_di, minus_di, adx


# =====================================================================
#  DATA FETCH
# =====================================================================

_CANDLE_CACHE = {}
_CANDLE_CACHE_TS = {}
CANDLE_CACHE_TTL = 1800  # 30 минут — на 4H свечах кеш можно подольше


_INTERVAL_SEC = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


def _fetch_candles(symbol, interval=CANDLE_INTERVAL, limit=300):
    """Берёт 4H свечи Gate.io. Кеширует на 30 минут (внутри одного 4H-скана).
    Фильтрует форминг-свечу — оставляет только закрытые."""
    gate_c = symbol if symbol.endswith("_USDT") else f"{symbol}_USDT"
    key = (gate_c, interval, limit)
    if key in _CANDLE_CACHE and (time.time() - _CANDLE_CACHE_TS.get(key, 0)) < CANDLE_CACHE_TTL:
        return _CANDLE_CACHE[key]
    # 3 страницы по 2000 свечей = ~6000 свечей 4H ≈ 1000 дней (2.7 года)
    pages_needed = 3 if interval == "4h" else 1
    all_candles = []
    to_ts = None
    for page in range(pages_needed):
        params = {"contract": gate_c, "interval": interval, "limit": 2000 if interval == "4h" else limit}
        if to_ts is not None:
            params["to"] = to_ts
        try:
            raw = B.api_get("candlesticks", params)
            page_candles = B.parse_candles(raw)
        except Exception as e:
            print(f"[warn] {symbol} page {page+1}: {e}")
            break
        if not page_candles:
            break
        page_candles.sort(key=lambda c: c['t'])
        if to_ts is not None:
            page_candles = [c for c in page_candles if c['t'] < to_ts]
        if not page_candles:
            break
        all_candles = page_candles + all_candles if all_candles else page_candles
        to_ts = page_candles[0]['t']
        if len(page_candles) < (2000 if interval == "4h" else limit):
            break

    # Дедуп
    seen, unique = set(), []
    for c in all_candles:
        if c['t'] not in seen:
            seen.add(c['t'])
            unique.append(c)
    unique.sort(key=lambda c: c['t'])

    # Фильтр форминг-свечи
    interval_sec = _INTERVAL_SEC.get(interval, 14400)
    now_ts = time.time()
    unique = [c for c in unique if c["t"] + interval_sec <= now_ts]

    _CANDLE_CACHE[key] = unique
    _CANDLE_CACHE_TS[key] = time.time()
    return unique


_BTC_REGIME_CACHE = None


def _get_btc_regime() -> int:
    """+1 если BTC > SMA50 на 1D, -1 если ниже, 0 если данных мало.
    S4: SMA50 считается по 50 свечам ДО последней (исключая текущую,
    как в бэктесте: closes[i - BTC_REGIME_SMA:i])."""
    global _BTC_REGIME_CACHE
    if _BTC_REGIME_CACHE is None:
        cds = _fetch_candles(BTC_CONTRACT, "1d", 200)
        if len(cds) < BTC_REGIME_SMA + 1:
            _BTC_REGIME_CACHE = 0
        else:
            closes = [c['c'] for c in cds]
            # S4: исключаем последнюю свечу — берём 50 ДО текущей
            s = sum(closes[-(BTC_REGIME_SMA + 1):-1]) / BTC_REGIME_SMA
            _BTC_REGIME_CACHE = +1 if cds[-1]['c'] > s else -1
    return _BTC_REGIME_CACHE


def _reset_btc_regime_cache():
    """Сбрасывает кеш BTC regime (нужно при новом 4H-скане после закрытия 1D-свечи)."""
    global _BTC_REGIME_CACHE
    _BTC_REGIME_CACHE = None


def _get_funding_snap() -> dict:
    try:
        return {
            t["contract"]: float(t.get("funding_rate", 0))
            for t in B.api_get("tickers", {})
            if t.get("contract")
        }
    except Exception as e:
        print(f"[exec_dr22] funding: {e}")
        return {}


# =====================================================================
#  ОЦЕНКА СИГНАЛА — идентична бэктесту
# =====================================================================

def _evaluate(candles, btc_regime, funding_snap, symbol) -> Optional[dict]:
    """Возвращает dict с сигналом или None. Сигнал = пробой Donchian(20) +
    BTC regime + DMI + ATR фильтр + |funding| ≤ 0.05%."""
    if len(candles) < DONCHIAN_PERIOD + 2:
        return None
    last = candles[-1]
    dc = _donchian(candles, DONCHIAN_PERIOD)
    if dc is None:
        return None
    dc_high, dc_low = dc
    a = _atr_daily(candles[:-1])
    if a is None or a <= 0:
        return None
    atr_pct = a / last['c']
    if not (ATR_PCT_MIN <= atr_pct <= ATR_PCT_MAX):
        return None
    plus_di, minus_di, adx = _dmi(candles[:-1])
    if plus_di is None:
        return None
    fc_key = symbol if symbol.endswith("_USDT") else f"{symbol}_USDT"
    funding = funding_snap.get(fc_key, 0.0)
    long_ok  = (last['c'] > dc_high and btc_regime == +1
                and plus_di > minus_di and abs(funding) <= 0.0005)
    short_ok = (last['c'] < dc_low and btc_regime == -1
                and minus_di > plus_di and abs(funding) <= 0.0005)
    if not long_ok and not short_ok:
        return None
    side = +1 if long_ok else -1
    price = last['c']
    stop     = price - side * ATR_STOP_MULT * a
    stop_pct = abs(price - stop) / price * 100
    return {
        "symbol":    symbol,
        "side":      "long" if side == +1 else "short",
        "price":     price,
        "stop":      stop,
        "stop_pct":  stop_pct,
        "exit_mode": "trailing",   # без TP1/TP2/TP3 — выходы ведёт _manage_open_positions
        "atr":       a,
        "atr_pct":   atr_pct,
        "dc_high":   dc_high,
        "dc_low":    dc_low,
        "plus_di":   plus_di,
        "minus_di":  minus_di,
        "adx":       adx,
        "funding":   funding,
        "ext_atr":   0.0,   # вход по закрытию свечи, не догоняем
    }


# =====================================================================
#  ГЛАВНАЯ ФУНКЦИЯ — ПРОВЕРКА СИГНАЛОВ
# =====================================================================

def check_signals():
    """Вызывается из bot.py каждые 4 часа (после закрытия 4H-свечи + 5 мин).
    Сканирует все пары, находит сигналы, передаёт в EXECUTOR.
    Также ведёт уже открытые позиции (trailing, partial TP, выходы).
    Защищена _CHECK_LOCK от параллельного /scan + 4H-расписания."""
    # C11: защита от параллельных вызовов (/scan + расписание 4H)
    if not _CHECK_LOCK.acquire(blocking=False):
        print("[exec_dr22] check_signals уже выполняется — пропускаю")
        if B:
            B.send_telegram("ℹ️ Скан уже выполняется (параллельный /scan?) — пропускаю.")
        return
    try:
        _check_signals_impl()
    finally:
        _CHECK_LOCK.release()


def _check_signals_impl():
    if B is None:
        print(f"[exec_dr22] bot.py недоступен: {_BOT_IMPORT_ERR}")
        return
    if UE is None:
        print(f"[exec_dr22] upscale_exec недоступен: {_UE_IMPORT_ERR}")
        return

    executor = getattr(B, "EXECUTOR", None)
    if executor is None:
        print("[exec_dr22] EXECUTOR не найден")
        return

    # Сбрасываем кеши для нового скана
    _CANDLE_CACHE.clear()
    _CANDLE_CACHE_TS.clear()
    _reset_btc_regime_cache()

    # ── 1. Пары (фильтр exclude) ──
    all_pairs = list(B.UPSCALE_PAIRS)
    pairs = [p for p in all_pairs if p not in EXCLUDE_PAIRS]

    # ── 2. Открытые позиции на Upscale + авто-детекция закрытий ──
    open_long, open_short = set(), set()
    pos_by_sym = {}
    positions_ok = False
    try:
        executor._ensure_account()
        if executor.client and executor.account_id:
            pos_list = UE._as_list(executor.client.positions(executor.account_id))
            executor._refresh_markets()
            for p in pos_list:
                if not isinstance(p, dict):
                    continue
                mid = UE._pos_market(p)
                sym = None
                if executor._mk:
                    for s, m in executor._mk.items():
                        if str(m.get("id", "")) == mid or s == mid:
                            sym = s
                            break
                if sym is None:
                    continue
                pos_by_sym[sym] = p
                if UE._pos_dir(p) == "short":
                    open_short.add(sym)
                else:
                    open_long.add(sym)
            positions_ok = True
    except Exception as e:
        print(f"[exec_dr22] позиции: {e}")

    if not positions_ok:
        B.send_telegram(
            "⛔ donchian_4h_v22: не удалось получить список открытых позиций "
            "Upscale — пропускаю весь скан (не трогаю cooldown и не открываю новых "
            "входов, чтобы не исказить учёт закрытых сделок на неполных данных)."
        )
        return

    pairs_set = set(pairs)
    open_long_dr42  = {s for s in open_long  if s in pairs_set}
    open_short_dr42 = {s for s in open_short if s in pairs_set}
    open_syms = open_long | open_short

    try:
        _detect_closed_positions(executor, open_syms, universe=pairs_set)
    except Exception as e:
        print(f"[exec_dr22] detect_closed_positions: {e}")

    # ── 3. Ведём открытые позиции (trailing, partial TP, выходы) ──
    try:
        pos_by_sym_dr42 = {s: p for s, p in pos_by_sym.items() if s in pairs_set}
        _manage_open_positions(executor, pos_by_sym_dr42)
    except Exception as e:
        print(f"[exec_dr22] manage_open_positions: {e}")

    # ── 4. Баланс ──
    bal_ok = refresh_balance(executor, force=True)
    if not bal_ok:
        msg = f"⛔ donchian_4h_v22: не удалось получить баланс Upscale — входы отменены.\n{_balance_cache.get('error', '')}"
        B.send_telegram(msg)
        return

    # S9: _maybe_roll_prev_day ПОСЛЕ refresh_balance (нужен day_start_equity)
    _maybe_roll_prev_day()

    equity   = get_equity()
    dd_brake = is_dd_brake_active()
    risk_usd = compute_risk_slot(equity, dd_brake)
    max_per_side = compute_max_per_side(risk_usd)
    max_total    = min(MAX_CONCURRENT, max_per_side * 2)

    # ── 5. Daily Stop today-only ──
    day_loss_stop_active = _load_day_stop_active()
    if not day_loss_stop_active:
        today_realized = get_today_realized()
        today_open_losses = 0.0
        today_midnight = _today_midnight_ts()
        tracked_snapshot = _load_tracked_locked()
        for sym, pos in pos_by_sym_dr42.items():
            st = tracked_snapshot.get(sym)
            if not st or st.get("opened_ts", 0) < today_midnight:
                continue
            # C4: пропускаем recovered позиции (открыты до рестарта, не сегодня)
            if st.get("recovered"):
                continue
            if st.get("entry") is None:
                continue
            # S15: нереализованный PnL — основной путь через Upscale, fallback через свечи Gate
            up_pnl = _pnl_money(pos.get("pnl"))
            if up_pnl is None:
                try:
                    cds = _fetch_candles(sym, CANDLE_INTERVAL, 50)
                    if cds:
                        last = cds[-1]
                        entry = float(st["entry"])
                        side_int = 1 if st.get("side") == "long" else -1
                        price_corr = float(st.get("price_correction", 1.0))
                        size = UE._pos_size(pos) / UE.FP
                        notional = size * entry * price_corr
                        up_pnl = side_int * (last['c'] - entry) / entry * notional
                except Exception:
                    up_pnl = 0.0
            if up_pnl is not None and up_pnl < 0:
                today_open_losses += up_pnl

        prev_day_pnl = get_prev_day_pnl()
        threshold = DAILY_STOP_LOSS_CONSEC if prev_day_pnl < 0 else DAILY_STOP_LOSS
        today_pnl = today_realized + today_open_losses

        if today_pnl <= threshold:
            day_loss_stop_active = True
            _save_day_stop_active(True)
            # Закрываем только сегодня-открытые убыточные
            closed_n = 0
            closed_pnl = 0.0
            today_midnight = _today_midnight_ts()
            with _TRACKED_LOCK:
                tracked = _load_json(OPEN_POS_FILE, {})
                for sym in list(tracked.keys()):
                    st = tracked.get(sym)
                    if not st or st.get("opened_ts", 0) < today_midnight:
                        continue
                    if st.get("recovered"):
                        continue
                    if st.get("entry") is None:
                        continue
                    pos = pos_by_sym_dr42.get(sym)
                    if not pos:
                        continue
                    up_pnl = _pnl_money(pos.get("pnl"))
                    # S15: fallback если up_pnl is None
                    if up_pnl is None:
                        try:
                            cds = _fetch_candles(sym, CANDLE_INTERVAL, 50)
                            if cds:
                                last = cds[-1]
                                entry = float(st["entry"])
                                side_int = 1 if st.get("side") == "long" else -1
                                price_corr = float(st.get("price_correction", 1.0))
                                size = UE._pos_size(pos) / UE.FP
                                notional = size * entry * price_corr
                                up_pnl = side_int * (last['c'] - entry) / entry * notional
                        except Exception:
                            up_pnl = 0.0
                    if up_pnl is None or up_pnl >= 0:
                        continue   # позиция в плюсе — не трогаем
                    info = {"id": UE._pos_id(pos), "mid": UE._pos_market(pos),
                            "dir": UE._pos_dir(pos)}
                    try:
                        res = executor.close_position(info, reason="DSTOP today-only")
                        closed_n += 1
                        closed_pnl += up_pnl
                        add_today_realized(up_pnl)
                        # C2: помечаем dstop_closed, чтобы _detect_closed_positions
                        # не учёл pnl повторно. Удалим из tracked при следующем скане.
                        tracked[sym]["dstop_closed"] = True
                        if B:
                            B.send_telegram(
                                f"🛑 <b>Daily Stop today-only {sym}</b>: позиция в убытке "
                                f"${up_pnl:.2f} — закрыта ({res})"
                            )
                    except Exception as e:
                        if B:
                            B.send_telegram(f"⚠️ DSTOP {sym}: не удалось закрыть — {e}")
                _atomic_write_json(OPEN_POS_FILE, tracked)

            B.send_telegram(
                f"🔴 <b>DAILY STOP today-only ${threshold:.0f}</b> сработал\n"
                f"Today realized: ${today_realized:.2f} | Today open losses: ${today_open_losses:.2f}\n"
                f"Закрыто сегодня-открытых убыточных: {closed_n} (PnL ${closed_pnl:.2f})\n"
                f"Старые позиции НЕ тронуты. Новые входы заблокированы до 00:00 UTC."
            )

    if day_loss_stop_active:
        B.send_telegram(
            f"🛑 donchian_4h_v22: Daily Stop today-only активен — новых входов нет до 00:00 UTC.\n"
            f"Открыто: {len(open_long_dr42)} long / {len(open_short_dr42)} short (продолжаем вести)"
        )
        return

    # ── 6. BTC режим ──
    btc_regime = _get_btc_regime()
    if btc_regime == 0:
        B.send_telegram("⚪ donchian_4h_v22: BTC нейтраль (SMA50 не определена) — входов нет.")
        return
    regime_str = "🐂 BULL" if btc_regime == +1 else "🐻 BEAR"

    # ── 7. Funding snapshot ──
    funding_snap = _get_funding_snap()

    side_today   = "long" if btc_regime == +1 else "short"
    open_on_side = open_long_dr42 if side_today == "long" else open_short_dr42
    open_dr42    = open_long_dr42 | open_short_dr42
    slots_side   = max(0, max_per_side - len(open_on_side))

    if len(open_dr42) >= max_total:
        B.send_telegram(f"⛔ donchian_4h_v22: уже {len(open_dr42)} позиций (лимит {max_total}).")
        return
    if slots_side <= 0:
        B.send_telegram(f"⛔ donchian_4h_v22: уже {len(open_on_side)} {side_today} позиций (лимит {max_per_side}).")
        return

    # ── 8. MAX_LOSERS_PER_SIDE фильтр ──
    long_losers = 0
    short_losers = 0
    tracked_snapshot = _load_tracked_locked()
    for sym in open_dr42:
        st = tracked_snapshot.get(sym)
        if not st or st.get("entry") is None:
            continue
        pos = pos_by_sym_dr42.get(sym)
        if not pos:
            continue
        up_pnl = _pnl_money(pos.get("pnl"))
        if up_pnl is not None and up_pnl < 0:
            if st.get("side") == "long":
                long_losers += 1
            else:
                short_losers += 1

    can_long  = (long_losers  < MAX_LOSERS_PER_SIDE) and btc_regime == +1
    can_short = (short_losers < MAX_LOSERS_PER_SIDE) and btc_regime == -1
    if side_today == "long" and not can_long:
        B.send_telegram(f"⛔ donchian_4h_v22: {long_losers} убыточных long позиций (лимит {MAX_LOSERS_PER_SIDE}) — новых long не открываем.")
        return
    if side_today == "short" and not can_short:
        B.send_telegram(f"⛔ donchian_4h_v22: {short_losers} убыточных short позиций (лимит {MAX_LOSERS_PER_SIDE}) — новых short не открываем.")
        return

    # ── 9. Cooldown ──
    cooldowns = _load_cooldowns()
    now_ts = time.time()

    # ── 10. Сканирование кандидатов ──
    candidates = []
    errors = []
    for sym in pairs:
        if sym in cooldowns and cooldowns[sym] > now_ts:
            continue
        if sym in open_syms:
            continue
        try:
            cds = _fetch_candles(sym, CANDLE_INTERVAL, 300)
            sig = _evaluate(cds, btc_regime, funding_snap, sym)
            if sig is None:
                continue
            if btc_regime == +1 and sig["side"] != "long":
                continue
            if btc_regime == -1 and sig["side"] != "short":
                continue
            sig["_di_gap"] = abs(sig["plus_di"] - sig["minus_di"])
            candidates.append(sig)
        except Exception as e:
            errors.append(f"{sym}: {e}")

    if errors:
        B.send_telegram("⚠️ drv22 ошибки " + str(len(errors)) + " пар: "
                        + ", ".join(errors[:5]) + ("..." if len(errors) > 5 else ""))

    # ── 11. Сортировка и лимит ──
    # S1: сортировка по atr_pct (как в бэктесте), не по _di_gap
    candidates.sort(key=lambda x: x["atr_pct"], reverse=True)
    # C1: лимит MAX_NEW_PER_DAY — суточный, не на скан
    new_today_count = _get_new_today_count()
    new_today_remaining = max(0, MAX_NEW_PER_DAY - new_today_count)
    slots_free = min(slots_side, max_total - len(open_dr42), new_today_remaining)
    to_trade = candidates[:max(0, slots_free)]

    # ── 12. Отчёт в Telegram ──
    stray = len(open_syms) - len(open_dr42)
    prev_day_pnl = get_prev_day_pnl()
    today_realized = get_today_realized()
    B.send_telegram(
        f"🔍 <b>donchian_4h_v22 v2.2-FINAL</b> | {regime_str}\n"
        + balance_summary() + "\n"
        + f"Risk/сделку: <b>${risk_usd:.1f}</b>" + (" 🔴 DD-brake" if dd_brake else "") + "\n"
        + f"Лимит в сторону: {max_per_side} (итого {max_total}) | "
        + f"Открыто: {len(open_long_dr42)} long / {len(open_short_dr42)} short"
        + (f" (+{stray} чужих)" if stray else "") + "\n"
        + f"Лузеры: {long_losers}L / {short_losers}S (лимит {MAX_LOSERS_PER_SIDE})\n"
        + f"Пар: {len(pairs)} (excl: {len(all_pairs)-len(pairs)}) | "
        + f"Cooldown: {len(cooldowns)} | "
        + f"Кандидаты: {len(candidates)} | Входим: {len(to_trade)}\n"
        + f"New today: {new_today_count}/{MAX_NEW_PER_DAY} | Today realized: ${today_realized:+.2f}"
        + (f"\nPrev day PnL: ${prev_day_pnl:+.2f}" + (" (consec)" if prev_day_pnl < 0 else "") if prev_day_pnl != 0 else "")
    )

    if not to_trade:
        B.send_telegram("donchian_4h_v22: сигналов нет.")
        return

    # ── 13. Передача сигналов в EXECUTOR ──
    # S3: убран жёсткий лимит $3000 на max_pos_usd (соответствие бэктесту)
    max_pos_usd_dr22 = float(equity) * MAX_POSITION_PCT

    for sig in to_trade:
        sym = sig["symbol"]
        side_str = "🟢 LONG" if sig["side"] == "long" else "🔴 SHORT"
        B.send_telegram(
            f"🎯 <b>СИГНАЛ {sym} {side_str}</b>\n"
            f"Цена: {sig['price']:.6g} | Стоп: {sig['stop']:.6g} ({sig['stop_pct']:.2f}%)\n"
            f"Выход: trailing 4.5×ATR по close / разворот Donchian / {MAX_HOLD_DAYS}св 4H + Partial TP +8%/50%\n"
            f"ATR: {sig['atr']:.4g} ({sig['atr_pct']*100:.2f}%)\n"
            f"+DI: {sig['plus_di']:.1f} / -DI: {sig['minus_di']:.1f} | "
            f"Funding: {sig['funding']*100:.4f}% | Risk: ${risk_usd:.1f}"
        )
        sig["risk_usd"]      = risk_usd
        sig["max_pos_usd"]   = max_pos_usd_dr22
        sig["max_same_side"] = max_per_side
        score = int(sig["_di_gap"])
        executor.on_signal(sig, score)
        # Отслеживаем сразу (on_signal асинхронный, но entry/atr/stop уже известны)
        _track_new_position(sym, sig["side"],
                            entry=sig["price"], atr=sig["atr"], stop=sig["stop"],
                            price_correction=1.0)
        # S16: обновляем price_correction после реального входа (асинхронно,
        # в отдельном потоке — позиция может появиться не сразу)
        def _update_correction_async(s=sym, p=sig["price"]):
            time.sleep(3)   # даём ордеру исполниться
            try:
                _update_price_correction(executor, s, p)
            except Exception as e:
                print(f"[exec_dr22] price_correction {s}: {e}")
        threading.Thread(target=_update_correction_async, daemon=True,
                          name=f"pxcorr-{sym}").start()


# =====================================================================
#  ОБНОВЛЕНИЕ COOLDOWN (вызывается из _detect_closed_positions)
# =====================================================================

def on_trade_closed(symbol: str, pnl: Optional[float]):
    """Обновляет consec_losses по паре. 4 убытка подряд → 14 дней cooldown.
    C5: если pnl is None (ошибка API / нет данных) — НЕ сбрасываем consec_losses.
    Убыток увеличивает счётчик, прибыль/ноль сбрасывает."""
    if pnl is None:
        # Ошибка API — ничего не делаем, ждём следующего скана
        print(f"[exec_dr22] on_trade_closed({symbol}): pnl is None — пропускаю")
        return

    state = _load_json(TRADE_STATE_FILE, {})
    sym_state = state.get(symbol, {"consec_losses": 0})

    if pnl < 0:
        sym_state["consec_losses"] = sym_state.get("consec_losses", 0) + 1
        if sym_state["consec_losses"] >= CONSEC_LOSS_LIMIT:
            cooldowns = _load_cooldowns()
            until = time.time() + COOLDOWN_DAYS * 86400
            cooldowns[symbol] = until
            _save_cooldowns(cooldowns)
            until_str = dt.datetime.fromtimestamp(until, dt.timezone.utc).strftime("%Y-%m-%d")
            if B:
                B.send_telegram(
                    f"🚫 <b>COOLDOWN {symbol}</b>: {CONSEC_LOSS_LIMIT} убытка подряд → "
                    f"блок до {until_str} ({COOLDOWN_DAYS} дн)"
                )
            sym_state["consec_losses"] = 0
    else:
        # Прибыль ИЛИ ноль (реальный ноль, не ошибка) — сбрасываем счётчик
        sym_state["consec_losses"] = 0

    state[symbol] = sym_state
    try:
        _atomic_write_json(TRADE_STATE_FILE, state)
    except Exception as e:
        print(f"[exec_dr22] state save: {e}")

    # Обновляем кеш баланса после закрытия (реализован PnL)
    refresh_balance(force=False)


# =====================================================================
#  СТАТУС ДЛЯ /up и /state, /trail, /daily
# =====================================================================

def status_report() -> str:
    """Статус стратегии — добавляется в /up."""
    cooldowns = _load_cooldowns()
    now_ts = time.time()
    active_cd = {k: v for k, v in cooldowns.items() if v > now_ts}

    lines = ["", "━━━ donchian_4h_v22 v2.2-FINAL ━━━"]
    lines.append(balance_summary())

    if B:
        try:
            r = _get_btc_regime()
            regime_str = "🐂 BULL" if r == +1 else ("🐻 BEAR" if r == -1 else "⚪ НЕЙТРАЛЬ")
            lines.append(f"BTC режим: {regime_str}")
        except Exception as e:
            lines.append(f"BTC режим: ⚠️ {e}")

    lines.append(f"Exclude: {sorted(EXCLUDE_PAIRS)}")
    lines.append(f"Выход: trailing {ATR_STOP_MULT}×ATR по close / разворот Donchian / {MAX_HOLD_DAYS}св 4H + Partial TP +{int(PARTIAL_TP_PCT*100)}%/{int(PARTIAL_TP_FRACTION*100)}%")

    eq = get_equity()
    if eq is not None:
        dd_brake = is_dd_brake_active()
        risk_now = compute_risk_slot(eq, dd_brake)
        same_now = compute_max_per_side(risk_now)
        day_loss = get_day_loss() or 0.0
        day_stop_active = _load_day_stop_active()
        today_realized = get_today_realized()
        lines.append(f"Риск сейчас: ${risk_now:.1f} → лимит {same_now} в сторону (итого {min(MAX_CONCURRENT, same_now*2)}) | New/day: {MAX_NEW_PER_DAY} | Лузера: {MAX_LOSERS_PER_SIDE}")
        lines.append(f"Daily Stop today-only: today_realized=${today_realized:.2f}, day_loss=${day_loss:.2f} / порог ${abs(DAILY_STOP_LOSS):.0f}"
                     + (" 🔴 АКТИВЕН (до 00:00 UTC)" if day_stop_active else ""))
    else:
        lines.append(f"Лимит в сторону: до {MAX_PER_SIDE_CAP} (риск-зависимо) | New/day: {MAX_NEW_PER_DAY} | Лузера: {MAX_LOSERS_PER_SIDE}")
    lines.append(f"Cooldown активных: {len(active_cd)}")

    if active_cd:
        for sym, until in sorted(active_cd.items()):
            until_str = dt.datetime.fromtimestamp(until, dt.timezone.utc).strftime("%Y-%m-%d")
            lines.append(f"  🚫 {sym} до {until_str}")

    return "\n".join(lines)


def state_report() -> str:
    """Подробное состояние для /state."""
    tracked = _load_tracked_positions()
    cooldowns = _load_cooldowns()
    now_ts = time.time()
    active_cd = {k: v for k, v in cooldowns.items() if v > now_ts}
    today_realized = get_today_realized()
    day_stop_active = _load_day_stop_active()
    prev_day_pnl = get_prev_day_pnl()
    peak = _load_peak()
    eq = get_equity()

    lines = ["", "━━━ /state — donchian_4h_v22 ━━━"]
    lines.append(f"Equity: ${eq:.2f}" if eq else "Equity: нет данных")
    lines.append(f"Пик: ${peak:.2f}" if peak else "Пик: нет данных")
    lines.append(f"DD от пика: ${peak-eq:.2f}" if peak and eq else "DD: нет данных")
    lines.append(f"DD-brake активен: {is_dd_brake_active()}")
    lines.append(f"Today realized (today-only P&L закрытых сегодня-открытых): ${today_realized:.2f}")
    lines.append(f"Daily Stop today-only active: {day_stop_active}")
    lines.append(f"Prev day PnL: ${prev_day_pnl:+.2f}" + (" (consec режим)" if prev_day_pnl < 0 else ""))
    lines.append(f"Tracked positions: {len(tracked)}")
    for sym, st in sorted(tracked.items()):
        lines.append(f"  • {sym} {st.get('side','?')}: hold={st.get('hold_days',0)}св, partial={st.get('partial_taken',False)}, entry={st.get('entry','?')}, trail_stop={st.get('trail_stop','?')}")
    lines.append(f"Cooldown active: {len(active_cd)}")
    for sym, until in sorted(active_cd.items()):
        until_str = dt.datetime.fromtimestamp(until, dt.timezone.utc).strftime("%Y-%m-%d")
        lines.append(f"  🚫 {sym} до {until_str}")
    return "\n".join(lines)


def trail_report() -> str:
    """Текущие trailing-стопы всех открытых donchian-позиций для /trail."""
    tracked = _load_tracked_positions()
    if not tracked:
        return "📐 Открытых donchian-позиций нет."

    lines = ["", "━━━ /trail — trailing-стопы ━━━"]
    for sym, st in sorted(tracked.items()):
        side = st.get("side", "?")
        hold = st.get("hold_days", 0)
        entry = st.get("entry")
        trail = st.get("trail_stop")
        partial = st.get("partial_taken", False)
        max_fav = st.get("max_favorable")
        atr = st.get("atr")
        partial_mark = " [PTP taken]" if partial else ""
        if entry and trail and atr:
            dist_pct = abs(trail - entry) / entry * 100
            lines.append(f"  • {sym} {side} | hold={hold}св{partial_mark} | entry={entry:.6g} trail={trail:.6g} (дистанция {dist_pct:.2f}%) | ATR={atr:.6g} | max_fav={max_fav:.6g}")
        else:
            lines.append(f"  • {sym} {side} | hold={hold}св | нет entry/atr (позиция не управляется)")
    return "\n".join(lines)


def daily_report() -> str:
    """P&L дня (сегодня, вчера) для /daily."""
    eq = get_equity()
    day_start = get_day_start()
    today_realized = get_today_realized()
    prev_day_pnl = get_prev_day_pnl()
    day_stop_active = _load_day_stop_active()

    lines = ["", "━━━ /daily — P&L дня ━━━"]
    if eq is not None and day_start is not None:
        day_pnl = eq - day_start
        lines.append(f"Сегодня (UTC): day_start=${day_start:.2f}, equity=${eq:.2f}, P&L дня=${day_pnl:+.2f}")
        lines.append(f"  из них today_realized (сегодня-открытых закрытых): ${today_realized:+.2f}")
    else:
        lines.append("⚠️ Баланс недоступен")

    if prev_day_pnl != 0:
        lines.append(f"Вчера (UTC): ${prev_day_pnl:+.2f}" + (" (consec режим)" if prev_day_pnl < 0 else ""))
    else:
        lines.append("Вчера: нет данных (первый день работы или файл утерян при рестарте)")

    lines.append(f"Daily Stop today-only: {'🔴 АКТИВЕН (до 00:00 UTC)' if day_stop_active else '✅ не активен'}")
    lines.append(f"Порог: ${abs(DAILY_STOP_LOSS):.0f} / подряд ${abs(DAILY_STOP_LOSS_CONSEC):.0f}")
    return "\n".join(lines)


# =====================================================================
#  ФОНОВОЕ ОБНОВЛЕНИЕ БАЛАНСА
# =====================================================================

def start_balance_updater():
    """Запускает фоновый поток обновления баланса каждые BALANCE_TTL_SEC."""
    def _loop():
        while True:
            time.sleep(BALANCE_TTL_SEC)
            try:
                ok = refresh_balance(force=True)
                if not ok and B:
                    print(f"[exec_dr22] balance update failed: {_balance_cache.get('error')}")
            except Exception as e:
                print(f"[exec_dr22] balance updater: {e}")

    t = threading.Thread(target=_loop, daemon=True, name="dr22-balance")
    t.start()
    print(f"[exec_dr22] фоновый обновитель баланса запущен (каждые {BALANCE_TTL_SEC}с = {BALANCE_TTL_SEC//60} мин)")


# =====================================================================
#  ТОЧКА ВХОДА
# =====================================================================

if __name__ == "__main__":
    print("exec_donchian_4h_v22.py — модуль исполнения стратегии Donchian 4H v2.2-FINAL")
    print()
    print("Функции для bot.py:")
    print("  check_signals()           — сканировать пары и передать сигналы в EXECUTOR")
    print("  on_trade_closed(sym, pnl) — обновить cooldown при закрытии сделки")
    print("  status_report()           — статус стратегии для /up")
    print("  state_report()             — подробное состояние для /state")
    print("  trail_report()              — trailing-стопы для /trail")
    print("  daily_report()              — P&L дня для /daily")
    print("  start_balance_updater()    — запустить фоновый поток обновления баланса")
    print("  balance_summary()         — строка с текущим балансом и риском")
    print("  refresh_balance(force=T)  — принудительно обновить баланс из API")
    print("  get_equity()              — текущий equity float или None")
    print("  is_dd_brake_active()      — True если DD > $900")
    print("  compute_risk_slot(eq, dd) — risk $ для одной сделки")
    print("  get_day_loss()            — убыток дня (UTC) float или None")
