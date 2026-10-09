# -*- coding: utf-8 -*-
"""bt_donchian_4h_v18.py - Donchian 4H v1.8 (cap=4). RUN_BACKTEST=donchian_4h_v18"""
import os, sys, math, statistics, datetime as dt
from collections import defaultdict
try:
    import bot as B
except Exception as e:
    B = None
    _BOT_IMPORT_ERR = e
else:
    _BOT_IMPORT_ERR = None

STRATEGY_NAME = "Donchian 4H"
STRATEGY_VERSION = "v1.8-TEST"
STRATEGY_FILE = "bt_donchian_4h_v18"
INIT_CAPITAL = 10_000.0
RISK_FRACTION = 0.008
SLOT_RISK_MIN = 80.0
SLOT_RISK_MAX = 200.0
MAX_POSITION_PCT = 0.20
DD_BRAKE_THRESHOLD = 900.0
DD_BRAKE_FACTOR = 0.4
DD_BRAKE_RECOVERY = 0.85
MAX_CONCURRENT = 7
MAX_PER_SIDE_CAP = 4
PER_SIDE_BUDGET = 1200
MAX_NEW_PER_DAY = 8
DAILY_STOP_LOSS = -450.0
DAILY_STOP_LOSS_CONSEC = -300.0
EXCLUDE_PAIRS = {"TRX","XLM","BNB","UNI","LTC","RUNE","PENDLE","HBAR","KAIA","STX","IOTA","ARB","GRT","CRV"}
CONSEC_LOSS_LIMIT = 4
COOLDOWN_DAYS = 14
CANDLE_INTERVAL = "4h"
DONCHIAN_PERIOD = 20
BTC_REGIME_SMA = 50
DMI_PERIOD = 14
ADX_THRESHOLD = 20.0
ATR_PERIOD = 14
ATR_PCT_MIN = 0.006
ATR_PCT_MAX = 0.020
ATR_STOP_MULT = 4.5
MAX_HOLD_DAYS = 35
PARTIAL_TP_PCT = 0.08
PARTIAL_TP_FRACTION = 0.50
STOP_REVERSAL_LOOKFORWARD_DAYS = 15
COMM_TAKER = 0.0005
SLIPPAGE = 0.0002
FUNDING_TIMES_UTC = (0, 8, 16)
Z_SCORE = 2.64
WORST_DAY_LIMIT = -500.0
MAX_DD_LIMIT = 2_000.0
YEAR_LOSS_LIMIT = -500.0
BTC_CONTRACT = "BTC_USDT"
BACKTEST_START_ISO = "2023-01-01"
BACKTEST_END_ISO = ""


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
    seen = set()
    unique = []
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
    long_ok = (last['c'] > dc_high and btc_regime_today == +1 and plus_di > minus_di and abs(funding) <= 0.0005)
    short_ok = (last['c'] < dc_low and btc_regime_today == -1 and minus_di > plus_di and abs(funding) <= 0.0005)
    side = +1 if long_ok else (-1 if short_ok else 0)
    return {"side": side, "atr": a, "close": last['c'], "atr_pct": atr_pct,
            "dc_high": dc_high, "dc_low": dc_low, "plus_di": plus_di, "minus_di": minus_di, "adx": adx}


class Position:
    __slots__ = ("contract", "side", "entry", "atr_at_entry", "size_usd", "original_size_usd",
                 "initial_stop", "trail_stop", "max_favorable", "max_adverse", "entry_idx",
                 "entry_day_ts", "hold_days", "donchian_at_entry", "partial_taken")
    def __init__(self, contract, side, entry, atr_at_entry, size_usd, entry_idx, entry_day_ts, donchian_at_entry):
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


def live_signal_filters(contract):
    try:
        tickers = B.api_get("tickers", {})
        t = next((x for x in tickers if x.get("contract") == contract), None)
        if t is None: return False, "no ticker"
        funding = float(t.get("funding_rate", 0))
        if abs(funding) > 0.0005: return False, f"funding={funding*100:.3f}%"
        stats = B.api_get("contract_stats", {"contract": contract, "interval": "5m", "limit": 50})
        if not stats or len(stats) < 12: return False, "no stats"
        latest = stats[-1]
        prev = stats[-12]
        lsr = float(latest.get("lsr_taker", 1.0))
        if not (1.0 <= lsr <= 2.0): return False, f"LSR={lsr:.2f}"
        oi_now = float(latest.get("open_interest_usd", 0))
        oi_prev = float(prev.get("open_interest_usd", 0))
        if oi_now <= oi_prev: return False, "OI flat/down"
        long_liq = float(latest.get("long_liq_usd", 0))
        short_liq = float(latest.get("short_liq_usd", 0))
        if (long_liq + short_liq) > 0.01 * oi_now and oi_now > 0: return False, "liq cascade"
        return True, "ok"
    except Exception as e:
        return False, f"err: {e}"


def compute_risk_slot(equity, dd_brake_active=False):
    base = max(SLOT_RISK_MIN, min(SLOT_RISK_MAX, equity * RISK_FRACTION))
    if dd_brake_active: base *= DD_BRAKE_FACTOR
    return base

def compute_max_per_side(current_risk):
    if current_risk <= 0: return 0
    return min(MAX_PER_SIDE_CAP, int(PER_SIDE_BUDGET / current_risk))


def run_backtest(pairs, start_iso=BACKTEST_START_ISO, end_iso=BACKTEST_END_ISO, verbose=True):
    if B is None: raise RuntimeError("bot module not available")
    pairs_active = [p for p in pairs if p not in EXCLUDE_PAIRS]
    excluded = len(pairs) - len(pairs_active)
    if verbose:
        B.send_telegram(f"📡 {STRATEGY_NAME} {STRATEGY_VERSION}: загружаю {CANDLE_INTERVAL} свечи (3 стр x 2000) для {len(pairs_active)} пар (excluded {excluded})")
    data = {}
    for i, p in enumerate(pairs_active):
        try:
            cds = fetch_candles(p, CANDLE_INTERVAL, 2000)
            if cds:
                data[p] = cds
                if verbose and i == 0:
                    B.send_telegram(f"  пример {p}: {len(cds)} свечей (от {dt.datetime.utcfromtimestamp(cds[0]['t']).strftime('%Y-%m-%d')} до {dt.datetime.utcfromtimestamp(cds[-1]['t']).strftime('%Y-%m-%d')})")
        except Exception as e:
            print(f"[warn] {p}: {e}")
        if verbose and (i + 1) % 20 == 0:
            B.send_telegram(f"  загружено {i+1}/{len(pairs_active)}")
    if not data: raise RuntimeError("Нет данных")
    if verbose:
        B.send_telegram(f"📡 {STRATEGY_NAME} {STRATEGY_VERSION}: вычисляю BTC regime (1D SMA{BTC_REGIME_SMA})...")
    btc_regime = get_btc_regime()
    funding_snap = get_funding_snapshot()
    start_ts = int(dt.datetime.fromisoformat(start_iso).timestamp())
    end_ts = int(dt.datetime.fromisoformat(end_iso).timestamp()) if end_iso else None
    all_candles = sorted(set(c['t'] for p in data for c in data[p] if c['t'] >= start_ts and (end_ts is None or c['t'] < end_ts)))
    min_periods = max(DONCHIAN_PERIOD, DMI_PERIOD * 2, ATR_PERIOD, BTC_REGIME_SMA) + 5
    if len(all_candles) < min_periods: raise RuntimeError(f"Слишком мало свечей: {len(all_candles)}")
    by_pair_candle = {p: {c['t']: c for c in cds} for p, cds in data.items()}
    idx_by_pair_candle = {p: {c['t']: i for i, c in enumerate(cds)} for p, cds in data.items()}
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
    pair_stats = defaultdict(lambda: {"consec_losses": 0, "cooldown_until": 0})
    cooldown_blocked = 0
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
    for candle_idx, candle_ts in enumerate(all_candles):
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
                    if consec_loss_days_count > consec_loss_days_max: consec_loss_days_max = consec_loss_days_count
                else:
                    consec_loss_days_count = 0
            prev_cal_day = cal_day
            prev_day_start_equity = equity
            day_loss_stop_active = False
            new_today = 0
        positions_before
