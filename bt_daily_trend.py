# -*- coding: utf-8 -*-
"""
bt_daily_trend.py v3.0 — Daily Trend-Following | 60-дневный бэктест
=====================================================================

Запуск: RUN_BACKTEST=daily_trend
       (env: BT_DAYS=60  — окно в днях, считается назад от сегодня)

ДАННЫЕ — только с Gate.io API:
  • candlesticks  interval=1d  limit=2000   → свечи, ATR, SMA, сигнал
  • contract_stats interval=1d limit=1200   → OI_usd, lsr_taker, liq (история)

МЕХАНИКА
--------
Сигнал формируется на ЗАКРЫТИИ дня D, вход на ОТКРЫТИИ D+1.

ВХОД LONG:
  T1. close > SMA(20, 1d)
  T2. close > open  И  ret > +1.5%
  T3. ATR%(14, 1d) ∈ [2%, 8%]
  T4. SMA(20) растёт за последние 5 дней

BTC-ФИЛЬТР (B0):
  Торгуем альты только когда BTC сам в апренде:
    BTC close > BTC SMA(20, 1d)  И  BTC ATR% ∈ [1%, 8%]
  Если BTC под SMA20 — новых входов нет (лонги закрываем штатно по правилам выхода).

ИСТОРИЧЕСКИЕ ФИЛЬТРЫ (contract_stats 1d):
  F1. OI_usd вырос за последние 2 дня
  F2. lsr_taker ∈ [0.8, 2.5]
  F3. long_liq_usd + short_liq_usd < 0.5% OI_usd

МЕНЕДЖМЕНТ:
  F4. Consecutive losses ≥ 3 → пауза 1 день

ТОЛЬКО ЛОНГ — шорты убраны.

РАЗМЕР ПОЗИЦИИ
  risk_usd  = $200
  stop_dist = 1.5 × ATR(14, 1d)
  size_usd  = min($200 / stop_pct, 20% equity)
  max одновременно: 8 позиций
  max новых в день: 3

ВЫХОДЫ
  SL:   1.5 × ATR от entry
  TP:   3.0 × ATR от entry  (RR 1:2)
  TIME: 5 торговых дней
  SIG:  close < SMA(20)

ВАЛИДАЦИЯ (4 гейта, пре-рег):
  ① Final$ − CI(Z=2.64) > 0
  ② Худший день ≥ −$500
  ③ MaxDD ≤ $2,000
  ④ Ни одного года с итогом < −$500

Где брать живые OI/LSR/liq для реальной торговли:
  GET /futures/usdt/tickers         → funding_rate, open_interest, open_interest_usd
  GET /futures/usdt/contract_stats  → interval=5m, limit=1 → lsr_taker, long_liq_usd, short_liq_usd
"""

import os
import sys
import math
import time
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

INIT_CAPITAL     = 10_000.0
SLOT_RISK_USD    = 200.0
MAX_POSITION_PCT = 0.20
MAX_CONCURRENT   = 8
MAX_NEW_PER_DAY  = 3

SMA_PERIOD       = 20
ATR_PERIOD       = 14
SLOPE_LOOKBACK   = 5
DAILY_MIN_RET    = 0.015
ATR_PCT_MIN      = 0.02
ATR_PCT_MAX      = 0.08
ATR_STOP_MULT    = 1.5
ATR_TP_MULT      = 3.0
MAX_HOLD_DAYS    = 5

# BTC-фильтр
BTC_SYM          = "BTC"
BTC_ATR_PCT_MIN  = 0.01   # BTC должен двигаться (не меньше 1%)
BTC_ATR_PCT_MAX  = 0.08

# Исторические фильтры (contract_stats 1d)
LSR_MIN          = 0.8
LSR_MAX          = 2.5
LIQ_CASCADE_PCT  = 0.005   # 0.5% OI
OI_LOOKBACK_DAYS = 2
MAX_CONSEC_LOSS  = 3

COMM_TAKER       = 0.0005
SLIPPAGE         = 0.0002
FUNDING_PER_DAY  = 0.0003   # ~0.03%/день (константа для бэктеста)

Z_SCORE          = 2.64
WORST_DAY_LIMIT  = -500.0
MAX_DD_LIMIT     = 2_000.0
YEAR_LOSS_LIMIT  = -500.0

# Окно бэктеста: BT_DAYS дней назад от сегодня
BT_DAYS = int(os.environ.get("BT_DAYS", "60"))
# Можно жёстко задать через BT_START (приоритет над BT_DAYS)
BT_START_OVERRIDE = os.environ.get("BT_START", "")


def _backtest_start_iso():
    if BT_START_OVERRIDE:
        return BT_START_OVERRIDE
    start = dt.datetime.utcnow() - dt.timedelta(days=BT_DAYS)
    return start.strftime("%Y-%m-%d")


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
        tr = max(c['h'] - c['l'], abs(c['h'] - prev['c']), abs(c['l'] - prev['c']))
        trs.append(tr)
    return sum(trs) / period


# =====================================================================
#  DATA FETCH
# =====================================================================

_CANDLE_CACHE = {}
_STATS_CACHE  = {}


def fetch_candles(sym, interval="1d", limit=2000):
    """sym = голый символ (BTC) → добавляем _USDT → Gate API."""
    gate_c = f"{sym}_USDT" if not sym.endswith("_USDT") else sym
    key = (gate_c, interval, limit)
    if key in _CANDLE_CACHE:
        return _CANDLE_CACHE[key]
    raw = B.api_get("candlesticks", {"contract": gate_c, "interval": interval, "limit": limit})
    parsed = B.parse_candles(raw) if raw else []
    _CANDLE_CACHE[key] = parsed
    return parsed


def fetch_stats_daily(sym, limit=1200):
    """
    Исторические данные contract_stats interval=1d.
    Gate хранит ~3 года (limit=1200 ≈ 3.3 года).

    Для ЖИВОЙ торговли используй:
      contract_stats?contract=BTC_USDT&interval=5m&limit=1
      tickers → open_interest, open_interest_usd, funding_rate
    """
    gate_c = f"{sym}_USDT" if not sym.endswith("_USDT") else sym
    if gate_c in _STATS_CACHE:
        return _STATS_CACHE[gate_c]
    raw = B.api_get("contract_stats", {
        "contract": gate_c,
        "interval": "1d",
        "limit":    limit,
    })
    rows = []
    if isinstance(raw, list):
        for r in raw:
            t = int(float(r.get("time") or 0))
            if not t:
                continue
            oi_usd = float(r.get("open_interest_usd") or 0)
            lsr    = float(r.get("lsr_taker") or 0)
            ll     = float(r.get("long_liq_usd")  or 0)
            sl_val = float(r.get("short_liq_usd") or 0)
            rows.append({"t": t, "oi_usd": oi_usd, "lsr": lsr, "liq": ll + sl_val})
    rows.sort(key=lambda x: x["t"])
    _STATS_CACHE[gate_c] = rows
    return rows


# =====================================================================
#  ОЦЕНКА СИГНАЛА
# =====================================================================

def evaluate_signal(candles_up_to_today):
    """
    Только LONG.
    Возвращает dict {side, atr, close, sma, atr_pct, ret} или None.
    side: +1 = long, 0 = нет сигнала.
    """
    cds = candles_up_to_today
    if len(cds) < SMA_PERIOD + SLOPE_LOOKBACK + 2:
        return None

    closes = [c['c'] for c in cds]
    last, prev = cds[-1], cds[-2]

    s_now  = sma(closes[:-1], SMA_PERIOD)
    s_past = sma(closes[:-1 - SLOPE_LOOKBACK], SMA_PERIOD)
    a      = atr_daily(cds[:-1])
    if s_now is None or s_past is None or a is None or a <= 0:
        return None

    ret     = last['c'] / prev['c'] - 1.0
    atr_pct = a / last['c']
    base    = {"side": 0, "atr": a, "close": last['c'], "sma": s_now,
               "atr_pct": atr_pct, "ret": ret}

    if not (ATR_PCT_MIN <= atr_pct <= ATR_PCT_MAX):
        return base

    if (last['c'] > s_now
            and last['c'] > last['o']
            and ret > DAILY_MIN_RET
            and s_now > s_past):
        return {**base, "side": +1}

    return base


def btc_filter_ok(btc_candles, day_ts, idx_by_day):
    """
    B0: BTC close > SMA(20, 1d)  и  ATR% ∈ [1%, 8%].
    Если BTC под SMA20 → не открываем новые лонги.
    """
    idx = idx_by_day.get(day_ts)
    if idx is None or idx < SMA_PERIOD + ATR_PERIOD + 2:
        return True   # нет данных → не блокируем
    cds  = btc_candles[:idx + 1]
    sig  = evaluate_signal(cds)
    if sig is None:
        return True
    # BTC должен быть над SMA и иметь нормальную волатильность
    last = cds[-1]
    if last['c'] <= sig['sma']:
        return False
    if not (BTC_ATR_PCT_MIN <= sig['atr_pct'] <= BTC_ATR_PCT_MAX):
        return False
    return True


# =====================================================================
#  ИСТОРИЧЕСКИЕ ФИЛЬТРЫ
# =====================================================================

def check_hist_filters(sym, day_ts, stats_by_sym):
    """
    F1/F2/F3 по историческим данным contract_stats 1d.
    Возвращает (ok: bool, reason: str).

    Аналог для ЖИВОЙ торговли:
      F1: OI_usd из tickers сейчас vs час назад (contract_stats 5m)
      F2: lsr_taker из contract_stats 5m limit=1
      F3: long_liq_usd+short_liq_usd < 1% OI за 5m (порог строже для жизни)
    """
    rows = stats_by_sym.get(sym, [])
    if not rows:
        return True, "no_stats"

    past = [r for r in rows if r["t"] <= day_ts]
    if len(past) < OI_LOOKBACK_DAYS + 1:
        return True, "too_few"

    cur  = past[-1]
    prev = past[-1 - OI_LOOKBACK_DAYS]

    # F1: OI_usd растёт
    if cur["oi_usd"] > 0 and prev["oi_usd"] > 0:
        if cur["oi_usd"] <= prev["oi_usd"]:
            return False, f"OI_flat oi={cur['oi_usd']:.0f}"

    # F2: LSR в диапазоне
    lsr = cur["lsr"]
    if lsr > 0 and not (LSR_MIN <= lsr <= LSR_MAX):
        return False, f"LSR={lsr:.2f}"

    # F3: нет каскада ликвидаций
    oi = cur["oi_usd"]
    if oi > 0 and cur["liq"] > LIQ_CASCADE_PCT * oi:
        return False, f"liq_cascade={cur['liq']:.0f}"

    return True, "ok"


# =====================================================================
#  POSITION
# =====================================================================

class Position:
    __slots__ = ("contract", "side", "entry", "atr_at_entry",
                 "size_usd", "stop", "tp", "entry_idx",
                 "entry_day_ts", "hold_days")

    def __init__(self, contract, side, entry, atr_at_entry,
                 size_usd, entry_idx, entry_day_ts):
        self.contract     = contract
        self.side         = side
        self.entry        = entry
        self.atr_at_entry = atr_at_entry
        self.size_usd     = size_usd
        self.stop         = entry - ATR_STOP_MULT * atr_at_entry
        self.tp           = entry + ATR_TP_MULT   * atr_at_entry
        self.entry_idx    = entry_idx
        self.entry_day_ts = entry_day_ts
        self.hold_days    = 0


# =====================================================================
#  ДВИЖОК БЭКТЕСТА
# =====================================================================

def run_backtest(pairs, start_iso, verbose=True):
    if B is None:
        raise RuntimeError("bot module not available")

    # --- 1) Свечи для всех пар ---
    if verbose:
        B.send_telegram(f"📡 [v3] Загружаю 1d свечи: {len(pairs)} пар, старт {start_iso}...")
    data = {}
    for i, p in enumerate(pairs):
        try:
            cds = fetch_candles(p, "1d", 2000)
            if len(cds) >= SMA_PERIOD + SLOPE_LOOKBACK + 10:
                data[p] = cds
        except Exception as e:
            print(f"[warn candles] {p}: {e}")
        if verbose and (i + 1) % 25 == 0:
            B.send_telegram(f"  свечи {i+1}/{len(pairs)}")

    if not data:
        raise RuntimeError("Не удалось получить данные ни по одной паре")

    # --- 2) Свечи BTC отдельно ---
    btc_candles = data.get(BTC_SYM, [])
    if not btc_candles:
        try:
            btc_candles = fetch_candles(BTC_SYM, "1d", 2000)
        except Exception:
            btc_candles = []
    btc_idx_by_day = {c['t']: i for i, c in enumerate(btc_candles)}

    # --- 3) Исторические contract_stats 1d ---
    if verbose:
        B.send_telegram(f"📡 Загружаю contract_stats 1d для {len(data)} пар...")
    stats_by_sym = {}
    for i, p in enumerate(list(data.keys())):
        try:
            stats_by_sym[p] = fetch_stats_daily(p)
        except Exception as e:
            print(f"[warn stats] {p}: {e}")
            stats_by_sym[p] = []
        if verbose and (i + 1) % 25 == 0:
            B.send_telegram(f"  stats {i+1}/{len(data)}")

    # --- 4) Таймлайн ---
    start_ts = int(dt.datetime.fromisoformat(start_iso).replace(
        tzinfo=dt.timezone.utc).timestamp())
    all_days = sorted(set(
        c['t'] for p in data for c in data[p] if c['t'] >= start_ts
    ))
    if len(all_days) < SMA_PERIOD + 10:
        raise RuntimeError(f"Мало дней в окне бэктеста: {len(all_days)} (нужно ≥{SMA_PERIOD+10})")

    by_pair_day     = {p: {c['t']: c for c in cds} for p, cds in data.items()}
    idx_by_pair_day = {p: {c['t']: i for i, c in enumerate(cds)} for p, cds in data.items()}

    # --- 5) Состояние ---
    cash          = INIT_CAPITAL
    positions     = []
    closed_trades = []
    equity_curve  = []
    daily_pnl     = []
    consec_loss   = 0
    pause_day     = None
    btc_filter_blocks = 0   # счётчик дней, когда BTC блокировал входы

    # --- 6) Главный цикл ---
    for day_idx, day_ts in enumerate(all_days):

        # 6.1) Закрытие позиций
        new_positions  = []
        realized_today = 0.0
        for pos in positions:
            candle = by_pair_day[pos.contract].get(day_ts)
            if candle is None:
                new_positions.append(pos)
                continue
            pos.hold_days += 1

            exit_price, exit_reason = None, None

            if candle['l'] <= pos.stop:
                exit_price, exit_reason = pos.stop, "SL"
            elif candle['h'] >= pos.tp:
                exit_price, exit_reason = pos.tp, "TP"

            if exit_price is None and pos.hold_days >= MAX_HOLD_DAYS:
                exit_price, exit_reason = candle['c'], "TIME"

            if exit_price is None:
                idx = idx_by_pair_day[pos.contract].get(day_ts)
                cds = data[pos.contract]
                if idx is not None and idx >= SMA_PERIOD:
                    s = sma([cc['c'] for cc in cds[:idx]], SMA_PERIOD)
                    if s is not None and candle['c'] < s:
                        exit_price, exit_reason = candle['c'], "SIG"

            if exit_price is not None:
                gross     = (exit_price - pos.entry) / pos.entry * pos.size_usd
                comm      = (COMM_TAKER + SLIPPAGE) * pos.size_usd * 2
                fund_cost = FUNDING_PER_DAY * pos.size_usd * max(pos.hold_days, 1)
                net       = gross - comm - fund_cost
                realized_today += net
                closed_trades.append({
                    "contract":  pos.contract,
                    "side":      pos.side,
                    "entry":     pos.entry,
                    "exit":      exit_price,
                    "size_usd":  pos.size_usd,
                    "pnl":       net,
                    "reason":    exit_reason,
                    "hold_days": pos.hold_days,
                    "entry_day": pos.entry_day_ts,
                    "exit_day":  day_ts,
                })
                if net < 0:
                    consec_loss += 1
                    if consec_loss >= MAX_CONSEC_LOSS:
                        pause_day = day_ts
                else:
                    consec_loss = 0
            else:
                new_positions.append(pos)

        positions = new_positions
        cash += realized_today

        # 6.2) Mark-to-market
        unrealized = 0.0
        for pos in positions:
            candle = by_pair_day[pos.contract].get(day_ts)
            if candle:
                unrealized += (candle['c'] - pos.entry) / pos.entry * pos.size_usd

        equity = cash + unrealized
        equity_curve.append((day_ts, equity))
        day_pnl = equity - (equity_curve[-2][1] if day_idx > 0 else INIT_CAPITAL)
        daily_pnl.append((day_ts, day_pnl))

        # 6.3) Пауза после серии убытков
        if pause_day == day_ts:
            continue

        # 6.4) BTC-фильтр (B0)
        if btc_candles:
            if not btc_filter_ok(btc_candles, day_ts, btc_idx_by_day):
                btc_filter_blocks += 1
                continue

        # 6.5) Новые входы
        if len(positions) >= MAX_CONCURRENT:
            continue

        candidates = []
        for p, cds in data.items():
            if p == BTC_SYM:
                continue   # BTC не торгуем как альт
            idx = idx_by_pair_day[p].get(day_ts)
            if idx is None or idx < SMA_PERIOD + SLOPE_LOOKBACK + 2:
                continue
            if any(pos.contract == p for pos in positions):
                continue

            sig = evaluate_signal(cds[:idx + 1])
            if sig is None or sig["side"] == 0:
                continue

            ok, reason = check_hist_filters(p, day_ts, stats_by_sym)
            if not ok:
                continue

            candidates.append((p, sig, idx))

        candidates.sort(key=lambda x: x[1]["ret"], reverse=True)

        new_today = 0
        for p, sig, idx in candidates:
            if new_today >= MAX_NEW_PER_DAY:
                break
            if len(positions) >= MAX_CONCURRENT:
                break

            stop_pct = ATR_STOP_MULT * sig["atr"] / sig["close"]
            if stop_pct <= 0:
                continue
            size_usd = min(SLOT_RISK_USD / stop_pct, MAX_POSITION_PCT * equity)
            if size_usd < 50:
                continue

            # Вход по open следующего дня
            next_days = [d for d in all_days if d > day_ts]
            if next_days:
                nc = by_pair_day[p].get(next_days[0])
                entry_price = nc['o'] if nc and nc['o'] > 0 else sig["close"]
            else:
                entry_price = sig["close"]

            pos = Position(p, +1, entry_price, sig["atr"], size_usd, idx, day_ts)
            positions.append(pos)
            cash -= (COMM_TAKER + SLIPPAGE) * size_usd
            new_today += 1

    # 7) Принудительное закрытие остатков
    last_day_ts = all_days[-1]
    for pos in positions:
        candle = by_pair_day[pos.contract].get(last_day_ts)
        if candle is None:
            continue
        gross     = (candle['c'] - pos.entry) / pos.entry * pos.size_usd
        comm      = (COMM_TAKER + SLIPPAGE) * pos.size_usd * 2
        fund_cost = FUNDING_PER_DAY * pos.size_usd * max(pos.hold_days, 1)
        net       = gross - comm - fund_cost
        closed_trades.append({
            "contract":  pos.contract, "side": pos.side,
            "entry":     pos.entry,    "exit": candle['c'],
            "size_usd":  pos.size_usd, "pnl":  net,
            "reason":    "EOD",        "hold_days": pos.hold_days,
            "entry_day": pos.entry_day_ts, "exit_day": last_day_ts,
        })
        cash += net

    return {
        "final_equity":      cash,
        "trades":            closed_trades,
        "equity_curve":      equity_curve,
        "daily_pnl":         daily_pnl,
        "n_trades":          len(closed_trades),
        "n_days":            len(all_days),
        "btc_filter_blocks": btc_filter_blocks,
        "start_iso":         start_iso,
    }


# =====================================================================
#  ВАЛИДАЦИЯ
# =====================================================================

def validate(result, z=Z_SCORE):
    final     = result["final_equity"]
    total_pnl = final - INIT_CAPITAL

    daily_vals = [p[1] for p in result["daily_pnl"]]
    n = len(daily_vals)
    if n > 1:
        std = statistics.pstdev(daily_vals)
        ci  = z * std * math.sqrt(n) if std > 0 else 0.0
    else:
        ci = 0.0
    gate1 = (total_pnl - ci) > 0

    worst_day = min(daily_vals) if daily_vals else 0.0
    gate2     = worst_day >= WORST_DAY_LIMIT

    eqs = [e for _, e in result["equity_curve"]]
    peak, max_dd = INIT_CAPITAL, 0.0
    for e in eqs:
        peak  = max(peak, e)
        max_dd = max(max_dd, peak - e)
    gate3 = max_dd <= MAX_DD_LIMIT

    yearly = defaultdict(float)
    for ts, pnl in result["daily_pnl"]:
        yearly[dt.datetime.utcfromtimestamp(ts).year] += pnl
    gate4 = all(v >= YEAR_LOSS_LIMIT for v in yearly.values())

    trades = result["trades"]
    wr     = (sum(1 for t in trades if t["pnl"] > 0) / len(trades) * 100) if trades else 0
    reasons = defaultdict(int)
    for t in trades:
        reasons[t["reason"]] += 1

    return {
        "final_equity": final,
        "total_pnl":    total_pnl,
        "ci_z":         ci,
        "n_trades":     result["n_trades"],
        "n_days":       n,
        "gate1":        gate1,
        "gate2":        gate2,
        "gate3":        gate3,
        "gate4":        gate4,
        "worst_day":    worst_day,
        "max_dd":       max_dd,
        "yearly_pnl":   dict(yearly),
        "win_rate":     wr,
        "exit_reasons": dict(reasons),
        "all_pass":     gate1 and gate2 and gate3 and gate4,
    }


# =====================================================================
#  ОТЧЁТ
# =====================================================================

def format_report(result, val, n_pairs):
    btc_blocks = result.get("btc_filter_blocks", 0)
    lines = [
        f"📊 <b>bt_daily_trend v3.0 — {BT_DAYS}д бэктест</b>",
        f"<i>Только лонг | OI↑ | LSR [0.8-2.5] | liq&lt;0.5%OI | BTC-фильтр</i>",
        f"<i>Период: {result['start_iso']} → сегодня</i>",
        "",
        f"Капитал: ${INIT_CAPITAL:,.0f}  |  Пары: {n_pairs}",
        f"Сделок: {val['n_trades']}  |  Дней: {val['n_days']}  |  ВР: {val['win_rate']:.0f}%",
        f"BTC блокировал: {btc_blocks} дн  |  Исходы: {val['exit_reasons']}",
        "",
        f"Final equity : ${val['final_equity']:+,.2f}",
        f"Total P&L    : ${val['total_pnl']:+,.2f}",
        f"CI(Z={Z_SCORE}): ±${val['ci_z']:,.2f}",
        "",
        "<b>— ВАЛИДАЦИЯ —</b>",
        f"① Final − CI &gt; 0  : {'✅ PASS' if val['gate1'] else '❌ FAIL'}"
        f"  (edge = ${val['total_pnl'] - val['ci_z']:+,.2f})",
        f"② Worst day ≥ −$500 : {'✅ PASS' if val['gate2'] else '❌ FAIL'}"
        f"  (worst = ${val['worst_day']:+,.2f})",
        f"③ MaxDD ≤ $2,000    : {'✅ PASS' if val['gate3'] else '❌ FAIL'}"
        f"  (MaxDD = ${val['max_dd']:,.2f})",
        f"④ No year &lt; −$500 : {'✅ PASS' if val['gate4'] else '❌ FAIL'}",
    ]
    for y in sorted(val["yearly_pnl"]):
        lines.append(f"   {y}: ${val['yearly_pnl'][y]:+,.2f}")
    lines.append("")
    verdict = "✅✅✅✅ ALL PASS — форвард!" if val["all_pass"] else "❌ НЕ ПРОШЁЛ"
    lines.append(f"<b>ИТОГ: {verdict}</b>")
    return lines


# =====================================================================
#  ТОЧКА ВХОДА
# =====================================================================

def main():
    if B is None:
        print(f"[ERROR] bot.py недоступен: {_BOT_IMPORT_ERR}")
        sys.exit(1)

    pairs      = list(B.UPSCALE_PAIRS)
    start_iso  = _backtest_start_iso()
    n_pairs    = len(pairs)

    B.send_telegram(
        f"🚀 <b>bt_daily_trend v3.0</b>\n"
        f"Пар: {n_pairs} | Окно: {BT_DAYS} дн | Старт: {start_iso}\n"
        f"<i>Лонг + OI/LSR/liq + BTC-фильтр | данные Gate API</i>"
    )

    try:
        result = run_backtest(pairs, start_iso=start_iso, verbose=True)
        val    = validate(result)
        lines  = format_report(result, val, n_pairs)
        B.send_blocks(lines)
    except Exception:
        import traceback
        tb = traceback.format_exc()
        print(tb)
        try:
            B.send_telegram(f"⚠️ bt_daily_trend v3 упал:\n{tb[-500:]}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
