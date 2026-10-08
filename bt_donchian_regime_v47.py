# -*- coding: utf-8 -*-
"""
bt_donchian_regime_v47.py - v4.7: v4.5 + Partial TP (+8%) / Breakeven stop
====================================================================

Запуск через диспетчер:
    RUN_BACKTEST=donchian_regime_v47 python bot.py

Что изменилось vs v4.5:
  Sideways-фильтр BTC (который был добавлен в v4.6) УБРАН по запросу
  пользователя — v4.7 строится от v4.5, не от v4.6.

  Вместо него — новая структура тейков (PARTIAL TP + BREAKEVEN):
    Если позиция дошла до +8% в свою сторону (favorable move от entry) —
    закрывается 50% позиции по цене закрытия дня (reason="PTP"),
    а trail_stop для остатка сразу переносится на entry (breakeven).
    Дальше остаток либо идёт по trailing 2xATR (большая прибыль),
    либо закрывается в ноль по breakeven — минус по сделке после
    partial TP становится невозможен (на остаток; сам partial кусок
    уже зафиксирован в плюс).

  PARTIAL_TP_PCT      = 0.08  (+8% favorable)
  PARTIAL_TP_FRACTION = 0.50  (закрывается половина позиции)

Новое в отчёте (без изменения логики стратегии):
  Для убыточных сделок, закрытых по стопу (SL/TRAIL), считаем:
    - MAE% (max adverse excursion) — максимальная просадка от entry
      в % за время удержания позиции (независимо от итоговой цены
      выхода — это худшая точка, которая была пройдена).
    - % таких сделок, где цена в течение 15 дней ПОСЛЕ закрытия по
      стопу всё же вернулась в сторону сделки (выше entry для Long,
      ниже entry для Short) — т.е. стоп вынес раньше времени.
  Это чисто информационная статистика (считается постфактум по факту
  котировок после закрытия), саму торговую логику она не меняет.

Что унаследовано из v4.5 (без изменений):
  - Compound sizing (floor $100, cap $200), DD brake x0.5 > $1,200
  - PerSide cap ОТКЛЮЧЁН (MAX_PER_SIDE_CAP=6=MAX_CONCURRENT)
  - Daily emergency stop -$400 (блок новых входов до конца дня)
  - Exclude 8 пар системных лузеров, per-pair cooldown 3/30
  - Trailing 2xATR / SIG-разворот / MAX_HOLD_DAYS=15
  - Весь расширенный отчёт v4.5: Long/Short win/loss, MFE% убыточных
    сделок, даты срабатывания daily stop, минусовые дни подряд,
    разбивка по парам с long/short — плюс новый блок Partial TP.
"""

import os
import sys
import math
import statistics
import datetime as dt
from collections import defaultdict

try:
    import bot as B
except Exception as e:
    B = None
    _BOT_IMPORT_ERR = e
else:
    _BOT_IMPORT_ERR = None


# =====================================================================
#  КОНСТАНТЫ
# =====================================================================

# --- Капитал и риск (v4.7-risk100: compound с floor $100) ---
INIT_CAPITAL     = 10_000.0
RISK_FRACTION    = 0.010      # 1.0% от equity на сделку
SLOT_RISK_MIN    = 100.0      # v4.7-risk100: floor $100
SLOT_RISK_MAX    = 200.0      # ceiling $200
MAX_POSITION_PCT = 0.20

# --- DD brake ---
DD_BRAKE_THRESHOLD = 1_200.0
DD_BRAKE_FACTOR    = 0.5
DD_BRAKE_RECOVERY  = 0.95

# --- v4.5: total limits (PerSide cap УБРАН) ---
MAX_CONCURRENT     = 6       # максимум одновременных позиций
MAX_PER_SIDE_CAP   = 6       # v4.5: = MAX_CONCURRENT, per-side cap отключён
PER_SIDE_BUDGET    = 999999  # v4.5: огромное число, per-side не ограничивает
DAILY_STOP_LOSS            = -300.0  # v4.7-risk100-v4: базовый порог (1-й минусовой день)
DAILY_STOP_LOSS_CONSEC     = -100.0  # v4.7-risk100-v4: 2-й минусовой день подряд -> порог -$100

# --- v4.4: Exclude + Cooldown (как в v4.3) ---
# FIX: UPSCALE_PAIRS в bot.py — голые тикеры ("TRX", без _USDT),
# поэтому EXCLUDE_PAIRS тоже должен быть без суффикса — иначе
# "p not in EXCLUDE_PAIRS" никогда не сработает.
EXCLUDE_PAIRS = {
    "TRX", "XLM", "BNB", "UNI",
    "LTC", "RUNE", "PENDLE", "HBAR",
}
CONSEC_LOSS_LIMIT = 3
COOLDOWN_DAYS     = 30

# --- Стратегия ---
DONCHIAN_PERIOD  = 20
BTC_REGIME_SMA   = 50
DMI_PERIOD       = 14
ADX_THRESHOLD    = 20.0     # в фильтре не используется, оставлен для отчёта
ATR_PERIOD       = 14
ATR_PCT_MIN      = 0.015
ATR_PCT_MAX      = 0.05

# --- Выходы ---
ATR_STOP_MULT    = 2.0
MAX_HOLD_DAYS    = 15
MAX_NEW_PER_DAY  = 2

# --- v4.7: Partial TP + Breakeven stop ---
PARTIAL_TP_PCT      = 0.08    # +8% favorable -> закрыть часть позиции (возвращено с 0.10)
PARTIAL_TP_FRACTION = 0.50    # какую долю закрыть

# v4.7: отчёт — сколько дней ПОСЛЕ стоп-выхода смотрим вперёд, чтобы
# проверить, вернулась ли цена в сторону сделки (чисто для статистики,
# на саму торговую логику не влияет)
STOP_REVERSAL_LOOKFORWARD_DAYS = 15
# После partial TP trail_stop перемещается на entry (breakeven) для остатка

# --- Издержки ---
COMM_TAKER       = 0.0005
SLIPPAGE         = 0.0002
FUNDING_TIMES_UTC = (0, 8, 16)

# --- Валидация ---
Z_SCORE          = 2.64
WORST_DAY_LIMIT  = -500.0
MAX_DD_LIMIT     = 2_000.0
YEAR_LOSS_LIMIT  = -500.0

BTC_CONTRACT     = "BTC_USDT"
BACKTEST_START_ISO = "2023-01-01"   # весь период (полная история)
BACKTEST_END_ISO   = ""             # пусто = окно идёт до сегодня


# =====================================================================
#  ИНДИКАТОРЫ
# =====================================================================

def sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def atr_daily(candles, period=ATR_PERIOD):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(-period, 0):
        c, prev = candles[i], candles[i - 1]
        tr = max(c['h'] - c['l'],
                 abs(c['h'] - prev['c']),
                 abs(c['l'] - prev['c']))
        trs.append(tr)
    return sum(trs) / period


def donchian(candles, period=DONCHIAN_PERIOD):
    """Возвращает (high_max, low_min) за период [-period-1 .. -1]
    (исключая последнюю свечу, чтобы избежать look-ahead)."""
    if len(candles) < period + 1:
        return None
    window = candles[-(period + 1):-1]
    highs = [c['h'] for c in window]
    lows  = [c['l'] for c in window]
    return max(highs), min(lows)


def dmi(candles, period=DMI_PERIOD):
    """Simplified DMI: returns (plus_di, minus_di, adx).
    plus_di > minus_di -> bullish trend stronger.
    Simple DX averaging (no Wilder smoothing)."""
    if len(candles) < period * 2 + 1:
        return None, None, None

    plus_dm, minus_dm, trs = [], [], []
    for i in range(-period * 2, 0):
        c, prev = candles[i], candles[i - 1]
        up   = c['h'] - prev['h']
        down = prev['l'] - c['l']
        if up > down and up > 0:
            plus_dm.append(up)
        else:
            plus_dm.append(0)
        if down > up and down > 0:
            minus_dm.append(down)
        else:
            minus_dm.append(0)
        trs.append(max(c['h'] - c['l'],
                       abs(c['h'] - prev['c']),
                       abs(c['l'] - prev['c'])))

    if len(trs) < period or sum(trs[-period:]) == 0:
        return None, None, None

    atr_v   = sum(trs[-period:])  / period
    plus_d  = sum(plus_dm[-period:])  / period
    minus_d = sum(minus_dm[-period:]) / period
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


def fetch_candles(contract, interval="1d", limit=2000):
    key = (contract, interval, limit)
    if key in _CANDLE_CACHE:
        return _CANDLE_CACHE[key]
    # FIX: Gate.io требует суффикс _USDT в имени контракта —
    # без неё все свечи молча не грузятся -> "Нет данных".
    gate_c = contract if contract.endswith("_USDT") else f"{contract}_USDT"
    raw = B.api_get("candlesticks", {
        "contract": gate_c,
        "interval": interval,
        "limit":    limit,
    })
    parsed = B.parse_candles(raw)
    _CANDLE_CACHE[key] = parsed
    return parsed


# =====================================================================
#  BTC REGIME
# =====================================================================

_BTC_REGIME_CACHE = None


def compute_btc_regime(btc_candles):
    """For each day returns:
        +1 if BTC close > SMA(50)_1d   (bullish -> only long)
        -1 if BTC close < SMA(50)_1d   (bearish -> only short)
         0 if not enough data
    """
    regime = {}
    closes = [c['c'] for c in btc_candles]
    for i, c in enumerate(btc_candles):
        if i < BTC_REGIME_SMA:
            regime[c['t']] = 0
            continue
        s = sum(closes[i - BTC_REGIME_SMA:i]) / BTC_REGIME_SMA
        regime[c['t']] = +1 if c['c'] > s else -1
    return regime


def get_btc_regime():
    global _BTC_REGIME_CACHE
    if _BTC_REGIME_CACHE is None:
        cds = fetch_candles(BTC_CONTRACT, "1d", 2000)
        _BTC_REGIME_CACHE = compute_btc_regime(cds)
    return _BTC_REGIME_CACHE


# =====================================================================
#  FUNDING SNAPSHOT
# =====================================================================

_FUNDING_CACHE = None


def get_funding_snapshot():
    global _FUNDING_CACHE
    if _FUNDING_CACHE is None:
        try:
            _FUNDING_CACHE = {
                t["contract"]: float(t.get("funding_rate", 0))
                for t in B.api_get("tickers", {})
                if t.get("contract")
            }
        except Exception:
            _FUNDING_CACHE = {}
    return _FUNDING_CACHE


# =====================================================================
#  ОЦЕНКА СИГНАЛА v4.2 (без ADX фильтра)
# =====================================================================

def evaluate_signal(candles_up_to_today, btc_regime_today, funding_snap):
    """Donchian(20) breakout + BTC regime + DMI direction + ATR_pct filter."""
    cds = candles_up_to_today
    if len(cds) < DONCHIAN_PERIOD + 2:
        return None

    last = cds[-1]
    dc   = donchian(cds, DONCHIAN_PERIOD)
    if dc is None:
        return None
    dc_high, dc_low = dc

    a = atr_daily(cds[:-1])
    if a is None or a <= 0:
        return None
    atr_pct = a / last['c']
    if not (ATR_PCT_MIN <= atr_pct <= ATR_PCT_MAX):
        return {"side": 0, "atr": a, "close": last['c'], "atr_pct": atr_pct,
                "dc_high": dc_high, "dc_low": dc_low,
                "plus_di": 0, "minus_di": 0, "adx": 0}

    plus_di, minus_di, adx = dmi(cds[:-1])
    if plus_di is None:
        return {"side": 0, "atr": a, "close": last['c'], "atr_pct": atr_pct,
                "dc_high": dc_high, "dc_low": dc_low,
                "plus_di": 0, "minus_di": 0, "adx": 0}

    funding = funding_snap.get(last.get('contract', ''), 0)

    long_ok = (last['c'] > dc_high
               and btc_regime_today == +1
               and plus_di > minus_di
               and abs(funding) <= 0.0005)
    short_ok = (last['c'] < dc_low
                and btc_regime_today == -1
                and minus_di > plus_di
                and abs(funding) <= 0.0005)

    side = +1 if long_ok else (-1 if short_ok else 0)
    return {"side": side, "atr": a, "close": last['c'], "atr_pct": atr_pct,
            "dc_high": dc_high, "dc_low": dc_low,
            "plus_di": plus_di, "minus_di": minus_di, "adx": adx}


# =====================================================================
#  POSITION v4.7: trailing stop + Partial TP / Breakeven
# =====================================================================

class Position:
    __slots__ = ("contract", "side", "entry", "atr_at_entry",
                 "size_usd", "original_size_usd", "initial_stop", "trail_stop",
                 "max_favorable", "max_adverse", "entry_idx", "entry_day_ts",
                 "hold_days", "donchian_at_entry", "partial_taken")

    def __init__(self, contract, side, entry, atr_at_entry,
                 size_usd, entry_idx, entry_day_ts, donchian_at_entry):
        self.contract           = contract
        self.side               = side
        self.entry              = entry
        self.atr_at_entry       = atr_at_entry
        self.size_usd           = size_usd
        self.original_size_usd  = size_usd   # v4.7: для отчёта
        self.initial_stop       = entry - side * ATR_STOP_MULT * atr_at_entry
        self.trail_stop         = self.initial_stop
        self.max_favorable      = entry
        self.max_adverse        = entry   # v4.7: для отчёта (MAE%)
        self.entry_idx         = entry_idx
        self.entry_day_ts      = entry_day_ts
        self.hold_days         = 0
        self.donchian_at_entry = donchian_at_entry
        self.partial_taken     = False      # v4.7: partial TP ещё не сработал

    def update_trail(self, candle):
        """Пересчитать max-favorable и trailing stop по новой свече."""
        if self.side == +1:
            self.max_favorable = max(self.max_favorable, candle['h'])
            self.max_adverse   = min(self.max_adverse, candle['l'])   # v4.7: MAE
            new_stop = self.max_favorable - ATR_STOP_MULT * self.atr_at_entry
            self.trail_stop = max(self.trail_stop, new_stop)
        else:
            self.max_favorable = min(self.max_favorable, candle['l'])
            self.max_adverse   = max(self.max_adverse, candle['h'])   # v4.7: MAE
            new_stop = self.max_favorable + ATR_STOP_MULT * self.atr_at_entry
            self.trail_stop = min(self.trail_stop, new_stop)

    def check_partial_tp(self, candle):
        """v4.7: Partial TP — если цена дошла до +PARTIAL_TP_PCT в сторону
        позиции, закрыть PARTIAL_TP_FRACTION позиции по цене закрытия свечи.
        После этого trail_stop перемещается на entry (breakeven) для остатка.
        Возвращает dict с инфо о partial TP или None."""
        if self.partial_taken:
            return None

        if self.side == +1:
            pct_favorable = (self.max_favorable - self.entry) / self.entry
        else:
            pct_favorable = (self.entry - self.max_favorable) / self.entry

        if pct_favorable < PARTIAL_TP_PCT:
            return None

        exit_price = candle['c']
        partial_size = self.size_usd * PARTIAL_TP_FRACTION
        partial_pnl = self.side * (exit_price - self.entry) / self.entry * partial_size

        self.size_usd -= partial_size
        self.partial_taken = True

        # переместить trail_stop на breakeven (entry), чтобы остаток
        # не мог уйти в минус
        if self.side == +1:
            self.trail_stop = max(self.trail_stop, self.entry)
        else:
            self.trail_stop = min(self.trail_stop, self.entry)

        return {
            "partial_size": partial_size,
            "partial_pnl": partial_pnl,
            "partial_price": exit_price,
            "favorable_pct": pct_favorable,
        }


# =====================================================================
#  LIVE-ФИЛЬТРЫ (для реальной торговли)
# =====================================================================

def live_signal_filters(contract):
    """Опциональные фильтры для live. В бэктесте не вызываются."""
    try:
        tickers = B.api_get("tickers", {})
        t = next((x for x in tickers if x.get("contract") == contract), None)
        if t is None:
            return False, "no ticker"
        funding = float(t.get("funding_rate", 0))
        if abs(funding) > 0.0005:
            return False, f"funding={funding*100:.3f}%"

        stats = B.api_get("contract_stats", {
            "contract": contract, "interval": "5m", "limit": 50})
        if not stats or len(stats) < 12:
            return False, "no stats"

        latest = stats[-1]
        prev   = stats[-12]
        lsr = float(latest.get("lsr_taker", 1.0))
        if not (1.0 <= lsr <= 2.0):
            return False, f"LSR={lsr:.2f}"
        oi_now  = float(latest.get("open_interest_usd", 0))
        oi_prev = float(prev.get("open_interest_usd", 0))
        if oi_now <= oi_prev:
            return False, "OI flat/down"
        long_liq  = float(latest.get("long_liq_usd", 0))
        short_liq = float(latest.get("short_liq_usd", 0))
        if (long_liq + short_liq) > 0.01 * oi_now and oi_now > 0:
            return False, f"liq cascade"
        return True, "ok"
    except Exception as e:
        return False, f"err: {e}"


# =====================================================================
#  ДВИЖОК БЭКТЕСТА
# =====================================================================

def compute_risk_slot(equity, dd_brake_active=False):
    """v4.7-risk100: compound sizing с floor $100, cap $200, brake x0.5."""
    base = max(SLOT_RISK_MIN, min(SLOT_RISK_MAX, equity * RISK_FRACTION))
    if dd_brake_active:
        base *= DD_BRAKE_FACTOR
    return base


def compute_max_per_side(current_risk):
    """v4.5: PerSide cap отключён, возвращает MAX_CONCURRENT."""
    if current_risk <= 0:
        return 0
    return min(MAX_PER_SIDE_CAP, int(PER_SIDE_BUDGET / current_risk))


def run_backtest(pairs, start_iso=BACKTEST_START_ISO, end_iso=BACKTEST_END_ISO, verbose=True):
    if B is None:
        raise RuntimeError("bot module not available")

    # --- 1) Свечи (с exclude-фильтром v4.4) ---
    pairs_active = [p for p in pairs if p not in EXCLUDE_PAIRS]
    excluded = len(pairs) - len(pairs_active)
    if verbose:
        B.send_telegram(f"📡 v4.7: загружаю 1d свечи для {len(pairs_active)} пар "
                        f"(excluded {excluded})")
    data = {}
    for i, p in enumerate(pairs_active):
        try:
            cds = fetch_candles(p, "1d", 2000)
            if cds:
                data[p] = cds
        except Exception as e:
            print(f"[warn] {p}: {e}")
        if verbose and (i + 1) % 20 == 0:
            B.send_telegram(f"  загружено {i+1}/{len(pairs_active)}")

    if not data:
        raise RuntimeError("Нет данных")

    # --- 2) BTC regime ---
    if verbose:
        B.send_telegram("📡 v4.7: вычисляю BTC weekly regime...")
    btc_regime = get_btc_regime()
    funding_snap = get_funding_snapshot()

    # --- 4) Общий таймлайн ---
    # end_iso ограничивает окно СВЕРХУ (нужно для непересекающегося "второго
    # периода" — без этого all_days всегда шёл до сегодня).
    start_ts = int(dt.datetime.fromisoformat(start_iso).timestamp())
    end_ts = int(dt.datetime.fromisoformat(end_iso).timestamp()) if end_iso else None
    all_days = sorted(set(
        c['t'] for p in data for c in data[p]
        if c['t'] >= start_ts and (end_ts is None or c['t'] < end_ts)
    ))
    if len(all_days) < DONCHIAN_PERIOD + BTC_REGIME_SMA + 5:
        raise RuntimeError(f"Слишком мало дней: {len(all_days)}")

    by_pair_day = {p: {c['t']: c for c in cds} for p, cds in data.items()}
    idx_by_pair_day = {
        p: {c['t']: i for i, c in enumerate(cds)}
        for p, cds in data.items()
    }

    # --- 5) Состояние ---
    cash = INIT_CAPITAL
    positions = []
    closed_trades = []
    equity_curve = []
    daily_pnl = []

    peak_equity = INIT_CAPITAL
    dd_brake_active = False
    dd_brake_days   = 0

    btc_blocked = 0
    adx_filtered = 0

    # v4.4: Per-pair cooldown state
    pair_stats = defaultdict(lambda: {"consec_losses": 0, "cooldown_until": 0})
    cooldown_blocked = 0
    excluded_count = excluded

    # v4.4: Daily emergency stop state
    day_loss_stop_active = False
    day_stop_triggered = 0
    last_day_idx = -1
    day_stop_events = []   # v4.5: [{"day": ts, "open_before": n, "open_left": n}, ...]

    # v4.7-risk100-v4: tracking previous day's PnL для consecutive-day logic
    prev_day_pnl = 0.0   # PnL предыдущего дня (для определения 2-го дня подряд в минусе)
    consec_loss_days_count = 0   # сколько дней подряд в минусе (для отчёта)
    consec_loss_days_max = 0     # макс серия минусовых дней (для отчёта)

    # v4.7: Partial TP счётчик
    partial_tp_count = 0
    partial_tp_total_pnl = 0.0

    # --- 6) Главный цикл ---
    for day_idx, day_ts in enumerate(all_days):
        btc_r = btc_regime.get(day_ts, 0)
        if btc_r == 0:
            btc_blocked += 1

        # v4.4: сброс флага daily stop в начале нового дня
        if day_idx != last_day_idx:
            day_loss_stop_active = False
            last_day_idx = day_idx

        # 6.1) Partial TP -> Закрытие по trailing stop / TIME / SIG
        positions_before = len(positions)   # v4.5: для отчёта "закрылись ли все позиции"
        new_positions = []
        realized_today = 0.0
        for pos in positions:
            candle = by_pair_day[pos.contract].get(day_ts)
            if candle is None:
                new_positions.append(pos)
                continue

            pos.hold_days += 1
            pos.update_trail(candle)   # пересчёт трейла по intraday high/low

            # v4.7: проверка partial TP (до основных exit-ов)
            ptp = pos.check_partial_tp(candle)
            if ptp is not None:
                partial_tp_count += 1
                partial_tp_total_pnl += ptp["partial_pnl"]
                realized_today += ptp["partial_pnl"]
                closed_trades.append({
                    "contract": pos.contract, "side": pos.side,
                    "entry": pos.entry, "exit": ptp["partial_price"],
                    "size_usd": ptp["partial_size"], "pnl": ptp["partial_pnl"],
                    "reason": "PTP", "hold_days": pos.hold_days,
                    "entry_day": pos.entry_day_ts, "exit_day": day_ts,
                    "max_favorable": pos.max_favorable,
                    "max_adverse": pos.max_adverse,
                })

            exit_price, exit_reason = None, None

            if pos.side == +1 and candle['l'] <= pos.trail_stop:
                exit_price, exit_reason = pos.trail_stop, "TRAIL"
            elif pos.side == -1 and candle['h'] >= pos.trail_stop:
                exit_price, exit_reason = pos.trail_stop, "TRAIL"
            elif pos.side == +1 and candle['l'] <= pos.initial_stop and pos.hold_days == 1:
                exit_price, exit_reason = pos.initial_stop, "SL"
            elif pos.side == -1 and candle['h'] >= pos.initial_stop and pos.hold_days == 1:
                exit_price, exit_reason = pos.initial_stop, "SL"

            if exit_price is None and pos.hold_days >= MAX_HOLD_DAYS:
                exit_price, exit_reason = candle['c'], "TIME"

            if exit_price is None:
                idx = idx_by_pair_day[pos.contract].get(day_ts)
                cds = data[pos.contract]
                if idx is not None and idx >= DONCHIAN_PERIOD + 1:
                    dc_h = max(c['h'] for c in cds[idx - DONCHIAN_PERIOD:idx])
                    dc_l = min(c['l'] for c in cds[idx - DONCHIAN_PERIOD:idx])
                    if pos.side == +1 and candle['c'] < dc_l:
                        exit_price, exit_reason = candle['c'], "SIG"
                    elif pos.side == -1 and candle['c'] > dc_h:
                        exit_price, exit_reason = candle['c'], "SIG"

            if exit_price is not None:
                gross = pos.side * (exit_price - pos.entry) / pos.entry * pos.size_usd
                comm  = (COMM_TAKER + SLIPPAGE) * pos.size_usd * 2
                n_fund = 0
                cur = pos.entry_day_ts
                while cur < day_ts:
                    if dt.datetime.utcfromtimestamp(cur).hour in FUNDING_TIMES_UTC:
                        n_fund += 1
                    cur += 3600
                # FIX: funding_snap ключи — _USDT, pos.contract — голый тикер
                _fc = pos.contract if pos.contract.endswith("_USDT") else f"{pos.contract}_USDT"
                funding_rate = funding_snap.get(_fc, 0.0)
                funding_cost = pos.side * funding_rate * pos.size_usd * n_fund
                net = gross - comm - funding_cost
                realized_today += net
                closed_trades.append({
                    "contract": pos.contract, "side": pos.side,
                    "entry": pos.entry, "exit": exit_price,
                    "size_usd": pos.size_usd, "pnl": net,
                    "reason": exit_reason, "hold_days": pos.hold_days,
                    "entry_day": pos.entry_day_ts, "exit_day": day_ts,
                    "max_favorable": pos.max_favorable,
                    "max_adverse": pos.max_adverse,
                })
                # v4.4: обновляем pair_stats для cooldown
                ps = pair_stats[pos.contract]
                if net < 0:
                    ps["consec_losses"] += 1
                    if ps["consec_losses"] >= CONSEC_LOSS_LIMIT:
                        ps["cooldown_until"] = day_ts + COOLDOWN_DAYS * 86400
                else:
                    ps["consec_losses"] = 0
            else:
                new_positions.append(pos)

        positions = new_positions

        # 6.2) Mark-to-market
        unrealized = 0.0
        for pos in positions:
            c = by_pair_day[pos.contract].get(day_ts)
            if c is None:
                continue
            unrealized += pos.side * (c['c'] - pos.entry) / pos.entry * pos.size_usd

        equity = cash + realized_today + unrealized
        cash  += realized_today
        equity_curve.append((day_ts, equity))

        if equity > peak_equity:
            peak_equity = equity
        if dd_brake_active and equity >= peak_equity * DD_BRAKE_RECOVERY:
            dd_brake_active = False
        elif (not dd_brake_active) and (peak_equity - equity) > DD_BRAKE_THRESHOLD:
            dd_brake_active = True
            dd_brake_days += 1
        elif dd_brake_active:
            dd_brake_days += 1

        if day_idx == 0:
            day_pnl = equity - INIT_CAPITAL
        else:
            day_pnl = equity - equity_curve[-2][1]
        daily_pnl.append((day_ts, day_pnl))

        # 6.3) Новые входы
        if btc_r == 0:
            continue
        if len(positions) >= MAX_CONCURRENT:
            continue

        # v4.4: compound sizing + DD brake
        current_risk = compute_risk_slot(equity, dd_brake_active)

        # v4.5: per-side cap отключён, max_per_side = MAX_CONCURRENT
        max_per_side = compute_max_per_side(current_risk)
        long_count  = sum(1 for p in positions if p.side == +1)
        short_count = sum(1 for p in positions if p.side == -1)

        # v4.4: daily emergency stop
        # v4.7-risk100-v5: триггер — суммарный дневной убыток:
        #   - realized_today (только отрицательные закрытые сделки за день)
        #   + unrealized (только отрицательные, открытые позиции)
        # Если суммарный дневной убыток <= порога — закрываем убыточные позиции
        # в "плохую" сторону (где больше убыток). Прибыльные оставляем.
        # Порог динамический:
        #   - Вчера был минус → -$100 (2-й день подряд, агрессивнее)
        #   - Иначе → -$300 (базовый)
        if prev_day_pnl < 0:
            current_threshold = DAILY_STOP_LOSS_CONSEC   # -$100 (2-й день подряд в минусе)
        else:
            current_threshold = DAILY_STOP_LOSS          # -$300 (базовый)
        # Считаем unrealized по сторонам и сумму только убыточных
        long_unrealized  = 0.0
        short_unrealized = 0.0
        open_losses = 0.0   # только отрицательные unrealized
        for pos in positions:
            c = by_pair_day[pos.contract].get(day_ts)
            if c is None:
                continue
            pos_unrealized = pos.side * (c['c'] - pos.entry) / pos.entry * pos.size_usd
            if pos.side == +1:
                long_unrealized += pos_unrealized
            else:
                short_unrealized += pos_unrealized
            if pos_unrealized < 0:   # только убытки
                open_losses += pos_unrealized
        # v4.7-risk100-v5: считаем также УБЫТОЧНЫЕ closed trades за сегодня
        realized_losses_today = 0.0
        for t in closed_trades:
            if t["exit_day"] == day_ts and t["pnl"] < 0:
                realized_losses_today += t["pnl"]
        # СУММАРНЫЙ дневной убыток: закрытые минусы + открытые минусы
        total_daily_losses = realized_losses_today + open_losses
        # Триггер: суммарный дневной убыток пробил порог
        if total_daily_losses <= current_threshold:
            if not day_loss_stop_active:
                day_loss_stop_active = True
                day_stop_triggered += 1
                # Определяем "плохую" сторону (с наибольшим убытком)
                bad_side = +1 if long_unrealized <= short_unrealized else -1
                # Закрываем только убыточные позиции в "плохую" сторону
                positions_remaining = []
                dstop_realized = 0.0
                for pos in positions:
                    candle_now = by_pair_day[pos.contract].get(day_ts)
                    if candle_now is None:
                        positions_remaining.append(pos)
                        continue
                    pos_unrealized = pos.side * (candle_now['c'] - pos.entry) / pos.entry * pos.size_usd
                    if pos.side == bad_side and pos_unrealized < 0:
                        exit_price_now = candle_now['c']
                        gross_now = pos.side * (exit_price_now - pos.entry) / pos.entry * pos.size_usd
                        comm_now  = (COMM_TAKER + SLIPPAGE) * pos.size_usd * 2
                        n_fund_now = 0
                        cur_now = pos.entry_day_ts
                        while cur_now < day_ts:
                            if dt.datetime.utcfromtimestamp(cur_now).hour in FUNDING_TIMES_UTC:
                                n_fund_now += 1
                            cur_now += 3600
                        _fc_now = pos.contract if pos.contract.endswith("_USDT") else f"{pos.contract}_USDT"
                        funding_rate_now = funding_snap.get(_fc_now, 0.0)
                        funding_cost_now = pos.side * funding_rate_now * pos.size_usd * n_fund_now
                        net_now = gross_now - comm_now - funding_cost_now
                        realized_today += net_now
                        dstop_realized  += net_now
                        closed_trades.append({
                            "contract": pos.contract, "side": pos.side,
                            "entry": pos.entry, "exit": exit_price_now,
                            "size_usd": pos.size_usd, "pnl": net_now,
                            "reason": "DSTOP", "hold_days": pos.hold_days,
                            "entry_day": pos.entry_day_ts, "exit_day": day_ts,
                            "max_favorable": pos.max_favorable,
                            "max_adverse": pos.max_adverse,
                        })
                    else:
                        positions_remaining.append(pos)
                positions = positions_remaining
                # Пересчёт cash/equity/daily_pnl с учётом DSTOP-закрытий
                cash      += dstop_realized
                unrealized = 0.0
                for pos in positions:
                    c = by_pair_day[pos.contract].get(day_ts)
                    if c is None:
                        continue
                    unrealized += pos.side * (c['c'] - pos.entry) / pos.entry * pos.size_usd
                equity = cash + unrealized
                equity_curve[-1] = (day_ts, equity)
                prev_eq = INIT_CAPITAL if day_idx == 0 else equity_curve[-2][1]
                daily_pnl[-1] = (day_ts, equity - prev_eq)
                day_stop_events.append({
                    "day": day_ts,
                    "open_before": positions_before,
                    "open_left": len(positions),
                    "all_closed": positions_before > 0 and len(positions) == 0,
                    "threshold_used": current_threshold,
                    "consec_day": prev_day_pnl < 0,
                })
        if day_loss_stop_active:
            # v4.7-risk100-v4: даже если сработал stop, обновляем prev_day_pnl
            # для следующего дня (день будет в минусе, завтра порог снова -$100)
            prev_day_pnl = daily_pnl[-1][1]
            if prev_day_pnl < 0:
                consec_loss_days_count += 1
                if consec_loss_days_count > consec_loss_days_max:
                    consec_loss_days_max = consec_loss_days_count
            else:
                consec_loss_days_count = 0
            continue

        # v4.7-risk100-v4: обновляем prev_day_pnl для следующего дня
        # (используем итоговый daily_pnl текущего дня)
        prev_day_pnl = daily_pnl[-1][1]
        if prev_day_pnl < 0:
            consec_loss_days_count += 1
            if consec_loss_days_count > consec_loss_days_max:
                consec_loss_days_max = consec_loss_days_count
        else:
            consec_loss_days_count = 0

        can_long  = long_count  < max_per_side and btc_r == +1
        can_short = short_count < max_per_side and btc_r == -1

        candidates = []
        for p, cds in data.items():
            idx = idx_by_pair_day[p].get(day_ts)
            if idx is None or idx < DONCHIAN_PERIOD + 2:
                continue
            if any(pos.contract == p for pos in positions):
                continue
            # v4.4: проверка cooldown
            ps = pair_stats[p]
            if day_ts < ps["cooldown_until"]:
                cooldown_blocked += 1
                continue
            sig = evaluate_signal(cds[:idx + 1], btc_r, funding_snap)
            if sig is None or sig["side"] == 0:
                if sig and sig.get("adx", 100) < ADX_THRESHOLD:
                    adx_filtered += 1
                continue
            if sig["side"] == +1 and not can_long:
                continue
            if sig["side"] == -1 and not can_short:
                continue
            candidates.append((p, sig, idx))

        candidates.sort(
            key=lambda x: abs(x[1]["plus_di"] - x[1]["minus_di"]),
            reverse=True)

        new_today = 0
        for p, sig, idx in candidates:
            if new_today >= MAX_NEW_PER_DAY:
                break
            if len(positions) >= MAX_CONCURRENT:
                break
            # v4.5: перепроверка per-side лимита (но cap = MAX_CONCURRENT, не ограничивает)
            if sig["side"] == +1:
                if long_count >= max_per_side:
                    continue
                long_count += 1
            else:
                if short_count >= max_per_side:
                    continue
                short_count += 1

            stop_dist = ATR_STOP_MULT * sig["atr"]
            stop_pct  = stop_dist / sig["close"]
            if stop_pct <= 0:
                continue
            raw_size  = current_risk / stop_pct     # v4.4: compound risk
            size_usd  = min(raw_size, MAX_POSITION_PCT * equity)
            if size_usd < 50:
                continue

            entry_price = sig["close"]
            pos = Position(p, sig["side"], entry_price, sig["atr"],
                           size_usd, idx, day_ts, sig["dc_high"])
            positions.append(pos)
            cash -= COMM_TAKER * size_usd
            new_today += 1

    # 7) Закрытие остатков
    last_day_ts = all_days[-1]
    for pos in positions:
        c = by_pair_day[pos.contract].get(last_day_ts)
        if c is None:
            continue
        gross = pos.side * (c['c'] - pos.entry) / pos.entry * pos.size_usd
        comm  = (COMM_TAKER + SLIPPAGE) * pos.size_usd * 2
        net   = gross - comm
        closed_trades.append({
            "contract": pos.contract, "side": pos.side,
            "entry": pos.entry, "exit": c['c'],
            "size_usd": pos.size_usd, "pnl": net,
            "reason": "END", "hold_days": pos.hold_days,
            "entry_day": pos.entry_day_ts, "exit_day": last_day_ts,
            "max_favorable": pos.max_favorable,
            "max_adverse": pos.max_adverse,
        })
        cash += net

    # 8) v4.7: для убыточных стоп-выходов (SL/TRAIL) — проверяем постфактум,
    #    вернулась ли цена в течение STOP_REVERSAL_LOOKFORWARD_DAYS дней
    #    ПОСЛЕ закрытия обратно в сторону сделки (выше entry для Long,
    #    ниже entry для Short). Чисто отчётная статистика, саму торговлю
    #    не меняет и не влияет ни на один вход/выход выше.
    for t in closed_trades:
        if t["reason"] not in ("SL", "TRAIL") or t["pnl"] > 0:
            continue
        cds = data.get(t["contract"])
        idx_map = idx_by_pair_day.get(t["contract"])
        if not cds or not idx_map:
            t["reversed_after_stop"] = None
            continue
        idx = idx_map.get(t["exit_day"])
        if idx is None:
            t["reversed_after_stop"] = None
            continue
        reversed_flag = False
        hi = min(idx + 1 + STOP_REVERSAL_LOOKFORWARD_DAYS, len(cds))
        for j in range(idx + 1, hi):
            c2 = cds[j]
            if t["side"] == +1 and c2['h'] >= t["entry"]:
                reversed_flag = True
                break
            if t["side"] == -1 and c2['l'] <= t["entry"]:
                reversed_flag = True
                break
        t["reversed_after_stop"] = reversed_flag

    return {
        "final_equity": cash,
        "trades": closed_trades,
        "equity_curve": equity_curve,
        "daily_pnl": daily_pnl,
        "n_trades": len(closed_trades),
        "n_days": len(all_days),
        "btc_blocked": btc_blocked,
        "dd_brake_days": dd_brake_days,
        "adx_filtered": adx_filtered,
        "cooldown_blocked": cooldown_blocked,
        "excluded_count": excluded_count,
        "day_stop_triggered": day_stop_triggered,
        "day_stop_events": day_stop_events,
        "partial_tp_count": partial_tp_count,
        "partial_tp_total_pnl": partial_tp_total_pnl,
        "consec_loss_days_max": consec_loss_days_max,   # v4.7-risk100-v4
    }


# =====================================================================
#  ВАЛИДАЦИЯ (4 гейта)
# =====================================================================

def validate(result, z=Z_SCORE):
    final     = result["final_equity"]
    total_pnl = final - INIT_CAPITAL

    daily_vals = [p[1] for p in result["daily_pnl"]]
    n = len(daily_vals)
    if n > 1 and statistics.pstdev(daily_vals) > 0:
        std = statistics.stdev(daily_vals)
        se  = std / math.sqrt(n)
        ci  = z * se
    else:
        ci = float("inf") if total_pnl > 0 else 0.0
    gate1 = (total_pnl - ci) > 0

    worst_day = min(daily_vals) if daily_vals else 0.0
    gate2 = worst_day >= WORST_DAY_LIMIT

    # худший день — с датой, + статистика минусовых дней (подряд)
    worst_day_ts = None
    if result["daily_pnl"]:
        worst_day_ts = min(result["daily_pnl"], key=lambda p: p[1])[0]

    losing_days_n = sum(1 for _, pnl in result["daily_pnl"] if pnl < 0)
    loss_streaks, cur_loss_streak = [], []
    for ts, pnl in sorted(result["daily_pnl"], key=lambda p: p[0]):
        if pnl < 0:
            cur_loss_streak.append((ts, pnl))
        else:
            if cur_loss_streak:
                loss_streaks.append(cur_loss_streak)
            cur_loss_streak = []
    if cur_loss_streak:
        loss_streaks.append(cur_loss_streak)
    longest_loss_streak = max((len(s) for s in loss_streaks), default=0)
    multi_loss_streaks = [s for s in loss_streaks if len(s) >= 2]

    # v4.7: суммарный убыток за серию подряд минусовых дней — максимум
    # (самая болезненная серия), а не просто длина серии
    worst_streak_loss = 0.0
    worst_streak_detail = None
    for s in loss_streaks:
        streak_total = sum(p for _, p in s)
        if streak_total < worst_streak_loss:
            worst_streak_loss = streak_total
            worst_streak_detail = {"from": s[0][0], "to": s[-1][0], "days": len(s)}

    eqs = [e for _, e in result["equity_curve"]]
    peak, max_dd = -math.inf, 0.0
    for e in eqs:
        peak = max(peak, e)
        max_dd = max(max_dd, peak - e)
    gate3 = max_dd <= MAX_DD_LIMIT

    yearly = defaultdict(float)
    for ts, pnl in result["daily_pnl"]:
        y = dt.datetime.utcfromtimestamp(ts).year
        yearly[y] += pnl
    gate4 = all(v >= YEAR_LOSS_LIMIT for v in yearly.values())

    reasons = defaultdict(int)
    for t in result["trades"]:
        reasons[t["reason"]] += 1

    # разбивка по парам — сделки / PnL / winrate + long/short на каждый инструмент
    by_pair = defaultdict(lambda: {
        "n": 0, "wins": 0, "losses": 0, "pnl": 0.0,
        "long_n": 0, "long_wins": 0, "short_n": 0, "short_wins": 0,
    })
    for t in result["trades"]:
        row = by_pair[t["contract"]]
        row["n"] += 1
        row["pnl"] += t["pnl"]
        win = t["pnl"] > 0
        if win:
            row["wins"] += 1
        else:
            row["losses"] += 1
        if t["side"] == +1:
            row["long_n"] += 1
            if win:
                row["long_wins"] += 1
        else:
            row["short_n"] += 1
            if win:
                row["short_wins"] += 1
    pair_stats = sorted(
        (
            {"pair": p, "n": r["n"], "pnl": r["pnl"],
             "winrate": (r["wins"] / r["n"] * 100) if r["n"] else 0.0,
             "wins": r["wins"], "losses": r["losses"],
             "long_n": r["long_n"], "long_wins": r["long_wins"],
             "short_n": r["short_n"], "short_wins": r["short_wins"]}
            for p, r in by_pair.items()
        ),
        key=lambda x: x["pnl"], reverse=True,
    )

    # общая статистика long/short (прибыльные/убыточные)
    all_trades = result["trades"]
    longs  = [t for t in all_trades if t["side"] == +1]
    shorts = [t for t in all_trades if t["side"] == -1]
    long_short_stats = {
        "long_n": len(longs),
        "long_wins": sum(1 for t in longs if t["pnl"] > 0),
        "long_losses": sum(1 for t in longs if t["pnl"] <= 0),
        "short_n": len(shorts),
        "short_wins": sum(1 for t in shorts if t["pnl"] > 0),
        "short_losses": sum(1 for t in shorts if t["pnl"] <= 0),
    }

    # для убыточных сделок — на сколько % они доходили в свою сторону
    # до разворота (MFE, max favorable excursion). Для сделок, где был
    # partial TP, max_favorable посчитан на всю сделку (включая остаток
    # после breakeven), это НЕ сам кусок PTP — он отдельной записью PTP.
    def _mfe_pct(t):
        return t["side"] * (t["max_favorable"] - t["entry"]) / t["entry"] * 100

    losing_longs  = [t for t in longs  if t["pnl"] <= 0]
    losing_shorts = [t for t in shorts if t["pnl"] <= 0]
    long_short_stats["long_losing_mfe_avg"]  = (
        sum(_mfe_pct(t) for t in losing_longs) / len(losing_longs) if losing_longs else 0.0)
    long_short_stats["long_losing_mfe_max"]  = (
        max((_mfe_pct(t) for t in losing_longs), default=0.0))
    long_short_stats["short_losing_mfe_avg"] = (
        sum(_mfe_pct(t) for t in losing_shorts) / len(losing_shorts) if losing_shorts else 0.0)
    long_short_stats["short_losing_mfe_max"] = (
        max((_mfe_pct(t) for t in losing_shorts), default=0.0))

    # v4.7: убыточные стоп-выходы (SL/TRAIL) — максимальная просадка от
    # входа (MAE%) за время удержания + % случаев, когда цена всё же
    # вернулась в сторону сделки в течение STOP_REVERSAL_LOOKFORWARD_DAYS
    # дней ПОСЛЕ закрытия (стоп вынес раньше времени). Отчётная статистика,
    # не влияет на торговую логику.
    def _mae_pct(t):
        return t["side"] * (t["entry"] - t["max_adverse"]) / t["entry"] * 100

    stop_losing = [
        t for t in result["trades"]
        if t["reason"] in ("SL", "TRAIL") and t["pnl"] <= 0
        and t.get("reversed_after_stop") is not None
    ]
    stop_reversed_n = sum(1 for t in stop_losing if t["reversed_after_stop"])
    stop_stats = {
        "n": len(stop_losing),
        "reversed_n": stop_reversed_n,
        "reversed_pct": (stop_reversed_n / len(stop_losing) * 100) if stop_losing else 0.0,
        "mae_avg": (sum(_mae_pct(t) for t in stop_losing) / len(stop_losing)) if stop_losing else 0.0,
        "mae_max": max((_mae_pct(t) for t in stop_losing), default=0.0),
        "lookforward_days": STOP_REVERSAL_LOOKFORWARD_DAYS,
    }

    # daily stop -400 — даты срабатывания, закрытие всех позиций,
    # и сколько раз срабатывало несколько дней ПОДРЯД
    day_stop_events = result.get("day_stop_events", [])
    streaks = []
    cur_streak = []
    for ev in sorted(day_stop_events, key=lambda e: e["day"]):
        if cur_streak and ev["day"] - cur_streak[-1]["day"] == 86400:
            cur_streak.append(ev)
        else:
            if cur_streak:
                streaks.append(cur_streak)
            cur_streak = [ev]
    if cur_streak:
        streaks.append(cur_streak)
    multi_day_streaks = [s for s in streaks if len(s) >= 2]

    return {
        "final_equity": final, "total_pnl": total_pnl, "ci_z": ci,
        "n_trades": result["n_trades"], "n_days": n,
        "gate1": gate1, "gate2": gate2, "gate3": gate3, "gate4": gate4,
        "worst_day": worst_day, "worst_day_ts": worst_day_ts, "max_dd": max_dd,
        "losing_days_n": losing_days_n,
        "longest_loss_streak": longest_loss_streak,
        "multi_loss_streaks_n": len(multi_loss_streaks),
        "multi_loss_streaks_detail": [
            {"from": s[0][0], "to": s[-1][0], "days": len(s),
             "total_loss": sum(p for _, p in s)}
            for s in multi_loss_streaks
        ],
        "worst_streak_loss": worst_streak_loss,
        "worst_streak_detail": worst_streak_detail,
        "yearly_pnl": dict(yearly),
        "reasons": dict(reasons),
        "pair_stats": pair_stats,
        "long_short_stats": long_short_stats,
        "stop_reversal_stats": stop_stats,
        "day_stop_events": day_stop_events,
        "day_stop_streaks_multi": len(multi_day_streaks),
        "day_stop_streaks_multi_detail": [
            {"from": s[0]["day"], "to": s[-1]["day"], "days": len(s)}
            for s in multi_day_streaks
        ],
        "partial_tp_count": result.get("partial_tp_count", 0),
        "partial_tp_total_pnl": result.get("partial_tp_total_pnl", 0.0),
        "btc_blocked": result.get("btc_blocked", 0),
        "dd_brake_days": result.get("dd_brake_days", 0),
        "adx_filtered": result.get("adx_filtered", 0),
        "cooldown_blocked": result.get("cooldown_blocked", 0),
        "excluded_count": result.get("excluded_count", 0),
        "day_stop_triggered": result.get("day_stop_triggered", 0),
        "consec_loss_days_max": result.get("consec_loss_days_max", 0),   # v4.7-risk100-v4
        "all_pass": gate1 and gate2 and gate3 and gate4,
    }


# =====================================================================
#  ОТЧЁТ
# =====================================================================

_PAIRS_USED = []


def format_report(result, val, n_pairs=None):
    if n_pairs is None:
        n_pairs = len(_PAIRS_USED)
    lines = []
    lines.append("📊 *bt_donchian_regime v4.7 — РЕЗУЛЬТАТЫ*")
    lines.append("")
    lines.append(f"Donchian(20) + BTC SMA(50) + DMI + Trailing 2xATR + Partial TP +{PARTIAL_TP_PCT*100:.0f}%/{PARTIAL_TP_FRACTION*100:.0f}% + Compound + Daily stop + Cooldown (NO PerSide cap, NO sideways-фильтр)")
    lines.append(f"Капитал: ${INIT_CAPITAL:,.0f}  |  Пары: {n_pairs}  |  Excluded: {val['excluded_count']}")
    lines.append(f"Risk: {RISK_FRACTION*100:.1f}% от equity (floor ${SLOT_RISK_MIN:.0f}, cap ${SLOT_RISK_MAX:.0f}, brake x{DD_BRAKE_FACTOR})")
    lines.append(f"Max concurrent: {MAX_CONCURRENT} (per-side cap ОТКЛЮЧЁН) | Daily stop: ${DAILY_STOP_LOSS:.0f} / 2-й день подряд ${DAILY_STOP_LOSS_CONSEC:.0f}")
    lines.append(f"Partial TP: +{PARTIAL_TP_PCT*100:.0f}% favorable -> закрыть {PARTIAL_TP_FRACTION*100:.0f}% позиции, остаток -> breakeven")
    lines.append(f"Сделок: {val['n_trades']}  |  Дней: {val['n_days']}")
    lines.append(f"BTC blocked: {val['btc_blocked']}  |  ADX filtered: {val['adx_filtered']}  |  DD brake days: {val['dd_brake_days']}")
    lines.append(f"Cooldown: {val['cooldown_blocked']}  |  Daily stop: {val['day_stop_triggered']}  |  "
                 f"Partial TPs: {val['partial_tp_count']} (${val['partial_tp_total_pnl']:+,.0f})  |  "
                 f"Max consec loss days: {val.get('consec_loss_days_max', 0)}")
    if val.get("reasons"):
        r = val["reasons"]
        lines.append(f"Исходы: SL/TRAIL={r.get('TRAIL',0)+r.get('SL',0)} "
                     f"PTP={r.get('PTP',0)} TIME={r.get('TIME',0)} SIG={r.get('SIG',0)} END={r.get('END',0)}")
    lines.append("")
    lines.append(f"Final equity : ${val['final_equity']:,.2f}")
    lines.append(f"Total P&L    : ${val['total_pnl']:,.2f}")
    lines.append(f"CI(Z={Z_SCORE}): ${val['ci_z']:,.2f}")
    lines.append("")
    lines.append("— ВАЛИДАЦИЯ —")
    lines.append(f"① Final − CI > 0     : {'✅ PASS' if val['gate1'] else '❌ FAIL'}"
                 f"  (edge = ${val['total_pnl']-val['ci_z']:,.2f})")
    worst_day_date = (dt.datetime.utcfromtimestamp(val["worst_day_ts"]).strftime("%Y-%m-%d")
                      if val.get("worst_day_ts") else "—")
    lines.append(f"② Worst day ≥ −$500   : {'✅ PASS' if val['gate2'] else '❌ FAIL'}"
                 f"  (worst = ${val['worst_day']:,.2f}, {worst_day_date})")
    lines.append(f"③ MaxDD ≤ $2,000      : {'✅ PASS' if val['gate3'] else '❌ FAIL'}"
                 f"  (MaxDD = ${val['max_dd']:,.2f})")
    lines.append(f"④ No year < −$500     : {'✅ PASS' if val['gate4'] else '❌ FAIL'}")
    for y in sorted(val["yearly_pnl"]):
        lines.append(f"   {y}: ${val['yearly_pnl'][y]:,.2f}")
    lines.append("")
    verdict = "✅✅✅✅ ALL PASS" if val["all_pass"] else "❌ НЕ ПРОШЁЛ"
    lines.append(f"ИТОГ: {verdict}")

    # Long/Short — сколько всего, прибыльных/убыточных, и сколько %
    # убыточные сделки доходили в свою сторону до разворота (MFE%)
    ls = val.get("long_short_stats")
    if ls:
        lines.append("")
        lines.append("— LONG / SHORT —")
        lines.append(f"Long : {ls['long_n']} сделок  (🟢 {ls['long_wins']} / 🔴 {ls['long_losses']})")
        lines.append(f"Short: {ls['short_n']} сделок  (🟢 {ls['short_wins']} / 🔴 {ls['short_losses']})")
        lines.append(
            f"Убыточные Long  — доходили в свою сторону в среднем на "
            f"{ls['long_losing_mfe_avg']:.2f}% (макс {ls['long_losing_mfe_max']:.2f}%)"
        )
        lines.append(
            f"Убыточные Short — доходили в свою сторону в среднем на "
            f"{ls['short_losing_mfe_avg']:.2f}% (макс {ls['short_losing_mfe_max']:.2f}%)"
        )

    # v4.7: убыточные стоп-выходы (SL/TRAIL) — просадка от входа (MAE%)
    # + % случаев, когда цена вернулась в сторону сделки после стопа
    ss = val.get("stop_reversal_stats")
    if ss and ss["n"]:
        lines.append("")
        lines.append("— СТОП-ВЫХОДЫ (SL/TRAIL), убыточные —")
        lines.append(f"Всего: {ss['n']}  |  вернулись в сторону сделки "
                     f"в течение {ss['lookforward_days']}д после стопа: "
                     f"{ss['reversed_n']} ({ss['reversed_pct']:.0f}%)")
        lines.append(f"Просадка от входа (MAE%): в среднем {ss['mae_avg']:.2f}%  "
                     f"(макс {ss['mae_max']:.2f}%)")

    # Daily stop -400 — даты срабатывания, закрылись ли все позиции,
    # и сколько раз это было несколько дней подряд
    events = val.get("day_stop_events") or []
    if events:
        lines.append("")
        lines.append("— DAILY STOP -$400 —")
        lines.append(f"Сработал: {val['day_stop_triggered']} раз(а)  |  "
                     f"подряд (2+ дня): {val.get('day_stop_streaks_multi', 0)} раз(а)")
        for det in (val.get("day_stop_streaks_multi_detail") or []):
            d_from = dt.datetime.utcfromtimestamp(det["from"]).strftime("%Y-%m-%d")
            d_to   = dt.datetime.utcfromtimestamp(det["to"]).strftime("%Y-%m-%d")
            lines.append(f"   подряд {det['days']}д: {d_from} → {d_to}")
        for ev in events[:30]:
            d = dt.datetime.utcfromtimestamp(ev["day"]).strftime("%Y-%m-%d")
            closed_mark = "все позиции закрылись" if ev["all_closed"] else f"осталось открыто {ev['open_left']}"
            lines.append(f"   {d}: открыто было {ev['open_before']}, {closed_mark}")
        if len(events) > 30:
            lines.append(f"   ... и ещё {len(events)-30} срабатываний")

    # максимальная просадка в день + минусовые дни (подряд или нет)
    lines.append("")
    lines.append("— МИНУСОВЫЕ ДНИ —")
    lines.append(f"Макс. просадка за день: ${val['worst_day']:,.2f} ({worst_day_date})")
    lines.append(f"Всего дней в минусе: {val.get('losing_days_n', 0)} из {val['n_days']}")
    lines.append(f"Самая длинная серия подряд: {val.get('longest_loss_streak', 0)} дн.  |  "
                 f"серий из 2+ дней подряд: {val.get('multi_loss_streaks_n', 0)}")
    wsd = val.get("worst_streak_detail")
    if wsd:
        d_from = dt.datetime.utcfromtimestamp(wsd["from"]).strftime("%Y-%m-%d")
        d_to   = dt.datetime.utcfromtimestamp(wsd["to"]).strftime("%Y-%m-%d")
        lines.append(f"Макс. суммарный убыток за серию подряд: ${val.get('worst_streak_loss', 0.0):,.2f}  "
                     f"({wsd['days']}д: {d_from} → {d_to})")
    for det in (val.get("multi_loss_streaks_detail") or [])[:15]:
        d_from = dt.datetime.utcfromtimestamp(det["from"]).strftime("%Y-%m-%d")
        d_to   = dt.datetime.utcfromtimestamp(det["to"]).strftime("%Y-%m-%d")
        lines.append(f"   подряд {det['days']}д: {d_from} → {d_to}  (убыток за серию: "
                     f"${det.get('total_loss', 0.0):,.2f})")

    # разбивка по парам (сделки / PnL / winrate + long/short), сортировка по PnL
    pair_stats = val.get("pair_stats") or []
    if pair_stats:
        lines.append("")
        lines.append("— ПО ПАРАМ —")
        for ps in pair_stats:
            mark = "🟢" if ps["pnl"] > 0 else ("🔴" if ps["pnl"] < 0 else "⚪")
            lines.append(
                f"{mark} {ps['pair']}: {ps['n']} сделок (🟢{ps['wins']}/🔴{ps['losses']}), "
                f"PnL ${ps['pnl']:,.2f}, winrate {ps['winrate']:.0f}%, "
                f"L={ps['long_n']}(🟢{ps['long_wins']}) S={ps['short_n']}(🟢{ps['short_wins']})"
            )
    return lines


# =====================================================================
#  ТОЧКА ВХОДА
# =====================================================================

def main():
    if B is None:
        print(f"[ERROR] bot.py недоступен: {_BOT_IMPORT_ERR}")
        print("Запускайте через диспетчер: RUN_BACKTEST=donchian_regime_v47 python bot.py")
        sys.exit(1)

    global _PAIRS_USED
    pairs = list(B.UPSCALE_PAIRS)
    _PAIRS_USED = pairs

    start = os.environ.get("BT_START", BACKTEST_START_ISO)
    end   = os.environ.get("BT_END", BACKTEST_END_ISO)
    B.send_telegram(
        f"🚀 *bt_donchian_regime v4.7* старт: {len(pairs)} пар, "
        f"окно {start} → {end or 'сегодня'}"
    )

    result = run_backtest(pairs, start_iso=start, end_iso=end, verbose=True)
    val    = validate(result)

    lines = format_report(result, val, n_pairs=len(pairs))
    B.send_blocks(lines)

    try:
        import json
        out_dir = "/home/z/my-project/download"
        os.makedirs(out_dir, exist_ok=True)
        with open(f"{out_dir}/bt_donchian_regime_v47_result.json", "w") as f:
            json.dump({
                "validation": {k: (v if not isinstance(v, bool) else int(v))
                               for k, v in val.items()},
                "trades": result["trades"][:200],
                "equity_curve_tail": result["equity_curve"][-60:],
            }, f, indent=2, default=str)
    except Exception as e:
        print(f"[warn] не удалось сохранить результат: {e}")

    return 0 if val["all_pass"] else 2


if __name__ == "__main__":
    sys.exit(main())
