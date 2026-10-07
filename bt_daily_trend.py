# -*- coding: utf-8 -*-
"""
bt_daily_trend.py — Daily Trend-Following Strategy with ATR-Scaled Exits
========================================================================

Запуск через диспетчер бэктестов:
    RUN_BACKTEST=daily_trend python bot.py

Стратегия использует только дневные свечи (interval=1d, limit=2000)
для воспроизводимости бэктеста. Live-фильтры (funding / LSR / OI) добавляются
сверху как опциональные гейты — см. live_signal_filters().

========================================================================
МЕХАНИКА СТРАТЕГИИ
========================================================================

СИГНАЛ ВХОДА (LONG), оценивается по закрытию дневной свечи (00:00 UTC):
  T1. Тренд:        close > SMA(20) на 1d
  T2. Импульс:      close > open И |close/prev_close − 1| > 1.0%
  T3. Волатильность: ATR(14, 1d) / close ∈ [2%, 8%]
                    (отсекаем «мёртвые» пары и гиперволатильные щиткоины)
  T4. Тренд-фильтр наклона: SMA(20) растёт за последние 5 дней
                    (close[-1] > SMA(20) > SMA(20)[-5])

SHORT — зеркальный (close < SMA(20), bearish candle, ret < −1%).

РАЗМЕР ПОЗИЦИИ
  risk_slot   = $200                          (слот по ТЗ)
  stop_dist   = 1.5 × ATR(14, 1d)             (в цене)
  stop_pct    = stop_dist / close
  size_usd    = min($200 / stop_pct, 0.25 × equity)
                 ↑ ограничение 25% капитала на одну позицию

ВХОД
  На открытии следующего дня (D+1 open).

ВЫХОДЫ (проверяются на каждой свече после входа)
  SL:   1.5 × ATR(14, 1d) от entry           (фиксированный)
  TP:   3.0 × ATR(14, 1d) от entry           (RR 1:2)
  TIME: 5 торговых дней (выход по закрытию)
  SIG:  пересечение SMA(20) в обратную сторону

ВАЖНО: если внутри дня сработали и SL, и TP — берём SL (консервативно).

LIVE-ФИЛЬТРЫ (только для реальной торговли, НЕ в бэктесте)
  F1. Funding rate (24h avg) ≤ 0.05% / 8h     — не толкаемся в крайний лонг
  F2. Taker LSR ∈ [1.0, 2.0]                  — отсекаем толпу с одной стороны
  F3. OI_usd растёт за последние 6h           — подтверждение новыми деньгами
  F4. Нет ликвидационного каскада             — long_liq_usd + short_liq_usd
                                                  < 1% от OI_usd за 5m

========================================================================
ПАРАМЕТРЫ ВАЛИДАЦИИ (по ТЗ)
========================================================================
  Капитал:        $10,000
  Комиссия тейк:  0.05% на вход + выход
  Проскальзывание: 0.02% на сторону
  Funding:        считается по snapshot funding_rate из tickers
                  (трётся на 00/08/16 UTC, пока позиция открыта)

ГЕЙТЫ
  ① Final $ − CI(Z=2.64) > 0   (статистическая значимость edge-а)
  ② Худший день ≥ −$500
  ③ MaxDD ≤ $2,000
  ④ Ни одного года с итогом < −$500

========================================================================
"""

import os
import sys
import math
import time
import statistics
import datetime as dt
from collections import defaultdict

# ---------------------------------------------------------------------
# Подключение к фреймворку бота (B.api_get, B.parse_candles, B.send_*, B.UPSCALE_PAIRS)
# ---------------------------------------------------------------------
try:
    import bot as B
except Exception as e:  # оффлайн-тест без bot.py
    B = None
    _BOT_IMPORT_ERR = e
else:
    _BOT_IMPORT_ERR = None


# =====================================================================
#  КОНСТАНТЫ
# =====================================================================

# --- Капитал и риск ---
INIT_CAPITAL     = 10_000.0
SLOT_RISK_USD    = 200.0
MAX_POSITION_PCT = 0.25          # не более 25% equity на одну позицию

# --- Стратегия ---
SMA_PERIOD       = 20
ATR_PERIOD       = 14
SLOPE_LOOKBACK   = 5             # SMA должна расти/падать за N дней
DAILY_MIN_RET    = 0.01          # |return| > 1%
ATR_PCT_MIN      = 0.02          # 2% min daily ATR%
ATR_PCT_MAX      = 0.08          # 8% max daily ATR%
ATR_STOP_MULT    = 1.5           # stop = 1.5 × ATR
ATR_TP_MULT      = 3.0           # TP   = 3.0 × ATR  (RR 1:2)
MAX_HOLD_DAYS    = 5             # time exit
MAX_CONCURRENT   = 10            # одновременных позиций
MAX_NEW_PER_DAY  = 5             # новых входов в день

# --- Торговые издержки ---
COMM_TAKER       = 0.0005        # 0.05% за сторону
SLIPPAGE         = 0.0002        # 0.02% за сторону
FUNDING_TIMES_UTC = (0, 8, 16)   # 3 раза в сутки

# --- Валидация ---
Z_SCORE          = 2.64
WORST_DAY_LIMIT  = -500.0
MAX_DD_LIMIT     = 2_000.0
YEAR_LOSS_LIMIT  = -500.0

# --- Бэктест ---
BACKTEST_START_ISO = "2023-01-01"   # можно переопределить env BT_START


# =====================================================================
#  ИНДИКАТОРЫ (pure Python, без зависимостей)
# =====================================================================

def sma(values, period):
    """Простая скользящая средняя по списку значений."""
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def atr_daily(candles, period=ATR_PERIOD):
    """
    Average True Range по списку дневных свечей [{o,h,l,c}, ...].
    True Range = max(H−L, |H−prevC|, |L−prevC|).
    """
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(-period, 0):
        c, prev = candles[i], candles[i - 1]
        tr = max(
            c['h'] - c['l'],
            abs(c['h'] - prev['c']),
            abs(c['l'] - prev['c']),
        )
        trs.append(tr)
    return sum(trs) / period


# =====================================================================
#  DATA FETCH (c кэшем)
# =====================================================================

_CANDLE_CACHE = {}


def fetch_candles(contract, interval="1d", limit=2000):
    """Получить и распарсить свечи через B.api_get. Кэшируется в памяти."""
    # Принимаем как «BTC», так и «BTC_USDT»
    gate_contract = contract if contract.endswith("_USDT") else f"{contract}_USDT"
    key = (gate_contract, interval, limit)
    if key in _CANDLE_CACHE:
        return _CANDLE_CACHE[key]
    raw = B.api_get("candlesticks", {
        "contract": gate_contract,
        "interval": interval,
        "limit":    limit,
    })
    parsed = B.parse_candles(raw)
    _CANDLE_CACHE[key] = parsed
    return parsed


# =====================================================================
#  ОЦЕНКА СИГНАЛА
# =====================================================================

def evaluate_signal(candles_up_to_today):
    """
    Возвращает dict с ключами:
        side: +1 (long), -1 (short), 0 (нет сигнала)
        atr:  значение ATR(14, 1d)
        close: цена закрытия дня сигнала
        sma:   SMA(20)
        atr_pct: ATR/close
        ret:  доходность дня сигнала
    Или None, если недостаточно данных.
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

    ret = last['c'] / prev['c'] - 1.0
    atr_pct = a / last['c']
    if not (ATR_PCT_MIN <= atr_pct <= ATR_PCT_MAX):
        return {"side": 0, "atr": a, "close": last['c'], "sma": s_now,
                "atr_pct": atr_pct, "ret": ret}

    # LONG: тренд вверх, свеча бычья, импульс > 1%, SMA растёт
    if (last['c'] > s_now
            and last['c'] > last['o']
            and ret > DAILY_MIN_RET
            and s_now > s_past):
        side = +1
    # SHORT: зеркально
    elif (last['c'] < s_now
          and last['c'] < last['o']
          and ret < -DAILY_MIN_RET
          and s_now < s_past):
        side = -1
    else:
        side = 0

    return {"side": side, "atr": a, "close": last['c'], "sma": s_now,
            "atr_pct": atr_pct, "ret": ret}


# =====================================================================
#  POSITION
# =====================================================================

class Position:
    __slots__ = ("contract", "side", "entry", "atr_at_entry",
                 "size_usd", "stop", "tp", "entry_idx",
                 "entry_day_ts", "hold_days")

    def __init__(self, contract, side, entry, atr_at_entry,
                 size_usd, entry_idx, entry_day_ts):
        self.contract       = contract
        self.side           = side              # +1 long / -1 short
        self.entry          = entry
        self.atr_at_entry   = atr_at_entry
        self.size_usd       = size_usd          # USD notional
        self.stop           = entry - side * ATR_STOP_MULT * atr_at_entry
        self.tp             = entry + side * ATR_TP_MULT   * atr_at_entry
        self.entry_idx      = entry_idx
        self.entry_day_ts   = entry_day_ts
        self.hold_days      = 0


# =====================================================================
#  LIVE-ФИЛЬТРЫ (funding / LSR / OI / liquidations)
# =====================================================================

def live_signal_filters(contract):
    """
    Опциональные фильтры для live-торговли. Используют tickers и contract_stats.
    Возвращает (ok: bool, reason: str).
    В бэктесте НЕ вызываются — только при live-сигнале.
    """
    gate_contract = contract if contract.endswith("_USDT") else f"{contract}_USDT"
    try:
        tickers = B.api_get("tickers", {})
        t = next((x for x in tickers if x.get("contract") == gate_contract), None)
        if t is None:
            return False, "no ticker"
        funding = float(t.get("funding_rate", 0))
        # F1: funding не экстремальный
        if abs(funding) > 0.0005:                  # > 0.05% за 8h
            return False, f"funding={funding*100:.3f}%"

        stats = B.api_get("contract_stats", {
            "contract": gate_contract,
            "interval": "5m",
            "limit":    50,
        })
        if not stats or len(stats) < 12:           # нужен хотя бы 1h истории
            return False, "no stats"

        latest = stats[-1]
        prev   = stats[-12]                        # 1h назад (5m × 12)

        # F2: LSR в коридоре [1.0, 2.0]
        lsr = float(latest.get("lsr_taker", 1.0))
        if not (1.0 <= lsr <= 2.0):
            return False, f"LSR={lsr:.2f}"

        # F3: OI_usd растёт за последний час
        oi_now  = float(latest.get("open_interest_usd", 0))
        oi_prev = float(prev.get("open_interest_usd", 0))
        if oi_now <= oi_prev:
            return False, "OI flat/down"

        # F4: нет ликвидационного каскада (> 1% OI за 5m)
        long_liq  = float(latest.get("long_liq_usd", 0))
        short_liq = float(latest.get("short_liq_usd", 0))
        if (long_liq + short_liq) > 0.01 * oi_now and oi_now > 0:
            return False, f"liq cascade ${(long_liq+short_liq):.0f}"

        return True, "ok"
    except Exception as e:
        return False, f"err: {e}"


# =====================================================================
#  ДВИЖОК БЭКТЕСТА
# =====================================================================

def run_backtest(pairs, start_iso=BACKTEST_START_ISO, verbose=True):
    """
    Walk-forward бэктест по дневным свечам.

    Возвращает dict:
        final_equity : float
        trades       : list[dict]
        equity_curve : [(ts, equity), ...]
        daily_pnl    : [(ts, pnl), ...]
        n_trades     : int
    """
    if B is None:
        raise RuntimeError("bot module not available")

    # --- 1) Загрузка свечей ---
    if verbose:
        B.send_telegram(f"📡 Загружаю 1d свечи для {len(pairs)} пар...")
    data = {}
    for i, p in enumerate(pairs):
        try:
            cds = fetch_candles(p, "1d", 2000)
            if cds:
                data[p] = cds
        except Exception as e:
            print(f"[warn] {p}: {e}")
        if verbose and (i + 1) % 20 == 0:
            B.send_telegram(f"  загружено {i+1}/{len(pairs)}")

    if not data:
        raise RuntimeError("Не удалось получить данные ни по одной паре")

    # --- 2) Общий таймлайн ---
    start_ts = int(dt.datetime.fromisoformat(start_iso).timestamp())
    all_days = sorted(set(
        c['t'] for p in data for c in data[p] if c['t'] >= start_ts
    ))
    if len(all_days) < SMA_PERIOD + 5:
        raise RuntimeError(f"Слишком мало дней в истории: {len(all_days)}")

    # Индекс по (pair, ts) → свеча
    by_pair_day = {p: {c['t']: c for c in cds} for p, cds in data.items()}
    # Индекс по (pair, ts) → позиция в cds (для среза candles_up_to)
    idx_by_pair_day = {
        p: {c['t']: i for i, c in enumerate(cds)}
        for p, cds in data.items()
    }

    # --- 3) Funding rate snapshot (по текущим tickers) ---
    # В бэктесте нет исторического funding, поэтому используем snapshot
    # как константу. Это огрубление, но в ТЗ нет эндпоинта для истории funding.
    try:
        funding_snap = {}
        for t in B.api_get("tickers", {}):
            c = t.get("contract", "")
            if c and c.endswith("_USDT"):
                # ключ — голый символ, как в B.UPSCALE_PAIRS
                funding_snap[c[:-5]] = float(t.get("funding_rate", 0))
    except Exception:
        funding_snap = {}

    # --- 4) Состояние портфеля ---
    cash         = INIT_CAPITAL
    positions    = []
    closed_trades = []
    equity_curve = []
    daily_pnl    = []

    # --- 5) Главный цикл по дням ---
    for day_idx, day_ts in enumerate(all_days):
        # 5.1) Закрытие позиций: SL / TP / TIME / SIG
        new_positions = []
        realized_today = 0.0
        for pos in positions:
            candle = by_pair_day[pos.contract].get(day_ts)
            if candle is None:
                new_positions.append(pos)
                continue
            pos.hold_days += 1

            exit_price = None
            exit_reason = None

            # SL/TP по внутридневным high/low.
            # Если оба задеты — консервативно берём SL (худший исход).
            if pos.side == +1:
                if candle['l'] <= pos.stop:
                    exit_price, exit_reason = pos.stop, "SL"
                elif candle['h'] >= pos.tp:
                    exit_price, exit_reason = pos.tp, "TP"
            else:
                if candle['h'] >= pos.stop:
                    exit_price, exit_reason = pos.stop, "SL"
                elif candle['l'] <= pos.tp:
                    exit_price, exit_reason = pos.tp, "TP"

            # TIME exit: по закрытию дня
            if exit_price is None and pos.hold_days >= MAX_HOLD_DAYS:
                exit_price, exit_reason = candle['c'], "TIME"

            # SIG exit: trend reversal (close пересек SMA20 в обратную сторону)
            if exit_price is None:
                idx = idx_by_pair_day[pos.contract].get(day_ts)
                cds = data[pos.contract]
                if idx is not None and idx >= SMA_PERIOD:
                    s = sma([cc['c'] for cc in cds[:idx]], SMA_PERIOD)
                    if s is not None:
                        if pos.side == +1 and candle['c'] < s:
                            exit_price, exit_reason = candle['c'], "SIG"
                        elif pos.side == -1 and candle['c'] > s:
                            exit_price, exit_reason = candle['c'], "SIG"

            if exit_price is not None:
                gross = pos.side * (exit_price - pos.entry) / pos.entry * pos.size_usd
                comm  = (COMM_TAKER + SLIPPAGE) * pos.size_usd * 2  # entry + exit
                # Funding: считаем сколько funding-таймов прошло
                n_fund = 0
                cur = pos.entry_day_ts
                while cur < day_ts:
                    cur_h = dt.datetime.utcfromtimestamp(cur).hour
                    if cur_h in FUNDING_TIMES_UTC:
                        n_fund += 1
                    cur += 3600
                funding_rate = funding_snap.get(pos.contract, 0.0)
                # Long платит положительный funding, short получает
                funding_cost = pos.side * funding_rate * pos.size_usd * n_fund
                net = gross - comm - funding_cost
                realized_today += net
                closed_trades.append({
                    "contract":   pos.contract,
                    "side":       pos.side,
                    "entry":      pos.entry,
                    "exit":       exit_price,
                    "size_usd":   pos.size_usd,
                    "pnl":        net,
                    "reason":     exit_reason,
                    "hold_days":  pos.hold_days,
                    "entry_day":  pos.entry_day_ts,
                    "exit_day":   day_ts,
                })
            else:
                new_positions.append(pos)

        positions = new_positions

        # 5.2) Mark-to-market: нереализованный P&L
        unrealized = 0.0
        for pos in positions:
            candle = by_pair_day[pos.contract].get(day_ts)
            if candle is None:
                continue
            unrealized += pos.side * (candle['c'] - pos.entry) / pos.entry * pos.size_usd

        equity = cash + realized_today + unrealized
        cash  += realized_today
        equity_curve.append((day_ts, equity))

        # daily_pnl: прирост equity за день
        if day_idx == 0:
            day_pnl = equity - INIT_CAPITAL
        else:
            day_pnl = equity - equity_curve[-2][1]
        daily_pnl.append((day_ts, day_pnl))

        # 5.3) Новые входы (по сигналу на закрытии текущего дня,
        #       исполнение по open следующего дня; здесь приближаем close=entry)
        if len(positions) >= MAX_CONCURRENT:
            continue

        candidates = []
        for p, cds in data.items():
            idx = idx_by_pair_day[p].get(day_ts)
            if idx is None or idx < SMA_PERIOD + SLOPE_LOOKBACK + 2:
                continue
            # Не открываем дубликат
            if any(pos.contract == p for pos in positions):
                continue
            sig = evaluate_signal(cds[:idx + 1])
            if sig is None or sig["side"] == 0:
                continue
            candidates.append((p, sig, idx))

        # Ранжирование по силе импульса
        candidates.sort(key=lambda x: abs(x[1]["ret"]), reverse=True)

        new_today = 0
        for p, sig, idx in candidates:
            if new_today >= MAX_NEW_PER_DAY:
                break
            if len(positions) >= MAX_CONCURRENT:
                break

            stop_dist = ATR_STOP_MULT * sig["atr"]
            stop_pct  = stop_dist / sig["close"]
            if stop_pct <= 0:
                continue
            raw_size  = SLOT_RISK_USD / stop_pct
            size_usd  = min(raw_size, MAX_POSITION_PCT * equity)
            if size_usd < 50:    # слишком мелко — пропускаем
                continue

            entry_price = sig["close"]   # упрощение: вход по close дня сигнала
            pos = Position(p, sig["side"], entry_price, sig["atr"],
                           size_usd, idx, day_ts)
            positions.append(pos)
            cash -= COMM_TAKER * size_usd   # комиссия за вход
            new_today += 1

    # 6) Принудительное закрытие остатков на последнем дне
    last_day_ts = all_days[-1]
    for pos in positions:
        candle = by_pair_day[pos.contract].get(last_day_ts)
        if candle is None:
            continue
        gross = pos.side * (candle['c'] - pos.entry) / pos.entry * pos.size_usd
        comm  = (COMM_TAKER + SLIPPAGE) * pos.size_usd * 2
        net   = gross - comm
        closed_trades.append({
            "contract":  pos.contract,
            "side":      pos.side,
            "entry":     pos.entry,
            "exit":      candle['c'],
            "size_usd":  pos.size_usd,
            "pnl":       net,
            "reason":    "END",
            "hold_days": pos.hold_days,
            "entry_day": pos.entry_day_ts,
            "exit_day":  last_day_ts,
        })
        cash += net

    return {
        "final_equity": cash,
        "trades":       closed_trades,
        "equity_curve": equity_curve,
        "daily_pnl":    daily_pnl,
        "n_trades":     len(closed_trades),
        "n_days":       len(all_days),
    }


# =====================================================================
#  ВАЛИДАЦИЯ (4 гейта)
# =====================================================================

def validate(result, z=Z_SCORE):
    """
    Проверяет 4 гейта из ТЗ:
      ① Final $ − CI(Z=2.64) > 0
      ② Худший день ≥ −$500
      ③ MaxDD ≤ $2,000
      ④ Ни одного года с итогом < −$500
    Возвращает dict с деталями.
    """
    final     = result["final_equity"]
    total_pnl = final - INIT_CAPITAL

    # --- ① Final − CI(Z) > 0 ---
    daily_vals = [p[1] for p in result["daily_pnl"]]
    n = len(daily_vals)
    if n > 1 and statistics.pstdev(daily_vals) > 0:
        std = statistics.stdev(daily_vals)
        se  = std / math.sqrt(n)
        ci  = z * se
    else:
        ci = float("inf") if total_pnl > 0 else 0.0
    gate1 = (total_pnl - ci) > 0

    # --- ② Худший день ≥ −$500 ---
    worst_day = min(daily_vals) if daily_vals else 0.0
    gate2 = worst_day >= WORST_DAY_LIMIT

    # --- ③ MaxDD ≤ $2,000 ---
    eqs = [e for _, e in result["equity_curve"]]
    peak, max_dd = -math.inf, 0.0
    for e in eqs:
        peak  = max(peak, e)
        max_dd = max(max_dd, peak - e)
    gate3 = max_dd <= MAX_DD_LIMIT

    # --- ④ Ни одного года < −$500 ---
    yearly = defaultdict(float)
    for ts, pnl in result["daily_pnl"]:
        y = dt.datetime.utcfromtimestamp(ts).year
        yearly[y] += pnl
    gate4 = all(v >= YEAR_LOSS_LIMIT for v in yearly.values())

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
        "all_pass":     gate1 and gate2 and gate3 and gate4,
    }


# =====================================================================
#  ОТЧЁТ В TELEGRAM
# =====================================================================

def format_report(result, val, n_pairs=None):
    """Готовит строки отчёта для B.send_blocks()."""
    if n_pairs is None:
        n_pairs = len(_PAIRS_USED)
    lines = []
    lines.append("📊 *bt_daily_trend — РЕЗУЛЬТАТЫ*")
    lines.append("")
    lines.append(f"Капитал: ${INIT_CAPITAL:,.0f}  |  Пары: {n_pairs}")
    lines.append(f"Сделок: {val['n_trades']}  |  Дней: {val['n_days']}")
    lines.append("")
    lines.append(f"Final equity : ${val['final_equity']:,.2f}")
    lines.append(f"Total P&L    : ${val['total_pnl']:,.2f}")
    lines.append(f"CI(Z={Z_SCORE}): ${val['ci_z']:,.2f}")
    lines.append("")
    lines.append("— ВАЛИДАЦИЯ —")
    lines.append(
        f"① Final − CI > 0        : {'✅ PASS' if val['gate1'] else '❌ FAIL'}"
        f"  (edge = ${val['total_pnl']-val['ci_z']:,.2f})"
    )
    lines.append(
        f"② Worst day ≥ −$500     : {'✅ PASS' if val['gate2'] else '❌ FAIL'}"
        f"  (worst = ${val['worst_day']:,.2f})"
    )
    lines.append(
        f"③ MaxDD ≤ $2,000         : {'✅ PASS' if val['gate3'] else '❌ FAIL'}"
        f"  (MaxDD = ${val['max_dd']:,.2f})"
    )
    lines.append(
        f"④ No year < −$500        : {'✅ PASS' if val['gate4'] else '❌ FAIL'}"
    )
    for y in sorted(val["yearly_pnl"]):
        lines.append(f"   {y}: ${val['yearly_pnl'][y]:,.2f}")
    lines.append("")
    verdict = "✅✅✅✅ ALL PASS" if val["all_pass"] else "❌ НЕ ПРОШЁЛ"
    lines.append(f"ИТОГ: {verdict}")
    return lines


# =====================================================================
#  ТОЧКА ВХОДА
# =====================================================================

_PAIRS_USED = []   # заполняется в main() для отчёта


def main():
    if B is None:
        print(f"[ERROR] bot.py недоступен: {_BOT_IMPORT_ERR}")
        print("Запускайте через диспетчер: RUN_BACKTEST=daily_trend python bot.py")
        sys.exit(1)

    global _PAIRS_USED
    pairs = list(B.UPSCALE_PAIRS)
    _PAIRS_USED = pairs

    start = os.environ.get("BT_START", BACKTEST_START_ISO)
    B.send_telegram(
        f"🚀 *bt_daily_trend* старт: {len(pairs)} пар, начало {start}"
    )

    result = run_backtest(pairs, start_iso=start, verbose=True)
    val    = validate(result)

    lines = format_report(result, val, n_pairs=len(pairs))
    B.send_blocks(lines)

    # Сохраняем трейды для анализа
    try:
        import json
        out_dir = "/home/z/my-project/download"
        os.makedirs(out_dir, exist_ok=True)
        with open(f"{out_dir}/bt_daily_trend_result.json", "w") as f:
            json.dump({
                "validation": {k: (v if not isinstance(v, bool) else int(v))
                               for k, v in val.items()},
                "trades":     result["trades"][:200],   # первые 200 для краткости
                "equity_curve_tail": result["equity_curve"][-60:],
            }, f, indent=2, default=str)
    except Exception as e:
        print(f"[warn] не удалось сохранить результат: {e}")

    # Код возврата для диспетчера
    return 0 if val["all_pass"] else 2


if __name__ == "__main__":
    sys.exit(main())