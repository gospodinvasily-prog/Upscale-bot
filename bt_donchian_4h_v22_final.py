# -*- coding: utf-8 -*-
"""
bt_donchian_4h_v22_soft.py
================================================================================
Donchian 4H v2.2 — SOFT EXCLUDE TEST (динамическое исключение пар)
================================================================================

ЧТО ПРОВЕРЯЕМ:
  - ВСЕ 103 пары участвуют (статичный EXCLUDE_PAIRS пустой — пусть dynamic
    сам отсеет плохих)
  - Rolling history: последние 5 сделок по каждой паре
  - SOFT EXCLUDE: если 3+ из 5 последних сделок в минусе → 30 дней кулдаун
    (поверх существующего 14-дневного кулдауна после 4 убытков подряд)
  - Сброс rolling_history после срабатывания (после 30 дней начинаем заново)

ОЖИДАЕМ:
  - AVAX/LINEA/INJ (худшие в финальном бэктесте) автоматически уйдут в кулдаун
  - Total P&L вырастет на 5-10% (от -$2,000 убытка избавимся)
  - Worst day улучшится (плохие пары не войдут в плохой день)
  - Winrate вырастет (хвосты обрезаны)

СРАВНИВАЕМ С ФИНАЛОМ:
  Финал:    P&L +$25,063 | PF 1.79 | MaxDD $1,739 | Worst day -$830 | Winrate 61.6%
  Soft:     P&L ???       | PF ???  | MaxDD ???    | Worst day ???    | Winrate ???

ВСЁ ОСТАЛЬНОЕ ИДЕНТИЧНО ФИНАЛУ:
  - Donchian(20) на 4H + BTC SMA(50) 1D + DMI + ATR
  - Trailing 4.5×ATR по close свечи
  - Partial TP +8% favourable → 50% позиции, остаток в breakeven
  - Daily Stop today-only -$350 (закрывает только сегодня-открытые убыточные)
  - DD brake ×0.4 при DD>$900 (восстановление 85%)
  - Cooldown 14 дней после 4 убытков подряд (как было)
  - MAX_CONCURRENT=10, MAX_PER_SIDE_CAP=6, MAX_LOSERS_PER_SIDE=3
  - Compound sizing: max($100, min($250, equity × 0.8%))

ЗАПУСК:
    RUN_BACKTEST=donchian_4h_v22_soft python bot.py
================================================================================
"""

import os, sys, math, statistics, datetime as dt
from collections import defaultdict

try:
    import bot as B
except Exception as e:
    B = None
    _BOT_IMPORT_ERR = e
else:
    _BOT_IMPORT_ERR = None

# ─── ИДЕНТИФИКАЦИЯ ────────────────────────────────────────────────────────────
STRATEGY_NAME    = "Donchian 4H"
STRATEGY_VERSION = "v2.2-SOFT-TEST"
STRATEGY_FILE    = "bt_donchian_4h_v22_soft"

# ─── КАПИТАЛ И РИСК ───────────────────────────────────────────────────────────
INIT_CAPITAL      = 10_000.0
RISK_FRACTION     = 0.008
SLOT_RISK_MIN     = 100.0
SLOT_RISK_MAX     = 250.0
MAX_POSITION_PCT  = 0.20

# ─── DD BRAKE ──────────────────────────────────────────────────────────────────
DD_BRAKE_THRESHOLD = 900.0
DD_BRAKE_FACTOR     = 0.4
DD_BRAKE_RECOVERY   = 0.85

# ─── ЛИМИТЫ ────────────────────────────────────────────────────────────────────
MAX_CONCURRENT      = 10
MAX_PER_SIDE_CAP    = 6
PER_SIDE_BUDGET     = 2000
MAX_NEW_PER_DAY     = 10
MAX_LOSERS_PER_SIDE = 3

# ─── DAILY STOP (today-only) ───────────────────────────────────────────────────
DAILY_STOP_LOSS       = -350.0
DAILY_STOP_LOSS_CONSEC = -350.0

# ─── НОВОЕ: SOFT EXCLUDE (динамическое исключение пар) ────────────────────────
SOFT_EXCLUDE_LOOKBACK   = 5     # смотрим последние N сделок
SOFT_EXCLUDE_MIN_LOSERS = 3     # если 3+ из 5 в минусе → кулдаун
SOFT_EXCLUDE_DAYS       = 30    # длительность кулдауна (дней)

# ─── EXCLUDE_PAIRS — ПУСТОЙ! Все 103 пары тестируются ─────────────────────────
EXCLUDE_PAIRS = set()   # ← КЛЮЧЕВОЕ ИЗМЕНЕНИЕ: ничего не исключаем статично

# ─── COOLDOWN (статичный, как был) ────────────────────────────────────────────
CONSEC_LOSS_LIMIT = 4
COOLDOWN_DAYS     = 14

# ─── ИНДИКАТОРЫ ────────────────────────────────────────────────────────────────
CANDLE_INTERVAL = "4h"
DONCHIAN_PERIOD = 20
BTC_REGIME_SMA  = 50
DMI_PERIOD      = 14
ADX_THRESHOLD   = 20.0
ATR_PERIOD      = 14
ATR_PCT_MIN     = 0.006
ATR_PCT_MAX     = 0.020
ATR_STOP_MULT   = 4.5

# ─── ВЫХОД ─────────────────────────────────────────────────────────────────────
MAX_HOLD_DAYS = 35
PARTIAL_TP_PCT      = 0.08
PARTIAL_TP_FRACTION = 0.50
STOP_REVERSAL_LOOKFORWARD_DAYS = 15

# ─── КОМИССИИ ──────────────────────────────────────────────────────────────────
COMM_TAKER        = 0.0005
SLIPPAGE          = 0.0002
FUNDING_TIMES_UTC = (0, 8, 16)

# ─── ВАЛИДАЦИЯ ─────────────────────────────────────────────────────────────────
Z_SCORE         = 2.64
WORST_DAY_LIMIT = -500.0
MAX_DD_LIMIT    = 2_000.0
YEAR_LOSS_LIMIT = -500.0

BTC_CONTRACT      = "BTC_USDT"
BACKTEST_START_ISO = "2023-01-01"
BACKTEST_END_ISO    = ""


# ═════════════════════════════════════════════════════════════════════════════
# ИНДИКАТОРЫ
# ═════════════════════════════════════════════════════════════════════════════

def sma(values, period):
    if len(values) < period: return None
    return sum(values[-period:]) / period

def atr_daily(candles, period=ATR_PERIOD):
    if len(candles) < period + 1: return None
    trs = []
    for i in range(-period, 0):
        c, prev = candles[i], candles[i - 1]
        trs.append(max(c['h'] - c['l'], abs(c['h'] - prev['c']), abs(c['l'] - prev['c'])))
    return sum(trs) / period

def donchian(candles, period=DONCHIAN_PERIOD):
    if len(candles) < period + 1: return None
    window = candles[-(period + 1):-1]
    return max(c['h'] for c in window), min(c['l'] for c in window)

def dmi(candles, period=DMI_PERIOD):
    if len(candles) < period * 2 + 1: return None, None, None
    plus_dm, minus_dm, trs = [], [], []
    for i in range(-period * 2, 0):
        c, prev = candles[i], candles[i - 1]
        up = c['h'] - prev['h']
        down = prev['l'] - c['l']
        plus_dm.append(up if (up > down and up > 0) else 0)
        minus_dm.append(down if (down > up and down > 0) else 0)
        trs.append(max(c['h'] - c['l'], abs(c['h'] - prev['c']), abs(c['l'] - prev['c'])))
    if len(trs) < period or sum(trs[-period:]) == 0: return None, None, None
    atr_v = sum(trs[-period:]) / period
    plus_d = sum(plus_dm[-period:]) / period
    minus_d = sum(minus_dm[-period:]) / period
    if atr_v <= 0: return None, None, None
    plus_di = 100 * plus_d / atr_v
    minus_di = 100 * minus_d / atr_v
    dx_values = []
    for j in range(period, period * 2 + 1):
        sub_tr = trs[j - period:j]
        sub_plus = plus_dm[j - period:j]
        sub_minus = minus_dm[j - period:j]
        if sum(sub_tr) == 0: continue
        a = sum(sub_tr) / period
        if a <= 0: continue
        pdi = 100 * sum(sub_plus) / period / a
        mdi = 100 * sum(sub_minus) / period / a
        if pdi + mdi > 0:
            dx_values.append(100 * abs(pdi - mdi) / (pdi + mdi))
    adx = sum(dx_values) / len(dx_values) if dx_values else 0
    return plus_di, minus_di, adx


# ═════════════════════════════════════════════════════════════════════════════
# ДАННЫЕ
# ═════════════════════════════════════════════════════════════════════════════

_CANDLE_CACHE = {}

def fetch_candles(contract, interval="1d", limit=2000):
    key = (contract, interval, limit)
    if key in _CANDLE_CACHE: return _CANDLE_CACHE[key]
    gate_c = contract if contract.endswith("_USDT") else f"{contract}_USDT"
    pages_needed = 3 if interval == "4h" else 1
    all_candles = []
    to_ts = None
    for page in range(pages_needed):
        params = {"contract": gate_c, "interval": interval, "limit": limit}
        if to_ts is not None: params["to"] = to_ts
        try:
            raw = B.api_get("candlesticks", params)
            page_candles = B.parse_candles(raw)
        except Exception as e:
            print(f"[warn] {contract} page {page+1}: {e}")
            break
        if not page_candles: break
        page_candles.sort(key=lambda c: c['t'])
        if to_ts is not None:
            page_candles = [c for c in page_candles if c['t'] < to_ts]
        if not page_candles: break
        all_candles = page_candles + all_candles if all_candles else page_candles
        to_ts = page_candles[0]['t']
        if len(page_candles) < limit: break
    seen, unique = set(), []
    for c in all_candles:
        if c['t'] not in seen:
            seen.add(c['t'])
            unique.append(c)
    unique.sort(key=lambda c: c['t'])
    _CANDLE_CACHE[key] = unique
    return unique


_BTC_REGIME_CACHE = None

def compute_btc_regime(btc_candles):
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


_FUNDING_CACHE = None

def get_funding_snapshot():
    global _FUNDING_CACHE
    if _FUNDING_CACHE is None:
        try:
            _FUNDING_CACHE = {t["contract"]: float(t.get("funding_rate", 0))
                              for t in B.api_get("tickers", {}) if t.get("contract")}
        except Exception:
            _FUNDING_CACHE = {}
    return _FUNDING_CACHE


# ═════════════════════════════════════════════════════════════════════════════
# СИГНАЛ
# ═════════════════════════════════════════════════════════════════════════════

def evaluate_signal(candles_up_to_today, btc_regime_today, funding_snap):
    cds = candles_up_to_today
    if len(cds) < DONCHIAN_PERIOD + 2: return None
    last = cds[-1]
    dc = donchian(cds, DONCHIAN_PERIOD)
    if dc is None: return None
    dc_high, dc_low = dc
    a = atr_daily(cds[:-1])
    if a is None or a <= 0: return None
    atr_pct = a / last['c']
    if not (ATR_PCT_MIN <= atr_pct <= ATR_PCT_MAX):
        return {"side": 0, "atr": a, "close": last['c'], "atr_pct": atr_pct,
                "dc_high": dc_high, "dc_low": dc_low, "plus_di": 0, "minus_di": 0, "adx": 0}
    plus_di, minus_di, adx = dmi(cds[:-1])
    if plus_di is None:
        return {"side": 0, "atr": a, "close": last['c'], "atr_pct": atr_pct,
                "dc_high": dc_high, "dc_low": dc_low, "plus_di": 0, "minus_di": 0, "adx": 0}
    funding = funding_snap.get(last.get('contract', ''), 0)
    long_ok  = (last['c'] > dc_high and btc_regime_today == +1 and plus_di > minus_di and abs(funding) <= 0.0005)
    short_ok = (last['c'] < dc_low  and btc_regime_today == -1 and minus_di > plus_di and abs(funding) <= 0.0005)
    side = +1 if long_ok else (-1 if short_ok else 0)
    return {"side": side, "atr": a, "close": last['c'], "atr_pct": atr_pct,
            "dc_high": dc_high, "dc_low": dc_low,
            "plus_di": plus_di, "minus_di": minus_di, "adx": adx}


# ═════════════════════════════════════════════════════════════════════════════
# ПОЗИЦИЯ
# ═════════════════════════════════════════════════════════════════════════════

class Position:
    __slots__ = ("contract", "side", "entry", "atr_at_entry", "size_usd",
                 "original_size_usd", "initial_stop", "trail_stop",
                 "max_favorable", "max_adverse", "entry_idx",
                 "entry_day_ts", "hold_days", "donchian_at_entry", "partial_taken")

    def __init__(self, contract, side, entry, atr_at_entry, size_usd,
                 entry_idx, entry_day_ts, donchian_at_entry):
        self.contract = contract
        self.side = side
        self.entry = entry
        self.atr_at_entry = atr_at_entry
        self.size_usd = size_usd
        self.original_size_usd = size_usd
        self.initial_stop = entry - side * ATR_STOP_MULT * atr_at_entry
        self.trail_stop = self.initial_stop
        self.max_favorable = entry
        self.max_adverse = entry
        self.entry_idx = entry_idx
        self.entry_day_ts = entry_day_ts
        self.hold_days = 0
        self.donchian_at_entry = donchian_at_entry
        self.partial_taken = False

    def update_trail(self, candle):
        if self.side == +1:
            self.max_favorable = max(self.max_favorable, candle['h'])
            self.max_adverse = min(self.max_adverse, candle['l'])
            new_stop = candle['c'] - ATR_STOP_MULT * self.atr_at_entry
            self.trail_stop = max(self.trail_stop, new_stop)
        else:
            self.max_favorable = min(self.max_favorable, candle['l'])
            self.max_adverse = max(self.max_adverse, candle['h'])
            new_stop = candle['c'] + ATR_STOP_MULT * self.atr_at_entry
            self.trail_stop = min(self.trail_stop, new_stop)

    def check_partial_tp(self, candle):
        if self.partial_taken: return None
        if self.side == +1:
            pct_favorable = (self.max_favorable - self.entry) / self.entry
        else:
            pct_favorable = (self.entry - self.max_favorable) / self.entry
        if pct_favorable < PARTIAL_TP_PCT: return None
        exit_price = candle['c']
        partial_size = self.size_usd * PARTIAL_TP_FRACTION
        partial_pnl = self.side * (exit_price - self.entry) / self.entry * partial_size
        self.size_usd -= partial_size
        self.partial_taken = True
        if self.side == +1:
            self.trail_stop = max(self.trail_stop, self.entry)
        else:
            self.trail_stop = min(self.trail_stop, self.entry)
        return {"partial_size": partial_size, "partial_pnl": partial_pnl,
                "partial_price": exit_price, "favorable_pct": pct_favorable}


# ═════════════════════════════════════════════════════════════════════════════
# РАЗМЕР ПОЗИЦИИ
# ═════════════════════════════════════════════════════════════════════════════

def compute_risk_slot(equity, dd_brake_active=False):
    base = max(SLOT_RISK_MIN, min(SLOT_RISK_MAX, equity * RISK_FRACTION))
    if dd_brake_active: base *= DD_BRAKE_FACTOR
    return base

def compute_max_per_side(current_risk):
    if current_risk <= 0: return 0
    return min(MAX_PER_SIDE_CAP, int(PER_SIDE_BUDGET / current_risk))


# ═════════════════════════════════════════════════════════════════════════════
# ОСНОВНОЙ БЭКТЕСТ (с soft exclude)
# ═════════════════════════════════════════════════════════════════════════════

def run_backtest(pairs, start_iso=BACKTEST_START_ISO, end_iso=BACKTEST_END_ISO, verbose=True):
    if B is None: raise RuntimeError("bot module not available")

    # EXCLUDE_PAIRS пустой — все 103 пары тестируются
    pairs_active = [p for p in pairs if p not in EXCLUDE_PAIRS]
    excluded = len(pairs) - len(pairs_active)

    if verbose:
        B.send_telegram(
            f"📡 {STRATEGY_NAME} {STRATEGY_VERSION}: загружаю {CANDLE_INTERVAL} свечи "
            f"для {len(pairs_active)} пар (SOFT EXCLUDE ВКЛЮЧЁН, static EXCLUDE пустой)"
        )

    data = {}
    for i, p in enumerate(pairs_active):
        try:
            cds = fetch_candles(p, CANDLE_INTERVAL, 2000)
            if cds:
                data[p] = cds
                if verbose and i == 0:
                    B.send_telegram(
                        f"  пример {p}: {len(cds)} свечей "
                        f"(от {dt.datetime.utcfromtimestamp(cds[0]['t']).strftime('%Y-%m-%d')} "
                        f"до {dt.datetime.utcfromtimestamp(cds[-1]['t']).strftime('%Y-%m-%d')})"
                    )
        except Exception as e:
            print(f"[warn] {p}: {e}")
        if verbose and (i + 1) % 20 == 0:
            B.send_telegram(f"  загружено {i+1}/{len(pairs_active)}")
    if not data: raise RuntimeError("Нет данных")

    if verbose:
        B.send_telegram(f"📡 {STRATEGY_NAME} {STRATEGY_VERSION}: вычисляю BTC regime...")

    btc_regime = get_btc_regime()
    funding_snap = get_funding_snapshot()

    start_ts = int(dt.datetime.fromisoformat(start_iso).timestamp())
    end_ts = int(dt.datetime.fromisoformat(end_iso).timestamp()) if end_iso else None
    all_candles = sorted({c['t'] for p in data for c in data[p]
                          if c['t'] >= start_ts and (end_ts is None or c['t'] < end_ts)})

    min_periods = max(DONCHIAN_PERIOD, DMI_PERIOD * 2, ATR_PERIOD, BTC_REGIME_SMA) + 5
    if len(all_candles) < min_periods:
        raise RuntimeError(f"Слишком мало свечей: {len(all_candles)}")

    by_pair_candle = {p: {c['t']: c for c in cds} for p, cds in data.items()}
    idx_by_pair_candle = {p: {c['t']: i for i, c in enumerate(cds)} for p, cds in data.items()}

    # --- Состояние ---
    cash = INIT_CAPITAL
    positions = []
    closed_trades = []
    equity_curve = []
    daily_pnl = []
    peak_equity = INIT_CAPITAL
    dd_brake_active = False
    dd_brake_days_set = set()
    btc_blocked_days_set = set()
    adx_filtered = 0
    # НОВОЕ: pair_stats с rolling_history для soft exclude
    pair_stats = defaultdict(lambda: {
        "consec_losses": 0,
        "cooldown_until": 0,
        "recent_pnls": [],          # последние 5 PnL (для soft exclude)
        "soft_blocks": 0,           # сколько раз блокировался soft exclude
        "total_trades": 0,
        "total_wins": 0,
        "total_pnl": 0.0,
    })
    cooldown_blocked = 0
    soft_blocked = 0  # НОВОЕ: счётчик блокировок soft exclude
    excluded_count = excluded
    day_loss_stop_active = False
    day_stop_triggered = 0
    day_stop_events = []
    prev_day_pnl = 0.0
    consec_loss_days_count = 0
    consec_loss_days_max = 0
    partial_tp_count = 0
    partial_tp_total_pnl = 0.0
    max_dd_peak_ts = None
    max_dd_trough_ts = None
    max_dd_value = 0.0
    current_peak_ts = None
    prev_cal_day = None
    prev_day_start_equity = INIT_CAPITAL
    equity = INIT_CAPITAL
    new_today = 0
    realized_today = 0.0
    today_entry_realized = 0.0

    for candle_ts in all_candles:
        cal_day = candle_ts - (candle_ts % 86400)
        btc_r = btc_regime.get(cal_day, 0)
        if btc_r == 0: btc_blocked_days_set.add(cal_day)

        if cal_day != prev_cal_day:
            if prev_cal_day is not None:
                day_pnl = equity - prev_day_start_equity
                daily_pnl.append((prev_cal_day, day_pnl))
                prev_day_pnl = day_pnl
                if prev_day_pnl < 0:
                    consec_loss_days_count += 1
                    if consec_loss_days_count > consec_loss_days_max:
                        consec_loss_days_max = consec_loss_days_count
                else:
                    consec_loss_days_count = 0
            prev_cal_day = cal_day
            prev_day_start_equity = equity
            day_loss_stop_active = False
            new_today = 0
            realized_today = 0.0
            today_entry_realized = 0.0

        positions_before = len(positions)
        new_positions = []
        realized_this_candle = 0.0

        # 1) Обновление и выходы открытых позиций
        for pos in positions:
            candle = by_pair_candle[pos.contract].get(candle_ts)
            if candle is None:
                new_positions.append(pos)
                continue
            pos.hold_days += 1
            pos.update_trail(candle)

            ptp = pos.check_partial_tp(candle)
            if ptp is not None:
                partial_tp_count += 1
                partial_tp_total_pnl += ptp["partial_pnl"]
                realized_this_candle += ptp["partial_pnl"]
                closed_trades.append({"contract": pos.contract, "side": pos.side, "entry": pos.entry,
                    "exit": ptp["partial_price"], "size_usd": ptp["partial_size"],
                    "pnl": ptp["partial_pnl"], "reason": "PTP",
                    "hold_days": pos.hold_days, "entry_day": pos.entry_day_ts,
                    "exit_day": candle_ts,
                    "max_favorable": pos.max_favorable, "max_adverse": pos.max_adverse})

            exit_price, exit_reason = None, None
            if pos.side == +1 and candle['c'] <= pos.trail_stop:
                exit_price, exit_reason = pos.trail_stop, "TRAIL"
            elif pos.side == -1 and candle['c'] >= pos.trail_stop:
                exit_price, exit_reason = pos.trail_stop, "TRAIL"
            elif pos.side == +1 and candle['c'] <= pos.initial_stop and pos.hold_days == 1:
                exit_price, exit_reason = pos.initial_stop, "SL"
            elif pos.side == -1 and candle['c'] >= pos.initial_stop and pos.hold_days == 1:
                exit_price, exit_reason = pos.initial_stop, "SL"
            if exit_price is None and pos.hold_days >= MAX_HOLD_DAYS:
                exit_price, exit_reason = candle['c'], "TIME"
            if exit_price is None:
                idx = idx_by_pair_candle[pos.contract].get(candle_ts)
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
                comm = (COMM_TAKER + SLIPPAGE) * pos.size_usd * 2
                n_fund = 0
                cur = pos.entry_day_ts
                while cur < candle_ts:
                    if dt.datetime.utcfromtimestamp(cur).hour in FUNDING_TIMES_UTC: n_fund += 1
                    cur += 3600
                _fc = pos.contract if pos.contract.endswith("_USDT") else f"{pos.contract}_USDT"
                funding_rate = funding_snap.get(_fc, 0.0)
                funding_cost = pos.side * funding_rate * pos.size_usd * n_fund
                net = gross - comm - funding_cost
                realized_this_candle += net

                if pos.entry_day_ts >= cal_day:
                    today_entry_realized += net

                closed_trades.append({"contract": pos.contract, "side": pos.side, "entry": pos.entry,
                    "exit": exit_price, "size_usd": pos.size_usd, "pnl": net, "reason": exit_reason,
                    "hold_days": pos.hold_days, "entry_day": pos.entry_day_ts, "exit_day": candle_ts,
                    "max_favorable": pos.max_favorable, "max_adverse": pos.max_adverse})

                # ── НОВОЕ: обновляем pair_stats с rolling_history для SOFT EXCLUDE ──
                ps = pair_stats[pos.contract]
                ps["total_trades"] += 1
                ps["total_pnl"] += net
                if net > 0: ps["total_wins"] += 1

                if net < 0:
                    ps["consec_losses"] += 1
                    if ps["consec_losses"] >= CONSEC_LOSS_LIMIT:
                        ps["cooldown_until"] = candle_ts + COOLDOWN_DAYS * 86400
                else:
                    ps["consec_losses"] = 0

                # НОВОЕ: rolling history для SOFT EXCLUDE
                ps["recent_pnls"].append(net)
                if len(ps["recent_pnls"]) > SOFT_EXCLUDE_LOOKBACK:
                    ps["recent_pnls"] = ps["recent_pnls"][-SOFT_EXCLUDE_LOOKBACK:]

                # Проверяем SOFT EXCLUDE: 3+ из 5 последних в минусе → 30 дней кулдаун
                if (len(ps["recent_pnls"]) >= SOFT_EXCLUDE_LOOKBACK
                        and sum(1 for p in ps["recent_pnls"] if p < 0) >= SOFT_EXCLUDE_MIN_LOSERS):
                    soft_until = candle_ts + SOFT_EXCLUDE_DAYS * 86400
                    # Берём максимум — если уже есть cooldown, продлеваем
                    ps["cooldown_until"] = max(ps["cooldown_until"], soft_until)
                    ps["soft_blocks"] += 1
                    ps["recent_pnls"] = []   # сбрасываем, после 30 дней начнём заново
                    soft_blocked += 1
            else:
                new_positions.append(pos)

        positions = new_positions
        realized_today += realized_this_candle

        # 2) Equity / peak / MaxDD / DD brake
        unrealized = 0.0
        for pos in positions:
            c = by_pair_candle[pos.contract].get(candle_ts)
            if c is None: continue
            unrealized += pos.side * (c['c'] - pos.entry) / pos.entry * pos.size_usd
        equity = cash + realized_this_candle + unrealized
        cash += realized_this_candle
        equity_curve.append((candle_ts, equity))

        if equity > peak_equity:
            peak_equity = equity
            current_peak_ts = candle_ts
        current_dd = peak_equity - equity
        if current_dd > max_dd_value:
            max_dd_value = current_dd
            max_dd_peak_ts = current_peak_ts
            max_dd_trough_ts = candle_ts

        if dd_brake_active and equity >= peak_equity * DD_BRAKE_RECOVERY:
            dd_brake_active = False
        elif (not dd_brake_active) and (peak_equity - equity) > DD_BRAKE_THRESHOLD:
            dd_brake_active = True
            dd_brake_days_set.add(cal_day)
        elif dd_brake_active:
            dd_brake_days_set.add(cal_day)

        if btc_r == 0: continue
        if len(positions) >= MAX_CONCURRENT: continue
        if day_loss_stop_active: continue

        # 3) Риск-параметры
        current_risk = compute_risk_slot(equity, dd_brake_active)
        max_per_side = compute_max_per_side(current_risk)
        long_count = sum(1 for p in positions if p.side == +1)
        short_count = sum(1 for p in positions if p.side == -1)
        current_threshold = DAILY_STOP_LOSS_CONSEC if prev_day_pnl < 0 else DAILY_STOP_LOSS

        # 4) Daily Stop (today-only)
        long_unrealized = 0.0
        short_unrealized = 0.0
        today_open_losses = 0.0
        for pos in positions:
            c = by_pair_candle[pos.contract].get(candle_ts)
            if c is None: continue
            pos_unrealized = pos.side * (c['c'] - pos.entry) / pos.entry * pos.size_usd
            if pos.side == +1: long_unrealized += pos_unrealized
            else: short_unrealized += pos_unrealized
            if pos.entry_day_ts >= cal_day and pos_unrealized < 0:
                today_open_losses += pos_unrealized

        if (today_entry_realized + today_open_losses) <= current_threshold:
            if not day_loss_stop_active:
                day_loss_stop_active = True
                day_stop_triggered += 1
                positions_remaining = []
                dstop_realized = 0.0
                for pos in positions:
                    candle_now = by_pair_candle[pos.contract].get(candle_ts)
                    if candle_now is None:
                        positions_remaining.append(pos)
                        continue
                    pos_unrealized = pos.side * (candle_now['c'] - pos.entry) / pos.entry * pos.size_usd
                    if pos.entry_day_ts >= cal_day and pos_unrealized < 0:
                        exit_price_now = candle_now['c']
                        gross_now = pos.side * (exit_price_now - pos.entry) / pos.entry * pos.size_usd
                        comm_now = (COMM_TAKER + SLIPPAGE) * pos.size_usd * 2
                        n_fund_now = 0
                        cur_now = pos.entry_day_ts
                        while cur_now < candle_ts:
                            if dt.datetime.utcfromtimestamp(cur_now).hour in FUNDING_TIMES_UTC: n_fund_now += 1
                            cur_now += 3600
                        _fc_now = pos.contract if pos.contract.endswith("_USDT") else f"{pos.contract}_USDT"
                        funding_rate_now = funding_snap.get(_fc_now, 0.0)
                        funding_cost_now = pos.side * funding_rate_now * pos.size_usd * n_fund_now
                        net_now = gross_now - comm_now - funding_cost_now
                        realized_this_candle += net_now
                        dstop_realized += net_now
                        today_entry_realized += net_now

                        # НОВОЕ: обновляем pair_stats для DSTOP закрытий тоже
                        ps_d = pair_stats[pos.contract]
                        ps_d["total_trades"] += 1
                        ps_d["total_pnl"] += net_now
                        if net_now > 0: ps_d["total_wins"] += 1
                        if net_now < 0:
                            ps_d["consec_losses"] += 1
                            if ps_d["consec_losses"] >= CONSEC_LOSS_LIMIT:
                                ps_d["cooldown_until"] = candle_ts + COOLDOWN_DAYS * 86400
                        else:
                            ps_d["consec_losses"] = 0
                        ps_d["recent_pnls"].append(net_now)
                        if len(ps_d["recent_pnls"]) > SOFT_EXCLUDE_LOOKBACK:
                            ps_d["recent_pnls"] = ps_d["recent_pnls"][-SOFT_EXCLUDE_LOOKBACK:]
                        if (len(ps_d["recent_pnls"]) >= SOFT_EXCLUDE_LOOKBACK
                                and sum(1 for p in ps_d["recent_pnls"] if p < 0) >= SOFT_EXCLUDE_MIN_LOSERS):
                            soft_until = candle_ts + SOFT_EXCLUDE_DAYS * 86400
                            ps_d["cooldown_until"] = max(ps_d["cooldown_until"], soft_until)
                            ps_d["soft_blocks"] += 1
                            ps_d["recent_pnls"] = []
                            soft_blocked += 1

                        closed_trades.append({"contract": pos.contract, "side": pos.side, "entry": pos.entry,
                            "exit": exit_price_now, "size_usd": pos.size_usd, "pnl": net_now, "reason": "DSTOP",
                            "hold_days": pos.hold_days, "entry_day": pos.entry_day_ts, "exit_day": candle_ts,
                            "max_favorable": pos.max_favorable, "max_adverse": pos.max_adverse})
                    else:
                        positions_remaining.append(pos)
                positions = positions_remaining
                cash += dstop_realized
                unrealized = 0.0
                for pos in positions:
                    c = by_pair_candle[pos.contract].get(candle_ts)
                    if c is None: continue
                    unrealized += pos.side * (c['c'] - pos.entry) / pos.entry * pos.size_usd
                equity = cash + unrealized
                equity_curve[-1] = (candle_ts, equity)
                day_stop_events.append({
                    "day": cal_day, "candle_ts": candle_ts,
                    "open_before": positions_before, "open_left": len(positions),
                    "all_closed": positions_before > 0 and len(positions) == 0,
                    "threshold_used": current_threshold, "consec_day": prev_day_pnl < 0,
                    "today_realized": round(today_entry_realized, 2),
                    "today_open_loss": round(today_open_losses, 2),
                })

        if day_loss_stop_active: continue

        # 5) MAX_LOSERS_PER_SIDE фильтр
        can_long = long_count < max_per_side and btc_r == +1
        can_short = short_count < max_per_side and btc_r == -1
        long_losers = 0
        short_losers = 0
        for pos in positions:
            c = by_pair_candle[pos.contract].get(candle_ts)
            if c is None: continue
            pos_unrealized = pos.side * (c['c'] - pos.entry) / pos.entry * pos.size_usd
            if pos_unrealized < 0:
                if pos.side == +1: long_losers += 1
                else: short_losers += 1
        if long_losers >= MAX_LOSERS_PER_SIDE: can_long = False
        if short_losers >= MAX_LOSERS_PER_SIDE: can_short = False

        # 6) Сканирование кандидатов
        candidates = []
        for p, cds in data.items():
            idx = idx_by_pair_candle[p].get(candle_ts)
            if idx is None or idx < DONCHIAN_PERIOD + 2: continue
            if any(pos.contract == p for pos in positions): continue
            ps = pair_stats[p]
            # ── НОВОЕ: проверяем cooldown (включая soft exclude) ──
            if candle_ts < ps["cooldown_until"]:
                cooldown_blocked += 1
                continue
            sig = evaluate_signal(cds[:idx + 1], btc_r, funding_snap)
            if sig is None or sig["side"] == 0:
                if sig and sig.get("adx", 100) < ADX_THRESHOLD: adx_filtered += 1
                continue
            if sig["side"] == +1 and not can_long: continue
            if sig["side"] == -1 and not can_short: continue
            candidates.append((p, sig, idx))

        # Сортировка по atr_pct (как в финале)
        candidates.sort(key=lambda x: x[1]["atr_pct"], reverse=True)

        # 7) Открытие новых
        for p, sig, idx in candidates:
            if new_today >= MAX_NEW_PER_DAY: break
            if len(positions) >= MAX_CONCURRENT: break
            if sig["side"] == +1:
                if long_count >= max_per_side: continue
                long_count += 1
            else:
                if short_count >= max_per_side: continue
                short_count += 1
            stop_dist = ATR_STOP_MULT * sig["atr"]
            stop_pct = stop_dist / sig["close"]
            if stop_pct <= 0: continue
            raw_size = current_risk / stop_pct
            size_usd = min(raw_size, MAX_POSITION_PCT * equity)
            if size_usd < 50: continue
            entry_price = sig["close"]
            pos = Position(p, sig["side"], entry_price, sig["atr"], size_usd, idx, candle_ts, sig["dc_high"])
            positions.append(pos)
            cash -= COMM_TAKER * size_usd
            new_today += 1

    # Хвост: закрытие оставшихся
    if prev_cal_day is not None:
        day_pnl = equity - prev_day_start_equity
        daily_pnl.append((prev_cal_day, day_pnl))

    last_candle_ts = all_candles[-1]
    for pos in positions:
        c = by_pair_candle[pos.contract].get(last_candle_ts)
        if c is None: continue
        gross = pos.side * (c['c'] - pos.entry) / pos.entry * pos.size_usd
        comm = (COMM_TAKER + SLIPPAGE) * pos.size_usd * 2
        net = gross - comm
        closed_trades.append({"contract": pos.contract, "side": pos.side, "entry": pos.entry,
            "exit": c['c'], "size_usd": pos.size_usd, "pnl": net, "reason": "END",
            "hold_days": pos.hold_days, "entry_day": pos.entry_day_ts, "exit_day": last_candle_ts,
            "max_favorable": pos.max_favorable, "max_adverse": pos.max_adverse})
        cash += net
        equity = cash

    return {
        "final_equity":         cash,
        "trades":               closed_trades,
        "equity_curve":         equity_curve,
        "daily_pnl":            daily_pnl,
        "n_trades":             len(closed_trades),
        "n_days":               len(daily_pnl),
        "n_candles":            len(all_candles),
        "btc_blocked":          len(btc_blocked_days_set),
        "dd_brake_days":        len(dd_brake_days_set),
        "adx_filtered":         adx_filtered,
        "cooldown_blocked":     cooldown_blocked,
        "excluded_count":       excluded_count,
        "day_stop_triggered":   day_stop_triggered,
        "day_stop_events":      day_stop_events,
        "partial_tp_count":     partial_tp_count,
        "partial_tp_total_pnl": partial_tp_total_pnl,
        "consec_loss_days_max": consec_loss_days_max,
        "max_dd_peak_ts":       max_dd_peak_ts,
        "max_dd_trough_ts":     max_dd_trough_ts,
        "max_dd_value":         max_dd_value,
        # НОВОЕ:
        "soft_blocked":         soft_blocked,
        "pair_stats":           dict(pair_stats),
    }


# ═════════════════════════════════════════════════════════════════════════════
# ВАЛИДАЦИЯ (сокращённая)
# ═════════════════════════════════════════════════════════════════════════════

def validate(result, z=Z_SCORE):
    final = result["final_equity"]
    total_pnl = final - INIT_CAPITAL
    daily_vals = [p[1] for p in result["daily_pnl"]]
    n = len(daily_vals)

    if n > 1 and statistics.pstdev(daily_vals) > 0:
        std = statistics.stdev(daily_vals)
        ci = z * std / math.sqrt(n)
    else:
        ci = float("inf") if total_pnl > 0 else 0.0
    gate1 = (total_pnl - ci) > 0

    worst_day = min(daily_vals) if daily_vals else 0.0
    gate2 = worst_day >= WORST_DAY_LIMIT
    worst_day_ts = min(result["daily_pnl"], key=lambda p: p[1])[0] if result["daily_pnl"] else None

    losing_days_n = sum(1 for _, pnl in result["daily_pnl"] if pnl < 0)
    loss_streaks, cur_loss_streak = [], []
    for ts, pnl in sorted(result["daily_pnl"], key=lambda p: p[0]):
        if pnl < 0:
            cur_loss_streak.append((ts, pnl))
        else:
            if cur_loss_streak: loss_streaks.append(cur_loss_streak)
            cur_loss_streak = []
    if cur_loss_streak: loss_streaks.append(cur_loss_streak)
    longest_loss_streak = max((len(s) for s in loss_streaks), default=0)
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
        yearly[dt.datetime.utcfromtimestamp(ts).year] += pnl
    gate4 = all(v >= YEAR_LOSS_LIMIT for v in yearly.values())

    reasons = defaultdict(int)
    for t in result["trades"]: reasons[t["reason"]] += 1

    winning_trades = [t for t in result["trades"] if t["pnl"] > 0]
    losing_trades = [t for t in result["trades"] if t["pnl"] < 0]
    gross_profit = sum(t["pnl"] for t in winning_trades)
    gross_loss = abs(sum(t["pnl"] for t in losing_trades))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    avg_win = (gross_profit / len(winning_trades)) if winning_trades else 0.0
    avg_loss = (-gross_loss / len(losing_trades)) if losing_trades else 0.0
    expectancy = ((avg_win * len(winning_trades) + avg_loss * len(losing_trades))
                   / len(result["trades"])) if result["trades"] else 0.0

    # НОВОЕ: статистика по парам для soft exclude анализа
    pair_stats = result.get("pair_stats", {})

    return {
        "final_equity":   final,
        "total_pnl":      total_pnl,
        "ci_z":           ci,
        "n_trades":       result["n_trades"],
        "n_days":         n,
        "gate1":          gate1,
        "gate2":          gate2,
        "gate3":          gate3,
        "gate4":          gate4,
        "worst_day":      worst_day,
        "worst_day_ts":   worst_day_ts,
        "max_dd":         max_dd,
        "losing_days_n":  losing_days_n,
        "longest_loss_streak":  longest_loss_streak,
        "worst_streak_loss":    worst_streak_loss,
        "worst_streak_detail":  worst_streak_detail,
        "yearly_pnl":     dict(yearly),
        "reasons":        dict(reasons),
        "profit_factor":  profit_factor,
        "gross_profit":   gross_profit,
        "gross_loss":     gross_loss,
        "avg_win":        avg_win,
        "avg_loss":       avg_loss,
        "expectancy":     expectancy,
        "winning_trades_n": len(winning_trades),
        "losing_trades_n":  len(losing_trades),
        "soft_blocked":   result.get("soft_blocked", 0),
        "pair_stats":     pair_stats,
        "all_pass":       gate1 and gate2 and gate3 and gate4,
    }


# ═════════════════════════════════════════════════════════════════════════════
# ОТЧЁТ (сокращённый + блок Soft Exclude)
# ═════════════════════════════════════════════════════════════════════════════

_PAIRS_USED = []

def format_report(result, val, n_pairs=None):
    if n_pairs is None: n_pairs = len(_PAIRS_USED)
    lines = []
    lines.append(f"📊 *{STRATEGY_NAME} {STRATEGY_VERSION} - РЕЗУЛЬТАТЫ*  [{STRATEGY_FILE}]")
    lines.append("")
    lines.append(f"Donchian({DONCHIAN_PERIOD}) на {CANDLE_INTERVAL} + BTC SMA({BTC_REGIME_SMA}) 1D + DMI + Trailing {ATR_STOP_MULT}xATR + Partial TP +{int(PARTIAL_TP_PCT*100)}%/{int(PARTIAL_TP_FRACTION*100)}% + Daily Stop today-only + Soft exclude (3/5→30д)")
    lines.append(f"Капитал: ${INIT_CAPITAL:,.0f}  |  Пары: {n_pairs} (ВСЕ, без static EXCLUDE)")
    lines.append(f"Risk: {RISK_FRACTION*100:.1f}% (floor ${SLOT_RISK_MIN:.0f}, cap ${SLOT_RISK_MAX:.0f}, brake x{DD_BRAKE_FACTOR} при DD>${DD_BRAKE_THRESHOLD:.0f})")
    lines.append(f"Max: {MAX_CONCURRENT} поз (per-side {MAX_PER_SIDE_CAP}, фильтр {MAX_LOSERS_PER_SIDE} лузера) | Daily stop: ${abs(DAILY_STOP_LOSS):.0f} today-only | New/day: {MAX_NEW_PER_DAY}")
    lines.append(f"Cooldown: {COOLDOWN_DAYS}д после {CONSEC_LOSS_LIMIT} убытков | SOFT EXCLUDE: {SOFT_EXCLUDE_MIN_LOSERS}/{SOFT_EXCLUDE_LOOKBACK} → {SOFT_EXCLUDE_DAYS}д")
    lines.append(f"Сделок: {val['n_trades']}  |  Дней: {val['n_days']}  |  Свечей 4H: {result.get('n_candles', 0)}")
    lines.append(f"BTC blocked: {val.get('btc_blocked', 0)}д | ADX filtered: {val.get('adx_filtered', 0)} | DD brake: {val.get('dd_brake_days', 0)}д")
    lines.append(f"Cooldown blocks: {val.get('cooldown_blocked', 0)} | Soft exclude blocks: {val.get('soft_blocked', 0)} | Daily stop: {val.get('day_stop_triggered', 0)} | Partial TPs: {val.get('partial_tp_count', 0)} (${val.get('partial_tp_total_pnl', 0):+.0f}) | Max consec loss days: {val.get('consec_loss_days_max', 0)}")
    if val.get("reasons"):
        r = val["reasons"]
        lines.append(f"Исходы: SL/TRAIL={r.get('TRAIL',0)+r.get('SL',0)} PTP={r.get('PTP',0)} TIME={r.get('TIME',0)} SIG={r.get('SIG',0)} DSTOP={r.get('DSTOP',0)} END={r.get('END',0)}")
    lines.append("")
    lines.append(f"Final equity : ${val['final_equity']:,.2f}")
    lines.append(f"Total P&L    : ${val['total_pnl']:,.2f}")
    lines.append(f"CI(Z={Z_SCORE}): ${val['ci_z']:,.2f}")
    pf = val.get("profit_factor", 0)
    pf_str = f"{pf:.2f}" if pf != float("inf") else "∞"
    lines.append(f"Profit Factor: {pf_str} | Win/Loss: {val.get('winning_trades_n',0)}/{val.get('losing_trades_n',0)} | Avg win ${val.get('avg_win',0):+.2f} / Avg loss ${val.get('avg_loss',0):+.2f} | Expectancy ${val.get('expectancy',0):+.2f}")
    lines.append("")
    lines.append("- ВАЛИДАЦИЯ -")
    lines.append(f"① Final - CI > 0   : {'✅ PASS' if val['gate1'] else '❌ FAIL'}  (edge = ${val['total_pnl']-val['ci_z']:,.2f})")
    worst_day_date = (dt.datetime.utcfromtimestamp(val["worst_day_ts"]).strftime("%Y-%m-%d") if val.get("worst_day_ts") else "-")
    lines.append(f"② Worst day ≥ ${WORST_DAY_LIMIT:.0f}: {'✅ PASS' if val['gate2'] else '❌ FAIL'}  (worst = ${val['worst_day']:,.2f}, {worst_day_date})")
    max_dd_peak_ts = val.get("max_dd_peak_ts")
    max_dd_trough_ts = val.get("max_dd_trough_ts")
    dd_dates = ""
    if max_dd_peak_ts and max_dd_trough_ts:
        d_peak = dt.datetime.utcfromtimestamp(max_dd_peak_ts).strftime("%Y-%m-%d")
        d_trough = dt.datetime.utcfromtimestamp(max_dd_trough_ts).strftime("%Y-%m-%d")
        dd_dates = f" ({d_peak} → {d_trough})"
    lines.append(f"③ MaxDD ≤ ${MAX_DD_LIMIT:,.0f}  : {'✅ PASS' if val['gate3'] else '❌ FAIL'}  (MaxDD = ${val['max_dd']:,.2f}){dd_dates}")
    lines.append(f"④ No year < ${YEAR_LOSS_LIMIT:.0f}: {'✅ PASS' if val['gate4'] else '❌ FAIL'}")
    for y in sorted(val["yearly_pnl"]):
        lines.append(f"   {y}: ${val['yearly_pnl'][y]:,.2f}")
    lines.append("")
    verdict = "✅✅✅✅ ALL PASS" if val["all_pass"] else "❌ НЕ ПРОШЁЛ"
    lines.append(f"ИТОГ: {verdict}")

    # ── НОВОЕ: БЛОК SOFT EXCLUDE ──
    lines.append("")
    lines.append(f"- SOFT EXCLUDE (3 из {SOFT_EXCLUDE_LOOKBACK} → {SOFT_EXCLUDE_DAYS}д) -")
    lines.append(f"Сработал: {val.get('soft_blocked', 0)} раз(а) суммарно")
    pair_stats = val.get("pair_stats", {})
    if pair_stats:
        sorted_by_blocks = sorted(pair_stats.items(), key=lambda x: x[1].get("soft_blocks", 0), reverse=True)
        blocked_pairs = [(p, s) for p, s in sorted_by_blocks if s.get("soft_blocks", 0) > 0]
        if blocked_pairs:
            lines.append(f"Топ пар по числу блокировок:")
            for p, s in blocked_pairs[:15]:
                wr = (s["total_wins"] / s["total_trades"] * 100) if s["total_trades"] else 0
                lines.append(f"   {p}: {s['soft_blocks']} раз(а) | всего {s['total_trades']} сделок, winrate {wr:.0f}%, PnL ${s['total_pnl']:+,.2f}")
        else:
            lines.append("Ни одна пара не блокировалась — soft exclude не сработал.")

    # ── НОВОЕ: CANDIDATES FOR PERMANENT EXCLUDE ──
    lines.append("")
    lines.append("- CANDIDATES FOR PERMANENT EXCLUDE (winrate <40% при ≥10 сделок) -")
    if pair_stats:
        candidates = []
        for p, s in pair_stats.items():
            if s["total_trades"] >= 10:
                wr = s["total_wins"] / s["total_trades"]
                if wr < 0.40:
                    candidates.append((p, s["total_trades"], wr, s["total_pnl"]))
        candidates.sort(key=lambda x: x[3])  # сортируем по PnL (худшие первыми)
        if candidates:
            for p, n_tr, wr, pnl in candidates[:15]:
                lines.append(f"   {p}: {n_tr} сделок, winrate {wr*100:.0f}%, PnL ${pnl:+,.2f}")
        else:
            lines.append("Нет кандидатов — все пары с winrate ≥40% при ≥10 сделках.")

    # ── СРАВНЕНИЕ С ФИНАЛОМ ──
    lines.append("")
    lines.append("- СРАВНЕНИЕ С ФИНАЛЬНЫМ БЭКТЕСТОМ -")
    lines.append(f"               ФИНАЛ          →  SOFT TEST")
    lines.append(f"Total P&L:     +$25,063       →  ${val['total_pnl']:+,.0f}")
    lines.append(f"Profit Factor: 1.79           →  {pf_str}")
    lines.append(f"Worst day:     -$830          →  ${val['worst_day']:,.0f}")
    lines.append(f"MaxDD:         $1,739         →  ${val['max_dd']:,.0f}")
    lines.append(f"Winrate:       61.6%          →  {val.get('winning_trades_n',0)/val['n_trades']*100:.1f}%" if val['n_trades'] else "Winrate: 0%")
    lines.append(f"Сделок:        1086           →  {val['n_trades']}")

    # Худшие 5 сделок
    worst_5 = sorted(result["trades"], key=lambda t: t["pnl"])[:5]
    if worst_5:
        lines.append("")
        lines.append("- ХУДШИЕ 5 СДЕЛОК -")
        for w in worst_5:
            d_entry = dt.datetime.utcfromtimestamp(w["entry_day"]).strftime("%Y-%m-%d")
            d_exit = dt.datetime.utcfromtimestamp(w["exit_day"]).strftime("%Y-%m-%d")
            lines.append(f"   {w['contract']} {'L' if w['side']==+1 else 'S'} ${w['pnl']:,.2f} ({w['reason']}, {d_entry} → {d_exit})")

    # Минусовые дни (кратко)
    lines.append("")
    lines.append("- МИНУСОВЫЕ ДНИ (кратко) -")
    lines.append(f"Всего в минусе: {val.get('losing_days_n', 0)}/{val['n_days']}")
    lines.append(f"Самая длинная серия: {val.get('longest_loss_streak', 0)} дн.")
    wsd = val.get("worst_streak_detail")
    if wsd:
        d_from = dt.datetime.utcfromtimestamp(wsd["from"]).strftime("%Y-%m-%d")
        d_to = dt.datetime.utcfromtimestamp(wsd["to"]).strftime("%Y-%m-%d")
        lines.append(f"Худшая серия: {wsd['days']}д ({d_from} → {d_to}), убыток ${val.get('worst_streak_loss', 0):,.2f}")

    # Топ-10 лучших и топ-10 худших пар
    if pair_stats:
        lines.append("")
        lines.append("- ТОП-10 ПРИБЫЛЬНЫХ ПАР -")
        sorted_profit = sorted(pair_stats.items(), key=lambda x: x[1].get("total_pnl", 0), reverse=True)
        for p, s in sorted_profit[:10]:
            wr = (s["total_wins"] / s["total_trades"] * 100) if s["total_trades"] else 0
            lines.append(f"   🟢 {p}: {s['total_trades']} сд, winrate {wr:.0f}%, PnL ${s['total_pnl']:+,.2f}")
        lines.append("")
        lines.append("- ТОП-10 УБЫТОЧНЫХ ПАР -")
        sorted_loss = sorted(pair_stats.items(), key=lambda x: x[1].get("total_pnl", 0))
        for p, s in sorted_loss[:10]:
            wr = (s["total_wins"] / s["total_trades"] * 100) if s["total_trades"] else 0
            lines.append(f"   🔴 {p}: {s['total_trades']} сд, winrate {wr:.0f}%, PnL ${s['total_pnl']:+,.2f}")

    return lines


# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════

def main():
    if B is None:
        print(f"[ERROR] bot.py недоступен: {_BOT_IMPORT_ERR}")
        sys.exit(1)
    global _PAIRS_USED
    pairs = list(B.UPSCALE_PAIRS)
    _PAIRS_USED = pairs
    start = os.environ.get("BT_START", BACKTEST_START_ISO)
    end = os.environ.get("BT_END", BACKTEST_END_ISO)
    B.send_telegram(
        f"🚀 *{STRATEGY_NAME} {STRATEGY_VERSION}* [{STRATEGY_FILE}] старт: "
        f"{len(pairs)} пар (БЕЗ static EXCLUDE), {CANDLE_INTERVAL}, "
        f"SOFT EXCLUDE {SOFT_EXCLUDE_MIN_LOSERS}/{SOFT_EXCLUDE_LOOKBACK}→{SOFT_EXCLUDE_DAYS}д, "
        f"окно {start} → {end or 'сегодня'}"
    )
    result = run_backtest(pairs, start_iso=start, end_iso=end, verbose=True)
    val = validate(result)
    lines = format_report(result, val, n_pairs=len(pairs))
    B.send_blocks(lines)
    try:
        import json
        out_dir = "/home/z/my-project/download"
        os.makedirs(out_dir, exist_ok=True)
        with open(f"{out_dir}/{STRATEGY_FILE}_result.json", "w") as f:
            json.dump({
                "strategy": STRATEGY_NAME,
                "version":  STRATEGY_VERSION,
                "file":     STRATEGY_FILE,
                "validation": {k: (v if not isinstance(v, bool) else int(v)) for k, v in val.items() if k != "pair_stats"},
                "pair_stats": {k: v for k, v in val.get("pair_stats", {}).items()},
                "trades": result["trades"][:200],
                "equity_curve_tail": result["equity_curve"][-60:],
            }, f, indent=2, default=str)
    except Exception as e:
        print(f"[warn] не удалось сохранить результат: {e}")
    return 0 if val["all_pass"] else 2


if __name__ == "__main__":
    sys.exit(main())
