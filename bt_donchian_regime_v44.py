# -*- coding: utf-8 -*-
"""
bt_donchian_regime_v44.py - v4.4: Compound + Per-side cap + Daily stop + Exclude + Cooldown
================================================================================

Запуск через диспетчер:
    RUN_BACKTEST=donchian_regime_v44 python bot.py

Спецификация v4.4 (по запросу пользователя):

  1. RISK SLOT: compound с floor $80
     risk = max($80, min($200, equity * 0.008))
     - При $10K: $80 (как v4.2)
     - При $15K: $120
     - При $25K+: $200 (cap)
     - Floor $80 вместо $40 - не даёт risk slot просесть в просадке

  2. PER-SIDE LIMIT (динамический)
     max_per_side = min(3, int($240 / risk))
     - risk $80  -> 3 (max 6 total = 3+3)
     - risk $120 -> 2 (max 4 total = 2+2)
     - risk $200 -> 1 (max 2 total = 1+1)
     Общий риск на сторону держится ~$240.

  3. MAX_CONCURRENT = 6 (3 long + 3 short max)
     Был 4 в v4.2 / 5 в v4.3.

  4. DAILY EMERGENCY STOP = -$400
     Если в течение дня P&L (realized_today + unrealized_today) падает ниже -$400,
     новые входы блокируются до конца дня.
     Ограничивает worst day на уровне -$400...-$500 (стоп + одна позиция).

  5. SAME-SIDE HARD CAP = 3
     Жёсткий потолок позиций в одну сторону, даже если dynamic limit выше.
     Safety-net против корреляционного скопления.

  6. EXCLUDE_PAIRS + COOLDOWN (как в v4.3)
     - 8 пар систематических лузеров выкинуты
     - 3 убыточные сделки подряд -> 30 дней блок

  7. DD BRAKE (как в v4.2/v4.3)
     Если drawdown > $1,200 -> risk x 0.5 до восстановления

Ожидание от v4.4 vs v4.3:
  - MaxDD ниже за счёт daily stop -$400 и per-side cap
  - Worst day ближе к -$400 (а не -$365)
  - P&L на 91/131д выше (max 6 vs 5)
  - 2025 год: может пройти за счёт daily stop (в худший день -$400 вместо -$500)
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

# --- Капитал и риск (v4.4: compound с floor $80) ---
INIT_CAPITAL     = 10_000.0
RISK_FRACTION    = 0.008      # 0.8% от equity на сделку
SLOT_RISK_MIN    = 80.0       # v4.4: floor $80
SLOT_RISK_MAX    = 200.0      # ceiling $200
MAX_POSITION_PCT = 0.20

# --- DD brake ---
DD_BRAKE_THRESHOLD = 1_200.0
DD_BRAKE_FACTOR    = 0.5
DD_BRAKE_RECOVERY  = 0.95

# --- v4.4: Per-side и total limits ---
MAX_CONCURRENT     = 6       # 3 long + 3 short max
MAX_PER_SIDE_CAP   = 3       # жёсткий потолок позиций в одну сторону
PER_SIDE_BUDGET    = 240.0   # max_per_side = min(3, int($240/risk))
DAILY_STOP_LOSS    = -400.0  # v4.4: emergency stop дня

# --- v4.4: Exclude + Cooldown (как в v4.3) ---
EXCLUDE_PAIRS = {
    "TRX_USDT", "XLM_USDT", "BNB_USDT", "UNI_USDT",
    "LTC_USDT", "RUNE_USDT", "PENDLE_USDT", "HBAR_USDT",
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
BACKTEST_START_ISO = "2023-01-01"


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
    adx > 20 -> trend exists (filter disabled in v4.4).
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
    raw = B.api_get("candlesticks", {
        "contract": contract,
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
#  POSITION v4: с trailing stop
# =====================================================================

class Position:
    __slots__ = ("contract", "side", "entry", "atr_at_entry",
                 "size_usd", "initial_stop", "trail_stop",
                 "max_favorable", "entry_idx", "entry_day_ts",
                 "hold_days", "donchian_at_entry")

    def __init__(self, contract, side, entry, atr_at_entry,
                 size_usd, entry_idx, entry_day_ts, donchian_at_entry):
        self.contract           = contract
        self.side               = side
        self.entry              = entry
        self.atr_at_entry       = atr_at_entry
        self.size_usd           = size_usd
        self.initial_stop       = entry - side * ATR_STOP_MULT * atr_at_entry
        self.trail_stop         = self.initial_stop
        self.max_favorable      = entry
        self.entry_idx         = entry_idx
        self.entry_day_ts      = entry_day_ts
        self.hold_days         = 0
        self.donchian_at_entry = donchian_at_entry

    def update_trail(self, candle):
        if self.side == +1:
            self.max_favorable = max(self.max_favorable, candle['h'])
            new_stop = self.max_favorable - ATR_STOP_MULT * self.atr_at_entry
            self.trail_stop = max(self.trail_stop, new_stop)
        else:
            self.max_favorable = min(self.max_favorable, candle['l'])
            new_stop = self.max_favorable + ATR_STOP_MULT * self.atr_at_entry
            self.trail_stop = min(self.trail_stop, new_stop)


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
    """v4.4: compound sizing с floor $80, cap $200, brake x0.5."""
    base = max(SLOT_RISK_MIN, min(SLOT_RISK_MAX, equity * RISK_FRACTION))
    if dd_brake_active:
        base *= DD_BRAKE_FACTOR
    return base


def compute_max_per_side(current_risk):
    """v4.4: динамический лимит позиций в одну сторону.
    max_per_side = min(MAX_PER_SIDE_CAP, int(PER_SIDE_BUDGET / current_risk))
    - risk $80  -> min(3, 3) = 3
    - risk $120 -> min(3, 2) = 2
    - risk $200 -> min(3, 1) = 1
    """
    if current_risk <= 0:
        return 0
    return min(MAX_PER_SIDE_CAP, int(PER_SIDE_BUDGET / current_risk))


def run_backtest(pairs, start_iso=BACKTEST_START_ISO, verbose=True):
    if B is None:
        raise RuntimeError("bot module not available")

    # --- 1) Свечи (с exclude-фильтром v4.4) ---
    pairs_active = [p for p in pairs if p not in EXCLUDE_PAIRS]
    excluded = len(pairs) - len(pairs_active)
    if verbose:
        B.send_telegram(f"📡 v4.4: загружаю 1d свечи для {len(pairs_active)} пар "
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
        B.send_telegram("📡 v4.4: вычисляю BTC weekly regime...")
    btc_regime = get_btc_regime()
    funding_snap = get_funding_snapshot()

    # --- 4) Общий таймлайн ---
    start_ts = int(dt.datetime.fromisoformat(start_iso).timestamp())
    all_days = sorted(set(
        c['t'] for p in data for c in data[p] if c['t'] >= start_ts
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

    # --- 6) Главный цикл ---
    for day_idx, day_ts in enumerate(all_days):
        btc_r = btc_regime.get(day_ts, 0)
        if btc_r == 0:
            btc_blocked += 1

        # v4.4: сброс флага daily stop в начале нового дня
        if day_idx != last_day_idx:
            day_loss_stop_active = False
            last_day_idx = day_idx

        # 6.1) Закрытие по trailing stop / TIME / SIG
        new_positions = []
        realized_today = 0.0
        for pos in positions:
            candle = by_pair_day[pos.contract].get(day_ts)
            if candle is None:
                new_positions.append(pos)
                continue

            pos.hold_days += 1
            pos.update_trail(candle)

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
                funding_rate = funding_snap.get(pos.contract, 0.0)
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

        # v4.4: dynamic per-side limit
        max_per_side = compute_max_per_side(current_risk)
        long_count  = sum(1 for p in positions if p.side == +1)
        short_count = sum(1 for p in positions if p.side == -1)

        # v4.4: daily emergency stop
        day_pnl_check = realized_today + unrealized
        if day_pnl_check < DAILY_STOP_LOSS:
            if not day_loss_stop_active:
                day_loss_stop_active = True
                day_stop_triggered += 1
        if day_loss_stop_active:
            continue

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
            # v4.4: перепроверка per-side лимита
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
        })
        cash += net

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

    return {
        "final_equity": final, "total_pnl": total_pnl, "ci_z": ci,
        "n_trades": result["n_trades"], "n_days": n,
        "gate1": gate1, "gate2": gate2, "gate3": gate3, "gate4": gate4,
        "worst_day": worst_day, "max_dd": max_dd,
        "yearly_pnl": dict(yearly),
        "reasons": dict(reasons),
        "btc_blocked": result.get("btc_blocked", 0),
        "dd_brake_days": result.get("dd_brake_days", 0),
        "adx_filtered": result.get("adx_filtered", 0),
        "cooldown_blocked": result.get("cooldown_blocked", 0),
        "excluded_count": result.get("excluded_count", 0),
        "day_stop_triggered": result.get("day_stop_triggered", 0),
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
    lines.append("📊 *bt_donchian_regime v4.4 — РЕЗУЛЬТАТЫ*")
    lines.append("")
    lines.append(f"Donchian(20) + BTC SMA(50) + DMI + Trailing 2xATR + Compound + PerSide cap + Daily stop + Cooldown")
    lines.append(f"Капитал: ${INIT_CAPITAL:,.0f}  |  Пары: {n_pairs}  |  Excluded: {val['excluded_count']}")
    lines.append(f"Risk: {RISK_FRACTION*100:.1f}% от equity (floor ${SLOT_RISK_MIN:.0f}, cap ${SLOT_RISK_MAX:.0f}, brake x{DD_BRAKE_FACTOR})")
    lines.append(f"Max concurrent: {MAX_CONCURRENT} (per side: min({MAX_PER_SIDE_CAP}, ${PER_SIDE_BUDGET:.0f}/risk)) | Daily stop: ${DAILY_STOP_LOSS:.0f}")
    lines.append(f"Сделок: {val['n_trades']}  |  Дней: {val['n_days']}")
    lines.append(f"BTC blocked: {val['btc_blocked']}  |  ADX filtered: {val['adx_filtered']}  |  DD brake days: {val['dd_brake_days']}")
    lines.append(f"Cooldown blocks: {val['cooldown_blocked']}  |  Daily stop triggered: {val['day_stop_triggered']}")
    if val.get("reasons"):
        r = val["reasons"]
        lines.append(f"Исходы: SL/TRAIL={r.get('TRAIL',0)+r.get('SL',0)} "
                     f"TIME={r.get('TIME',0)} SIG={r.get('SIG',0)} END={r.get('END',0)}")
    lines.append("")
    lines.append(f"Final equity : ${val['final_equity']:,.2f}")
    lines.append(f"Total P&L    : ${val['total_pnl']:,.2f}")
    lines.append(f"CI(Z={Z_SCORE}): ${val['ci_z']:,.2f}")
    lines.append("")
    lines.append("— ВАЛИДАЦИЯ —")
    lines.append(f"① Final − CI > 0     : {'✅ PASS' if val['gate1'] else '❌ FAIL'}"
                 f"  (edge = ${val['total_pnl']-val['ci_z']:,.2f})")
    lines.append(f"② Worst day ≥ −$500   : {'✅ PASS' if val['gate2'] else '❌ FAIL'}"
                 f"  (worst = ${val['worst_day']:,.2f})")
    lines.append(f"③ MaxDD ≤ $2,000      : {'✅ PASS' if val['gate3'] else '❌ FAIL'}"
                 f"  (MaxDD = ${val['max_dd']:,.2f})")
    lines.append(f"④ No year < −$500     : {'✅ PASS' if val['gate4'] else '❌ FAIL'}")
    for y in sorted(val["yearly_pnl"]):
        lines.append(f"   {y}: ${val['yearly_pnl'][y]:,.2f}")
    lines.append("")
    verdict = "✅✅✅✅ ALL PASS" if val["all_pass"] else "❌ НЕ ПРОШЁЛ"
    lines.append(f"ИТОГ: {verdict}")
    return lines


# =====================================================================
#  ТОЧКА ВХОДА
# =====================================================================

def main():
    if B is None:
        print(f"[ERROR] bot.py недоступен: {_BOT_IMPORT_ERR}")
        print("Запускайте через диспетчер: RUN_BACKTEST=donchian_regime_v44 python bot.py")
        sys.exit(1)

    global _PAIRS_USED
    pairs = list(B.UPSCALE_PAIRS)
    _PAIRS_USED = pairs

    start = os.environ.get("BT_START", BACKTEST_START_ISO)
    B.send_telegram(
        f"🚀 *bt_donchian_regime v4.4* старт: {len(pairs)} пар, начало {start}"
    )

    result = run_backtest(pairs, start_iso=start, verbose=True)
    val    = validate(result)

    lines = format_report(result, val, n_pairs=len(pairs))
    B.send_blocks(lines)

    try:
        import json
        out_dir = "/home/z/my-project/download"
        os.makedirs(out_dir, exist_ok=True)
        with open(f"{out_dir}/bt_donchian_regime_v44_result.json", "w") as f:
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
