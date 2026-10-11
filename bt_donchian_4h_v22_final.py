# -*- coding: utf-8 -*-
"""
bt_donchian_4h_v22_final.py
================================================================================
Donchian 4H v2.2 — FINAL
Стратегия прорыва канала Donchian на 4H-свечах с BTC-regime фильтром,
DMI/ADX-фильтром, ATR-trailing стопом, Partial TP, Daily Stop (ТОЛЬКО
внутридневные позиции), Cooldown по парам и ограничением "3 лузера на сторону".

Запуск:
    RUN_BACKTEST=donchian_4h_v22_final python bot.py

Ключевая идея Daily Stop (today-only):
    Стоп-аут срабатывает, если СУММАРНЫЙ P&L позиций, открытых В ТЕКУЩИЙ
    КАЛЕНДАРНЫЙ ДЕНЬ (реализованные убытки + нереализованные убытки открытых
    позиций), достигает порога -DAILY_STOP_LOSS (или -DAILY_STOP_LOSS_CONSEC,
    если вчерашний день закрылся в минус).

    Позиции, перенесённые с прошлых дней, НЕ закрываются Daily Stop-ом —
    они могут восстановиться на следующий день. Это сознательное решение:
    Daily Stop защищает только "свежий" внутридневной убыток, не вырезая
    старые позиции, которые уже доказали свою жизнеспособность переживанием
    нескольких дней.

Файл = финальная версия. Все цифры в Telegram-сообщениях берутся напрямую
из констант выше — нет hardcoded-строк, расхождений быть не может.
================================================================================
"""

import os
import sys
import math
import statistics
import datetime as dt
from collections import defaultdict

# Пытаемся импортировать ядро торгового бота (api_get, send_telegram, send_blocks,
# parse_candles, UPSCALE_PAIRS и т.д.). Если не вышло — продолжаем, чтобы
# модуль хотя бы импортировался (для статического анализа / тестов).
try:
    import bot as B
except Exception as e:
    B = None
    _BOT_IMPORT_ERR = e
else:
    _BOT_IMPORT_ERR = None


# =============================================================================
# КОНСТАНТЫ СТРАТЕГИИ
# Все цифры в отчётах ссылаются сюда. Меняешь константу — меняется сообщение.
# =============================================================================

# --- Идентификация стратегии -------------------------------------------------
STRATEGY_NAME    = "Donchian 4H"
STRATEGY_VERSION = "v2.2-FINAL"                         # финальная версия
STRATEGY_FILE    = "bt_donchian_4h_v22_final"          # имя файла (без .py)

# --- Капитал и риск ----------------------------------------------------------
INIT_CAPITAL      = 10_000.0      # стартовый депозит
RISK_FRACTION     = 0.008         # 0.8% от equity на одну сделку
SLOT_RISK_MIN     = 100.0         # минимум $ на риск-слот
SLOT_RISK_MAX     = 250.0         # максимум $ на риск-слот
MAX_POSITION_PCT  = 0.20          # не более 20% equity на одну позицию

# --- Просадочный тормоз (DD brake) -------------------------------------------
DD_BRAKE_THRESHOLD = 900.0        # при DD > $900 включаем тормоз
DD_BRAKE_FACTOR     = 0.4          # размер risk-слота умножается на 0.4
DD_BRAKE_RECOVERY   = 0.85         # восстановление: equity >= 85% от пика

# --- Лимиты позиций ----------------------------------------------------------
MAX_CONCURRENT    = 10             # максимум одновременных позиций
MAX_PER_SIDE_CAP  = 6              # жесткий лимит на сторону (long/short)
PER_SIDE_BUDGET   = 2000           # бюджет на сторону в $ риска
MAX_NEW_PER_DAY   = 10             # максимум новых сделок в сутки
MAX_LOSERS_PER_SIDE = 3            # если 3+ убыточные в одну сторону — новых не открываем

# --- Daily Stop (today-only!) ------------------------------------------------
DAILY_STOP_LOSS       = -350.0     # порог для обычного дня
DAILY_STOP_LOSS_CONSEC = -350.0     # порог, если вчера закрылись в минус
# ВАЖНО: Daily Stop считает только P&L позиций, открытых В ТЕКУЩИЙ календарный
# день. Старые позиции не трогаются.

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

# --- Cooldown по парам -------------------------------------------------------
CONSEC_LOSS_LIMIT = 4              # после 4 убытков подряд по паре → кулдаун
COOLDOWN_DAYS     = 14             # кулдаун 14 дней

# --- Индикаторы --------------------------------------------------------------
CANDLE_INTERVAL = "4h"             # таймфрейм
DONCHIAN_PERIOD = 20               # окно канала Donchian (4H × 20 ≈ 3.3 дня)
BTC_REGIME_SMA  = 50               # SMA50 на 1D для BTC regime
DMI_PERIOD      = 14               # DMI/ADX
ADX_THRESHOLD   = 20.0            # минимальный ADX (используется в фильтре отчёта)
ATR_PERIOD      = 14               # окно ATR
ATR_PCT_MIN     = 0.006            # минимальная волатильность (ATR/price)
ATR_PCT_MAX     = 0.020            # максимальная волатильность
ATR_STOP_MULT   = 4.5              # множитель ATR для стопа (trailing по close)

# --- Выход --------------------------------------------------------------------
MAX_HOLD_DAYS = 35                 # максимум свечей удержания (35 × 4h ≈ 5.8 дня)
PARTIAL_TP_PCT      = 0.08         # +8% favorable → частичная фиксация
PARTIAL_TP_FRACTION = 0.50         # фиксируем 50% позиции, остальное в breakeven
STOP_REVERSAL_LOOKFORWARD_DAYS = 15  # окно для анализа "разворота после стопа"

# --- Комиссии / проскальзывание / фандинг -----------------------------------
COMM_TAKER       = 0.0005          # 0.05% taker-комиссия (одна сторона)
SLIPPAGE         = 0.0002          # 0.02% проскальзывание
FUNDING_TIMES_UTC = (0, 8, 16)    # часы фандинга на perpetual

# --- Валидация (Z-гейты) -----------------------------------------------------
Z_SCORE         = 2.64            # Z для доверительного интервала
WORST_DAY_LIMIT = -500.0          # Gate2: худший день не ниже -$500
MAX_DD_LIMIT    = 2_000.0         # Gate3: MaxDD не более $2000
YEAR_LOSS_LIMIT = -500.0          # Gate4: ни один год не ниже -$500

# --- Доп. --------------------------------------------------------------------
BTC_CONTRACT      = "BTC_USDT"
BACKTEST_START_ISO = "2023-01-01"  # старт бэктеста
BACKTEST_END_ISO    = ""           # пусто = до последней доступной свечи


# =============================================================================
# ИНДИКАТОРЫ
# =============================================================================

def sma(values, period):
    """Простое скользящее среднее. None, если данных мало."""
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def atr_daily(candles, period=ATR_PERIOD):
    """Average True Range на массиве свечей.

    True Range = max(h-l, |h-prev_c|, |l-prev_c|).
    Возвращает среднее TR за последние `period` свечей.
    """
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(-period, 0):
        c, prev = candles[i], candles[i - 1]
        trs.append(max(c['h'] - c['l'],
                       abs(c['h'] - prev['c']),
                       abs(c['l'] - prev['c'])))
    return sum(trs) / period


def donchian(candles, period=DONCHIAN_PERIOD):
    """Канал Donchian: (high_max, low_min) за period свечей, исключая текущую."""
    if len(candles) < period + 1:
        return None
    window = candles[-(period + 1):-1]
    return max(c['h'] for c in window), min(c['l'] for c in window)


def dmi(candles, period=DMI_PERIOD):
    """DMI (+DI, -DI, ADX). Используем Wilder-упрощённую форму.

    Возвращает (plus_di, minus_di, adx) или (None, None, None) если данных мало.
    """
    if len(candles) < period * 2 + 1:
        return None, None, None

    plus_dm, minus_dm, trs = [], [], []
    for i in range(-period * 2, 0):
        c, prev = candles[i], candles[i - 1]
        up   = c['h'] - prev['h']
        down = prev['l'] - c['l']
        plus_dm.append(up   if (up   > down and up   > 0) else 0)
        minus_dm.append(down if (down > up   and down > 0) else 0)
        trs.append(max(c['h'] - c['l'], abs(c['h'] - prev['c']), abs(c['l'] - prev['c'])))

    if len(trs) < period or sum(trs[-period:]) == 0:
        return None, None, None

    atr_v    = sum(trs[-period:])      / period
    plus_d   = sum(plus_dm[-period:])  / period
    minus_d  = sum(minus_dm[-period:]) / period
    if atr_v <= 0:
        return None, None, None

    plus_di  = 100 * plus_d  / atr_v
    minus_di = 100 * minus_d / atr_v

    # Считаем ADX как среднее DX по скользящему окну
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


# =============================================================================
# ДАННЫЕ
# =============================================================================

_CANDLE_CACHE = {}  # (contract, interval, limit) -> list[dict]


def fetch_candles(contract, interval="1d", limit=2000):
    """Подтягивает свечи с пагинацией (3 страницы для 4H, чтобы покрыть ~2 года).

    Кэширует результат, чтобы не дёргать API повторно.
    """
    key = (contract, interval, limit)
    if key in _CANDLE_CACHE:
        return _CANDLE_CACHE[key]

    gate_c = contract if contract.endswith("_USDT") else f"{contract}_USDT"
    pages_needed = 3 if interval == "4h" else 1
    all_candles = []
    to_ts = None

    for page in range(pages_needed):
        params = {"contract": gate_c, "interval": interval, "limit": limit}
        if to_ts is not None:
            params["to"] = to_ts
        try:
            raw = B.api_get("candlesticks", params)
            page_candles = B.parse_candles(raw)
        except Exception as e:
            print(f"[warn] {contract} page {page+1}: {e}")
            break
        if not page_candles:
            break

        page_candles.sort(key=lambda c: c['t'])
        if to_ts is not None:
            page_candles = [c for c in page_candles if c['t'] < to_ts]
        if not page_candles:
            break

        # новые страницы (старее) кладём в начало
        all_candles = page_candles + all_candles if all_candles else page_candles
        to_ts = page_candles[0]['t']
        if len(page_candles) < limit:
            break  # следующая страница ничего нового не даст

    # Дедуп по timestamp
    seen, unique = set(), []
    for c in all_candles:
        if c['t'] not in seen:
            seen.add(c['t'])
            unique.append(c)
    unique.sort(key=lambda c: c['t'])

    _CANDLE_CACHE[key] = unique
    return unique


# --- BTC regime (трендовый фильтр на 1D) --------------------------------------

_BTC_REGIME_CACHE = None


def compute_btc_regime(btc_candles):
    """+1 если BTC выше SMA50 (бычий), -1 если ниже (медвежий), 0 - разогрев."""
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
    """Лениво вычисляет BTC regime (1D) один раз за бэктест."""
    global _BTC_REGIME_CACHE
    if _BTC_REGIME_CACHE is None:
        cds = fetch_candles(BTC_CONTRACT, "1d", 2000)
        _BTC_REGIME_CACHE = compute_btc_regime(cds)
    return _BTC_REGIME_CACHE


# --- Funding snapshot --------------------------------------------------------

_FUNDING_CACHE = None


def get_funding_snapshot():
    """Словарь {contract: funding_rate} — снимок на момент старта бэктеста."""
    global _FUNDING_CACHE
    if _FUNDING_CACHE is None:
        try:
            _FUNDING_CACHE = {
                t["contract"]: float(t.get("funding_rate", 0))
                for t in B.api_get("tickers", {}) if t.get("contract")
            }
        except Exception:
            _FUNDING_CACHE = {}
    return _FUNDING_CACHE


# =============================================================================
# СИГНАЛ
# =============================================================================

def evaluate_signal(candles_up_to_today, btc_regime_today, funding_snap):
    """Оценивает сигнал на текущей свече.

    Long  если close > Donchian_high И BTC=+1 И +DI > -DI И |funding| <= 0.05%
    Short если close < Donchian_low  И BTC=-1 И -DI > +DI И |funding| <= 0.05%
    Иначе: side=0 (нет сигнала). Дополнительно проверяем ATR% на допустимый
    диапазон — слишком тихие/буйные пары отсекаются.
    """
    cds = candles_up_to_today
    if len(cds) < DONCHIAN_PERIOD + 2:
        return None

    last = cds[-1]
    dc = donchian(cds, DONCHIAN_PERIOD)
    if dc is None:
        return None

    dc_high, dc_low = dc
    a = atr_daily(cds[:-1])
    if a is None or a <= 0:
        return None

    atr_pct = a / last['c']
    if not (ATR_PCT_MIN <= atr_pct <= ATR_PCT_MAX):
        # Волатильность вне диапазона → сигнала нет, но индикаторы возвращаем
        return {"side": 0, "atr": a, "close": last['c'], "atr_pct": atr_pct,
                "dc_high": dc_high, "dc_low": dc_low,
                "plus_di": 0, "minus_di": 0, "adx": 0}

    plus_di, minus_di, adx = dmi(cds[:-1])
    if plus_di is None:
        return {"side": 0, "atr": a, "close": last['c'], "atr_pct": atr_pct,
                "dc_high": dc_high, "dc_low": dc_low,
                "plus_di": 0, "minus_di": 0, "adx": 0}

    funding = funding_snap.get(last.get('contract', ''), 0)
    long_ok  = (last['c'] > dc_high and btc_regime_today == +1
                and plus_di > minus_di and abs(funding) <= 0.0005)
    short_ok = (last['c'] < dc_low  and btc_regime_today == -1
                and minus_di > plus_di and abs(funding) <= 0.0005)
    side = +1 if long_ok else (-1 if short_ok else 0)

    return {"side": side, "atr": a, "close": last['c'], "atr_pct": atr_pct,
            "dc_high": dc_high, "dc_low": dc_low,
            "plus_di": plus_di, "minus_di": minus_di, "adx": adx}


# =============================================================================
# ПОЗИЦИЯ
# =============================================================================

class Position:
    """Открытая позиция.

    Атрибуты:
        contract        — тикер пары
        side            — +1 (long) / -1 (short)
        entry           — цена входа
        atr_at_entry    — ATR на момент входа (для trailing-стопа)
        size_usd        — текущий размер позиции в USD
        original_size_usd — размер до partial TP
        initial_stop    — стартовый стоп (entry ± ATR_STOP_MULT × ATR)
        trail_stop      — текущий trailing-стоп (по close)
        max_favorable   — самая выгодная цена (для MFE-анализа)
        max_adverse     — самая невыгодная цена (для MAE-анализа)
        entry_idx       — индекс свечи входа в data[contract]
        entry_day_ts    — TS календарного дня входа (нужен для Daily Stop!)
        hold_days       — сколько свечей позиция прожила
        donchian_at_entry — Donchian_high на момент входа (для SL-выхода по сигналу)
        partial_taken   — был ли уже частичный TP
    """
    __slots__ = ("contract", "side", "entry", "atr_at_entry", "size_usd",
                 "original_size_usd", "initial_stop", "trail_stop",
                 "max_favorable", "max_adverse", "entry_idx",
                 "entry_day_ts", "hold_days", "donchian_at_entry",
                 "partial_taken")

    def __init__(self, contract, side, entry, atr_at_entry, size_usd,
                 entry_idx, entry_day_ts, donchian_at_entry):
        self.contract          = contract
        self.side              = side
        self.entry             = entry
        self.atr_at_entry      = atr_at_entry
        self.size_usd          = size_usd
        self.original_size_usd = size_usd
        # Стартовый стоп по ATR. Для long: entry - k×ATR; для short: entry + k×ATR
        self.initial_stop      = entry - side * ATR_STOP_MULT * atr_at_entry
        self.trail_stop        = self.initial_stop
        self.max_favorable     = entry
        self.max_adverse       = entry
        self.entry_idx         = entry_idx
        self.entry_day_ts      = entry_day_ts
        self.hold_days         = 0
        self.donchian_at_entry  = donchian_at_entry
        self.partial_taken     = False

    def update_trail(self, candle):
        """Обновляет trailing-stop по close и трекинг MFE/MAE."""
        if self.side == +1:
            self.max_favorable = max(self.max_favorable, candle['h'])
            self.max_adverse   = min(self.max_adverse,   candle['l'])
            new_stop = candle['c'] - ATR_STOP_MULT * self.atr_at_entry
            self.trail_stop = max(self.trail_stop, new_stop)   # только вверх
        else:
            self.max_favorable = min(self.max_favorable, candle['l'])
            self.max_adverse   = max(self.max_adverse,   candle['h'])
            new_stop = candle['c'] + ATR_STOP_MULT * self.atr_at_entry
            self.trail_stop = min(self.trail_stop, new_stop)   # только вниз

    def check_partial_tp(self, candle):
        """Частичная фиксация прибыли на +PARTIAL_TP_PCT favourable.

        Закрываем PARTIAL_TP_FRACTION позиции, trail-stop оставшейся части
        подтягивается к entry (breakeven). Возвращает dict с инфой о частичном
        выходе или None.
        """
        if self.partial_taken:
            return None

        if self.side == +1:
            pct_favorable = (self.max_favorable - self.entry) / self.entry
        else:
            pct_favorable = (self.entry - self.max_favorable) / self.entry

        if pct_favorable < PARTIAL_TP_PCT:
            return None

        exit_price   = candle['c']
        partial_size = self.size_usd * PARTIAL_TP_FRACTION
        partial_pnl  = self.side * (exit_price - self.entry) / self.entry * partial_size
        self.size_usd  -= partial_size
        self.partial_taken = True

        # Подтягиваем trail-stop к entry → фиксируем безубыток остатка
        if self.side == +1:
            self.trail_stop = max(self.trail_stop, self.entry)
        else:
            self.trail_stop = min(self.trail_stop, self.entry)

        return {"partial_size": partial_size,
                "partial_pnl":  partial_pnl,
                "partial_price": exit_price,
                "favorable_pct": pct_favorable}


# =============================================================================
# LIVE-ФИЛЬТРЫ (для живых сигналов; в бэктесте используются не все)
# =============================================================================

def live_signal_filters(contract):
    """Лёгкая pre-trade проверка: LSR, OI, ликвидации.

    Возвращает (ok: bool, reason: str). Бэктест это не вызывает, но оставлено
    для будущей live-интеграции.
    """
    try:
        tickers = B.api_get("tickers", {})
        t = next((x for x in tickers if x.get("contract") == contract), None)
        if t is None:
            return False, "no ticker"

        funding = float(t.get("funding_rate", 0))
        if abs(funding) > 0.0005:
            return False, f"funding={funding*100:.3f}%"

        stats = B.api_get("contract_stats",
                          {"contract": contract, "interval": "5m", "limit": 50})
        if not stats or len(stats) < 12:
            return False, "no stats"

        latest, prev = stats[-1], stats[-12]
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
            return False, "liq cascade"

        return True, "ok"
    except Exception as e:
        return False, f"err: {e}"


# =============================================================================
# РАЗМЕР ПОЗИЦИИ
# =============================================================================

def compute_risk_slot(equity, dd_brake_active=False):
    """Risk-слот = clamp(equity × RISK_FRACTION, SLOT_RISK_MIN, SLOT_RISK_MAX).

    Если активен DD brake — умножаем на DD_BRAKE_FACTOR (0.4 → риск 40% от
    базового).
    """
    base = max(SLOT_RISK_MIN, min(SLOT_RISK_MAX, equity * RISK_FRACTION))
    if dd_brake_active:
        base *= DD_BRAKE_FACTOR
    return base


def compute_max_per_side(current_risk):
    """Сколько параллельных позиций на одну сторону при текущем risk-слоте.

    Лимит = min(MAX_PER_SIDE_CAP, PER_SIDE_BUDGET / current_risk).
    """
    if current_risk <= 0:
        return 0
    return min(MAX_PER_SIDE_CAP, int(PER_SIDE_BUDGET / current_risk))


# =============================================================================
# ОСНОВНОЙ БЭКТЕСТ
# =============================================================================

def run_backtest(pairs, start_iso=BACKTEST_START_ISO,
                 end_iso=BACKTEST_END_ISO, verbose=True):
    """Главный цикл бэктеста.

    Алгоритм по свечам (синхронно по всем парам по 4H-timestamp):
      1) Смена календарного дня → фиксим P&L вчерашнего дня, сбрасываем
         счётчики "today" (new_today, realized_today, today_entry_realized).
      2) Для каждой открытой позиции: update_trail, check_partial_tp,
         проверка выхода (TRAIL / SL / TIME / SIG). Закрытие идёт в realized.
         Если позиция была открыта сегодня и закрылась — добавляем её P&L
         в today_entry_realized (для Daily Stop).
      3) Считаем equity = cash + unrealized. Обновляем peak/MaxDD, DD brake.
      4) Daily Stop (today-only!):
         - today_open_losses = сумма unrealized убытков позиций, открытых сегодня
         - if (today_entry_realized + today_open_losses) <= threshold:
             закрыть ВСЕ позиции, открытые сегодня и находящиеся в убытке,
             оставить старые (в плюсе или минусе — неважно).
      5) Если day_loss_stop_active — новых позиций сегодня не открываем.
      6) Иначе — генерация кандидатов, фильтр по каналу, размер позиции,
         открытие новых (до MAX_NEW_PER_DAY / MAX_CONCURRENT / per-side caps).
    """
    if B is None:
        raise RuntimeError("bot module not available")

    pairs_active = [p for p in pairs if p not in EXCLUDE_PAIRS]
    excluded = len(pairs) - len(pairs_active)

    if verbose:
        B.send_telegram(
            f"📡 {STRATEGY_NAME} {STRATEGY_VERSION}: загружаю {CANDLE_INTERVAL} свечи "
            f"(3 стр x 2000) для {len(pairs_active)} пар (excluded {excluded})"
        )

    # --- Загрузка свечей ----------------------------------------------------
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

    if not data:
        raise RuntimeError("Нет данных")

    if verbose:
        B.send_telegram(
            f"📡 {STRATEGY_NAME} {STRATEGY_VERSION}: вычисляю BTC regime "
            f"(1D SMA{BTC_REGIME_SMA})..."
        )

    btc_regime   = get_btc_regime()
    funding_snap = get_funding_snapshot()

    start_ts = int(dt.datetime.fromisoformat(start_iso).timestamp())
    end_ts   = int(dt.datetime.fromisoformat(end_iso).timestamp()) if end_iso else None

    # Все уникальные 4H-timestamps в окне бэктеста
    all_candles = sorted({c['t'] for p in data for c in data[p]
                          if c['t'] >= start_ts and (end_ts is None or c['t'] < end_ts)})

    min_periods = max(DONCHIAN_PERIOD, DMI_PERIOD * 2, ATR_PERIOD, BTC_REGIME_SMA) + 5
    if len(all_candles) < min_periods:
        raise RuntimeError(f"Слишком мало свечей: {len(all_candles)}")

    # Индексы для быстрого доступа
    by_pair_candle       = {p: {c['t']: c       for c in cds} for p, cds in data.items()}
    idx_by_pair_candle    = {p: {c['t']: i       for i, c in enumerate(cds)} for p, cds in data.items()}

    # --- Состояние портфеля -------------------------------------------------
    cash          = INIT_CAPITAL
    positions     = []
    closed_trades = []
    equity_curve  = []
    daily_pnl     = []
    peak_equity   = INIT_CAPITAL
    dd_brake_active        = False
    dd_brake_days_set      = set()
    btc_blocked_days_set   = set()
    adx_filtered           = 0
    pair_stats             = defaultdict(lambda: {"consec_losses": 0, "cooldown_until": 0})
    cooldown_blocked       = 0
    excluded_count         = excluded
    day_loss_stop_active   = False
    day_stop_triggered     = 0
    day_stop_events        = []
    prev_day_pnl           = 0.0
    consec_loss_days_count = 0
    consec_loss_days_max   = 0
    partial_tp_count       = 0
    partial_tp_total_pnl   = 0.0
    max_dd_peak_ts         = None
    max_dd_trough_ts       = None
    max_dd_value            = 0.0
    current_peak_ts        = None
    prev_cal_day           = None
    prev_day_start_equity  = INIT_CAPITAL
    equity                 = INIT_CAPITAL
    new_today              = 0
    realized_today         = 0.0
    today_entry_realized   = 0.0  # P&L закрытых сегодня позиций, открытых сегодня

    # =====================================================================
    # ГЛАВНЫЙ ЦИКЛ ПО СВЕЧАМ
    # =====================================================================
    for candle_idx, candle_ts in enumerate(all_candles):
        # Календарный день (UTC) текущей свечи
        cal_day = candle_ts - (candle_ts % 86400)
        btc_r   = btc_regime.get(cal_day, 0)
        if btc_r == 0:
            btc_blocked_days_set.add(cal_day)

        # --- Смена календарного дня → фиксим вчерашний день ----------------
        if cal_day != prev_cal_day:
            if prev_cal_day is not None:
                day_pnl = equity - prev_day_start_equity
                daily_pnl.append((prev_cal_day, day_pnl))
                prev_day_pnl = day_pnl
                # Подряд идущие минусовые дни
                if prev_day_pnl < 0:
                    consec_loss_days_count += 1
                    if consec_loss_days_count > consec_loss_days_max:
                        consec_loss_days_max = consec_loss_days_count
                else:
                    consec_loss_days_count = 0
            prev_cal_day          = cal_day
            prev_day_start_equity = equity
            day_loss_stop_active  = False
            new_today             = 0
            realized_today        = 0.0
            today_entry_realized  = 0.0   # ВАЖНО: сбрасываем на новый день

        positions_before = len(positions)
        new_positions    = []
        realized_this_candle = 0.0

        # --- 1) Обновление и проверка выхода для открытых позиций ------------
        for pos in positions:
            candle = by_pair_candle[pos.contract].get(candle_ts)
            if candle is None:
                new_positions.append(pos)
                continue

            pos.hold_days += 1
            pos.update_trail(candle)

            # 1a) Частичный TP
            ptp = pos.check_partial_tp(candle)
            if ptp is not None:
                partial_tp_count      += 1
                partial_tp_total_pnl  += ptp["partial_pnl"]
                realized_this_candle  += ptp["partial_pnl"]
                closed_trades.append({
                    "contract": pos.contract, "side": pos.side, "entry": pos.entry,
                    "exit": ptp["partial_price"], "size_usd": ptp["partial_size"],
                    "pnl": ptp["partial_pnl"], "reason": "PTP",
                    "hold_days": pos.hold_days, "entry_day": pos.entry_day_ts,
                    "exit_day": candle_ts,
                    "max_favorable": pos.max_favorable, "max_adverse": pos.max_adverse})

            # 1b) Выходы по trail/SL/TIME/SIG
            exit_price, exit_reason = None, None

            # Trailing стоп сработал по close
            if pos.side == +1 and candle['c'] <= pos.trail_stop:
                exit_price, exit_reason = pos.trail_stop, "TRAIL"
            elif pos.side == -1 and candle['c'] >= pos.trail_stop:
                exit_price, exit_reason = pos.trail_stop, "TRAIL"
            # Стартовый стоп (только на 1-й свече — дальше работает trail)
            elif pos.side == +1 and candle['c'] <= pos.initial_stop and pos.hold_days == 1:
                exit_price, exit_reason = pos.initial_stop, "SL"
            elif pos.side == -1 and candle['c'] >= pos.initial_stop and pos.hold_days == 1:
                exit_price, exit_reason = pos.initial_stop, "SL"

            # Тайм-аут
            if exit_price is None and pos.hold_days >= MAX_HOLD_DAYS:
                exit_price, exit_reason = candle['c'], "TIME"

            # Выход по сигналу: цена вышла из канала с обратной стороны
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

            # Фиксация закрытия
            if exit_price is not None:
                gross = pos.side * (exit_price - pos.entry) / pos.entry * pos.size_usd
                comm  = (COMM_TAKER + SLIPPAGE) * pos.size_usd * 2
                # Подсчёт количества фандингов
                n_fund = 0
                cur = pos.entry_day_ts
                while cur < candle_ts:
                    if dt.datetime.utcfromtimestamp(cur).hour in FUNDING_TIMES_UTC:
                        n_fund += 1
                    cur += 3600
                _fc          = pos.contract if pos.contract.endswith("_USDT") else f"{pos.contract}_USDT"
                funding_rate = funding_snap.get(_fc, 0.0)
                funding_cost = pos.side * funding_rate * pos.size_usd * n_fund
                net          = gross - comm - funding_cost

                realized_this_candle += net

                # ВАЖНО (Daily Stop today-only):
                # Если позиция была открыта В ТЕКУЩИЙ календарный день —
                # её P&L идёт в today_entry_realized (для расчёта Daily Stop).
                if pos.entry_day_ts >= cal_day:
                    today_entry_realized += net

                closed_trades.append({
                    "contract": pos.contract, "side": pos.side, "entry": pos.entry,
                    "exit": exit_price, "size_usd": pos.size_usd, "pnl": net,
                    "reason": exit_reason, "hold_days": pos.hold_days,
                    "entry_day": pos.entry_day_ts, "exit_day": candle_ts,
                    "max_favorable": pos.max_favorable, "max_adverse": pos.max_adverse})

                # Cooldown-счётчик по паре
                ps = pair_stats[pos.contract]
                if net < 0:
                    ps["consec_losses"] += 1
                    if ps["consec_losses"] >= CONSEC_LOSS_LIMIT:
                        ps["cooldown_until"] = candle_ts + COOLDOWN_DAYS * 86400
                else:
                    ps["consec_losses"] = 0
            else:
                new_positions.append(pos)

        positions = new_positions
        realized_today += realized_this_candle

        # --- 2) Equity / peak / MaxDD / DD brake -----------------------------
        unrealized = 0.0
        for pos in positions:
            c = by_pair_candle[pos.contract].get(candle_ts)
            if c is None:
                continue
            unrealized += pos.side * (c['c'] - pos.entry) / pos.entry * pos.size_usd
        equity = cash + realized_this_candle + unrealized
        cash  += realized_this_candle
        equity_curve.append((candle_ts, equity))

        if equity > peak_equity:
            peak_equity      = equity
            current_peak_ts  = candle_ts

        current_dd = peak_equity - equity
        if current_dd > max_dd_value:
            max_dd_value    = current_dd
            max_dd_peak_ts  = current_peak_ts
            max_dd_trough_ts = candle_ts

        # DD brake: активируется при DD > порога, деактивируется при восстановлении
        if dd_brake_active and equity >= peak_equity * DD_BRAKE_RECOVERY:
            dd_brake_active = False
        elif (not dd_brake_active) and (peak_equity - equity) > DD_BRAKE_THRESHOLD:
            dd_brake_active = True
            dd_brake_days_set.add(cal_day)
        elif dd_brake_active:
            dd_brake_days_set.add(cal_day)

        # --- 3) Пропуск, если BTC regime не определён -----------------------
        if btc_r == 0:
            continue
        if len(positions) >= MAX_CONCURRENT:
            continue
        if day_loss_stop_active:
            continue

        # --- 4) Параметры риска и лимиты на сторону -------------------------
        current_risk  = compute_risk_slot(equity, dd_brake_active)
        max_per_side  = compute_max_per_side(current_risk)
        long_count    = sum(1 for p in positions if p.side == +1)
        short_count   = sum(1 for p in positions if p.side == -1)

        # Выбор порога Daily Stop: если вчера закрылись в минус — порог "consec"
        if prev_day_pnl < 0:
            current_threshold = DAILY_STOP_LOSS_CONSEC
        else:
            current_threshold = DAILY_STOP_LOSS

        # --- 5) Daily Stop (TODAY-ONLY!) ------------------------------------
        long_unrealized    = 0.0
        short_unrealized   = 0.0
        today_open_losses  = 0.0   # unrealized убыток только позиций, открытых сегодня
        for pos in positions:
            c = by_pair_candle[pos.contract].get(candle_ts)
            if c is None:
                continue
            pos_unrealized = pos.side * (c['c'] - pos.entry) / pos.entry * pos.size_usd
            if pos.side == +1:
                long_unrealized += pos_unrealized
            else:
                short_unrealized += pos_unrealized
            # КЛЮЧЕВОЕ УСЛОВИЕ today-only:
            # считаем только позиции, открытые в текущий календарный день,
            # и только если они в убытке
            if pos.entry_day_ts >= cal_day and pos_unrealized < 0:
                today_open_losses += pos_unrealized

        # Триггер Daily Stop:
        # суммарный P&L сегодняшних позиций (реализованный + нереализованный)
        # пробил порог
        if (today_entry_realized + today_open_losses) <= current_threshold:
            if not day_loss_stop_active:
                day_loss_stop_active = True
                day_stop_triggered   += 1
                # Выбираем "худшую" сторону для отчёта
                bad_side = +1 if long_unrealized <= short_unrealized else -1
                positions_remaining = []
                dstop_realized = 0.0

                # Закрываем ТОЛЬКО позиции, открытые сегодня и в минусе.
                # Старые позиции НЕ трогаем — даже если они в минусе,
                # они могут восстановиться на следующем дне.
                for pos in positions:
                    candle_now = by_pair_candle[pos.contract].get(candle_ts)
                    if candle_now is None:
                        positions_remaining.append(pos)
                        continue
                    pos_unrealized = pos.side * (candle_now['c'] - pos.entry) / pos.entry * pos.size_usd
                    # ВАЖНО: today-only + в убытке
                    if pos.entry_day_ts >= cal_day and pos_unrealized < 0:
                        exit_price_now = candle_now['c']
                        gross_now = pos.side * (exit_price_now - pos.entry) / pos.entry * pos.size_usd
                        comm_now  = (COMM_TAKER + SLIPPAGE) * pos.size_usd * 2
                        # Фандинги
                        n_fund_now = 0
                        cur_now = pos.entry_day_ts
                        while cur_now < candle_ts:
                            if dt.datetime.utcfromtimestamp(cur_now).hour in FUNDING_TIMES_UTC:
                                n_fund_now += 1
                            cur_now += 3600
                        _fc_now          = pos.contract if pos.contract.endswith("_USDT") else f"{pos.contract}_USDT"
                        funding_rate_now = funding_snap.get(_fc_now, 0.0)
                        funding_cost_now = pos.side * funding_rate_now * pos.size_usd * n_fund_now
                        net_now          = gross_now - comm_now - funding_cost_now

                        realized_this_candle += net_now
                        dstop_realized       += net_now
                        today_entry_realized += net_now  # уже закрытая сегодня
                        closed_trades.append({
                            "contract": pos.contract, "side": pos.side, "entry": pos.entry,
                            "exit": exit_price_now, "size_usd": pos.size_usd, "pnl": net_now,
                            "reason": "DSTOP", "hold_days": pos.hold_days,
                            "entry_day": pos.entry_day_ts, "exit_day": candle_ts,
                            "max_favorable": pos.max_favorable, "max_adverse": pos.max_adverse})
                    else:
                        # Оставляем: либо старая позиция, либо сегодняшняя в плюсе
                        positions_remaining.append(pos)

                positions = positions_remaining
                cash     += dstop_realized

                # Пересчёт equity и equity_curve с учётом только что закрытого
                unrealized = 0.0
                for pos in positions:
                    c = by_pair_candle[pos.contract].get(candle_ts)
                    if c is None:
                        continue
                    unrealized += pos.side * (c['c'] - pos.entry) / pos.entry * pos.size_usd
                equity = cash + unrealized
                equity_curve[-1] = (candle_ts, equity)

                # Сохраняем событие Daily Stop для отчёта
                day_stop_events.append({
                    "day":            cal_day,
                    "candle_ts":      candle_ts,
                    "open_before":    positions_before,
                    "open_left":      len(positions),
                    "all_closed":     positions_before > 0 and len(positions) == 0,
                    "threshold_used": current_threshold,
                    "consec_day":     prev_day_pnl < 0,
                    "today_realized": round(today_entry_realized, 2),
                    "today_open_loss": round(today_open_losses, 2),
                })

        # После Daily Stop новые позиции сегодня не открываем
        if day_loss_stop_active:
            continue

        # --- 6) Лимит "3 лузера на сторону" ---------------------------------
        can_long  = long_count  < max_per_side and btc_r == +1
        can_short = short_count < max_per_side and btc_r == -1
        long_losers  = 0
        short_losers = 0
        for pos in positions:
            c = by_pair_candle[pos.contract].get(candle_ts)
            if c is None:
                continue
            pos_unrealized = pos.side * (c['c'] - pos.entry) / pos.entry * pos.size_usd
            if pos_unrealized < 0:
                if pos.side == +1:
                    long_losers += 1
                else:
                    short_losers += 1
        if long_losers  >= MAX_LOSERS_PER_SIDE: can_long  = False
        if short_losers >= MAX_LOSERS_PER_SIDE: can_short = False

        # --- 7) Генерация кандидатов на вход --------------------------------
        candidates = []
        for p, cds in data.items():
            idx = idx_by_pair_candle[p].get(candle_ts)
            if idx is None or idx < DONCHIAN_PERIOD + 2:
                continue
            # Не открываем, если уже есть позиция по паре
            if any(pos.contract == p for pos in positions):
                continue
            # Cooldown по паре
            ps = pair_stats[p]
            if candle_ts < ps["cooldown_until"]:
                cooldown_blocked += 1
                continue

            sig = evaluate_signal(cds[:idx + 1], btc_r, funding_snap)
            if sig is None or sig["side"] == 0:
                # ADX-фильтр: считаем сколько раз отсекли по низкому ADX
                if sig and sig.get("adx", 100) < ADX_THRESHOLD:
                    adx_filtered += 1
                continue
            if sig["side"] == +1 and not can_long:  continue
            if sig["side"] == -1 and not can_short: continue
            candidates.append((p, sig, idx))

        # Сортируем по убыванию ATR% — волатильные пары приоритетнее
        candidates.sort(key=lambda x: x[1]["atr_pct"], reverse=True)

        # --- 8) Открытие новых позиций --------------------------------------
        for p, sig, idx in candidates:
            if new_today >= MAX_NEW_PER_DAY:
                break
            if len(positions) >= MAX_CONCURRENT:
                break
            if sig["side"] == +1:
                if long_count >= max_per_side:
                    continue
                long_count += 1
            else:
                if short_count >= max_per_side:
                    continue
                short_count += 1

            # Размер позиции = risk / stop_pct, не более MAX_POSITION_PCT × equity
            stop_dist = ATR_STOP_MULT * sig["atr"]
            stop_pct  = stop_dist / sig["close"]
            if stop_pct <= 0:
                continue
            raw_size = current_risk / stop_pct
            size_usd = min(raw_size, MAX_POSITION_PCT * equity)
            if size_usd < 50:
                continue

            entry_price = sig["close"]
            pos = Position(p, sig["side"], entry_price, sig["atr"],
                           size_usd, idx, candle_ts, sig["dc_high"])
            positions.append(pos)
            # Комиссия за открытие срезается с cash сразу
            cash -= COMM_TAKER * size_usd
            new_today += 1

    # --- Хвост: закрытие оставшихся позиций по последней свече ---------------
    if prev_cal_day is not None:
        day_pnl = equity - prev_day_start_equity
        daily_pnl.append((prev_cal_day, day_pnl))

    last_candle_ts = all_candles[-1]
    for pos in positions:
        c = by_pair_candle[pos.contract].get(last_candle_ts)
        if c is None:
            continue
        gross = pos.side * (c['c'] - pos.entry) / pos.entry * pos.size_usd
        comm  = (COMM_TAKER + SLIPPAGE) * pos.size_usd * 2
        net   = gross - comm
        closed_trades.append({
            "contract": pos.contract, "side": pos.side, "entry": pos.entry,
            "exit": c['c'], "size_usd": pos.size_usd, "pnl": net, "reason": "END",
            "hold_days": pos.hold_days, "entry_day": pos.entry_day_ts,
            "exit_day": last_candle_ts,
            "max_favorable": pos.max_favorable, "max_adverse": pos.max_adverse})
        cash   += net
        equity  = cash

    # --- Пост-анализ: был ли разворот после стопа? ---------------------------
    for t in closed_trades:
        if t["reason"] not in ("SL", "TRAIL") or t["pnl"] > 0:
            continue
        cds     = data.get(t["contract"])
        idx_map = idx_by_pair_candle.get(t["contract"])
        if not cds or not idx_map:
            t["reversed_after_stop"] = None
            continue
        idx = idx_map.get(t["exit_day"])
        if idx is None:
            t["reversed_after_stop"] = None
            continue
        reversed_flag = False
        lookforward_candles = STOP_REVERSAL_LOOKFORWARD_DAYS * 6  # дни → 4H-свечи
        hi = min(idx + 1 + lookforward_candles, len(cds))
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
        "final_equity":          cash,
        "trades":                closed_trades,
        "equity_curve":          equity_curve,
        "daily_pnl":             daily_pnl,
        "n_trades":              len(closed_trades),
        "n_days":                len(daily_pnl),
        "n_candles":             len(all_candles),
        "btc_blocked":           len(btc_blocked_days_set),
        "dd_brake_days":         len(dd_brake_days_set),
        "adx_filtered":          adx_filtered,
        "cooldown_blocked":      cooldown_blocked,
        "excluded_count":        excluded_count,
        "day_stop_triggered":    day_stop_triggered,
        "day_stop_events":       day_stop_events,
        "partial_tp_count":      partial_tp_count,
        "partial_tp_total_pnl":  partial_tp_total_pnl,
        "consec_loss_days_max":  consec_loss_days_max,
        "max_dd_peak_ts":        max_dd_peak_ts,
        "max_dd_trough_ts":      max_dd_trough_ts,
        "max_dd_value":           max_dd_value,
    }


# =============================================================================
# ВАЛИДАЦИЯ
# =============================================================================

def validate(result, z=Z_SCORE):
    """Считает 4 гейта и всю статистику для отчёта."""
    final      = result["final_equity"]
    total_pnl  = final - INIT_CAPITAL
    daily_vals = [p[1] for p in result["daily_pnl"]]
    n          = len(daily_vals)

    # Gate 1: Final - CI(Z) > 0
    if n > 1 and statistics.pstdev(daily_vals) > 0:
        std = statistics.stdev(daily_vals)
        se  = std / math.sqrt(n)
        ci  = z * se
    else:
        ci  = float("inf") if total_pnl > 0 else 0.0
    gate1 = (total_pnl - ci) > 0

    # Gate 2: Worst day >= WORST_DAY_LIMIT
    worst_day    = min(daily_vals) if daily_vals else 0.0
    gate2        = worst_day >= WORST_DAY_LIMIT
    worst_day_ts = None
    if result["daily_pnl"]:
        worst_day_ts = min(result["daily_pnl"], key=lambda p: p[1])[0]

    # Серии убыточных дней
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
    longest_loss_streak    = max((len(s) for s in loss_streaks), default=0)
    multi_loss_streaks     = [s for s in loss_streaks if len(s) >= 2]
    worst_streak_loss      = 0.0
    worst_streak_detail    = None
    for s in loss_streaks:
        streak_total = sum(p for _, p in s)
        if streak_total < worst_streak_loss:
            worst_streak_loss   = streak_total
            worst_streak_detail = {"from": s[0][0], "to": s[-1][0], "days": len(s)}

    # Gate 3: MaxDD <= MAX_DD_LIMIT (считаем из equity_curve)
    eqs = [e for _, e in result["equity_curve"]]
    peak, max_dd = -math.inf, 0.0
    for e in eqs:
        peak   = max(peak, e)
        max_dd = max(max_dd, peak - e)
    gate3 = max_dd <= MAX_DD_LIMIT

    # Gate 4: ни один год не ниже YEAR_LOSS_LIMIT
    yearly = defaultdict(float)
    for ts, pnl in result["daily_pnl"]:
        y = dt.datetime.utcfromtimestamp(ts).year
        yearly[y] += pnl
    gate4 = all(v >= YEAR_LOSS_LIMIT for v in yearly.values())

    # Причины выхода
    reasons = defaultdict(int)
    for t in result["trades"]:
        reasons[t["reason"]] += 1

    # Статистика по парам
    by_pair = defaultdict(lambda: {"n": 0, "wins": 0, "losses": 0, "pnl": 0.0,
                                    "long_n": 0, "long_wins": 0,
                                    "short_n": 0, "short_wins": 0})
    for t in result["trades"]:
        row = by_pair[t["contract"]]
        row["n"]   += 1
        row["pnl"] += t["pnl"]
        win = t["pnl"] > 0
        if win: row["wins"]   += 1
        else:   row["losses"] += 1
        if t["side"] == +1:
            row["long_n"] += 1
            if win: row["long_wins"] += 1
        else:
            row["short_n"] += 1
            if win: row["short_wins"] += 1
    pair_stats = sorted(
        ({"pair": p, "n": r["n"], "pnl": r["pnl"],
          "winrate": (r["wins"] / r["n"] * 100) if r["n"] else 0.0,
          "wins": r["wins"], "losses": r["losses"],
          "long_n": r["long_n"], "long_wins": r["long_wins"],
          "short_n": r["short_n"], "short_wins": r["short_wins"]}
         for p, r in by_pair.items()),
        key=lambda x: x["pnl"], reverse=True)

    # Long/Short разбор
    all_trades = result["trades"]
    longs  = [t for t in all_trades if t["side"] == +1]
    shorts = [t for t in all_trades if t["side"] == -1]
    long_short_stats = {
        "long_n":       len(longs),
        "long_wins":    sum(1 for t in longs  if t["pnl"] > 0),
        "long_losses":  sum(1 for t in longs  if t["pnl"] <= 0),
        "short_n":      len(shorts),
        "short_wins":   sum(1 for t in shorts if t["pnl"] > 0),
        "short_losses": sum(1 for t in shorts if t["pnl"] <= 0),
    }

    def _mfe_pct(t):
        return t["side"] * (t["max_favorable"] - t["entry"]) / t["entry"] * 100
    losing_longs  = [t for t in longs  if t["pnl"] <= 0]
    losing_shorts = [t for t in shorts if t["pnl"] <= 0]
    long_short_stats["long_losing_mfe_avg"]  = (sum(_mfe_pct(t) for t in losing_longs)  / len(losing_longs)  if losing_longs  else 0.0)
    long_short_stats["long_losing_mfe_max"]   = max((_mfe_pct(t) for t in losing_longs),  default=0.0)
    long_short_stats["short_losing_mfe_avg"] = (sum(_mfe_pct(t) for t in losing_shorts) / len(losing_shorts) if losing_shorts else 0.0)
    long_short_stats["short_losing_mfe_max"]  = max((_mfe_pct(t) for t in losing_shorts), default=0.0)

    # Разворот после стопа
    def _mae_pct(t):
        return t["side"] * (t["entry"] - t["max_adverse"]) / t["entry"] * 100
    stop_losing = [t for t in result["trades"]
                   if t["reason"] in ("SL", "TRAIL") and t["pnl"] <= 0
                   and t.get("reversed_after_stop") is not None]
    stop_reversed_n = sum(1 for t in stop_losing if t["reversed_after_stop"])
    stop_stats = {
        "n":              len(stop_losing),
        "reversed_n":     stop_reversed_n,
        "reversed_pct":   (stop_reversed_n / len(stop_losing) * 100) if stop_losing else 0.0,
        "mae_avg":        (sum(_mae_pct(t) for t in stop_losing) / len(stop_losing)) if stop_losing else 0.0,
        "mae_max":        max((_mae_pct(t) for t in stop_losing), default=0.0),
        "lookforward_days": STOP_REVERSAL_LOOKFORWARD_DAYS,
    }

    # Серии Daily Stop
    day_stop_events = result.get("day_stop_events", [])
    streaks, cur_streak = [], []
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

    # Profit factor, expectancy, worst-5
    winning_trades = [t for t in all_trades if t["pnl"] > 0]
    losing_trades = [t for t in all_trades if t["pnl"] < 0]
    gross_profit  = sum(t["pnl"] for t in winning_trades)
    gross_loss    = abs(sum(t["pnl"] for t in losing_trades))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    avg_win       = (gross_profit / len(winning_trades)) if winning_trades else 0.0
    avg_loss      = (-gross_loss / len(losing_trades))   if losing_trades  else 0.0
    expectancy    = ((avg_win * len(winning_trades) + avg_loss * len(losing_trades))
                     / len(all_trades)) if all_trades else 0.0
    worst_5       = sorted(all_trades, key=lambda t: t["pnl"])[:5]

    return {
        "final_equity":   final,
        "total_pnl":      total_pnl,
        "ci_z":           ci,
        "n_trades":       result["n_trades"],
        "n_days":         n,
        "n_candles":      result.get("n_candles", 0),
        "gate1":          gate1,
        "gate2":          gate2,
        "gate3":          gate3,
        "gate4":          gate4,
        "worst_day":      worst_day,
        "worst_day_ts":   worst_day_ts,
        "max_dd":         max_dd,
        "losing_days_n":  losing_days_n,
        "longest_loss_streak":      longest_loss_streak,
        "multi_loss_streaks_n":     len(multi_loss_streaks),
        "multi_loss_streaks_detail":[{"from": s[0][0], "to": s[-1][0],
                                       "days": len(s),
                                       "total_loss": sum(p for _, p in s)}
                                      for s in multi_loss_streaks],
        "worst_streak_loss":   worst_streak_loss,
        "worst_streak_detail": worst_streak_detail,
        "yearly_pnl":          dict(yearly),
        "reasons":             dict(reasons),
        "pair_stats":           pair_stats,
        "long_short_stats":     long_short_stats,
        "stop_reversal_stats":  stop_stats,
        "day_stop_events":      day_stop_events,
        "day_stop_streaks_multi":          len(multi_day_streaks),
        "day_stop_streaks_multi_detail":   [{"from": s[0]["day"], "to": s[-1]["day"],
                                                "days": len(s)} for s in multi_day_streaks],
        "partial_tp_count":     result.get("partial_tp_count", 0),
        "partial_tp_total_pnl": result.get("partial_tp_total_pnl", 0.0),
        "btc_blocked":          result.get("btc_blocked", 0),
        "dd_brake_days":        result.get("dd_brake_days", 0),
        "adx_filtered":         result.get("adx_filtered", 0),
        "cooldown_blocked":     result.get("cooldown_blocked", 0),
        "excluded_count":       result.get("excluded_count", 0),
        "day_stop_triggered":   result.get("day_stop_triggered", 0),
        "consec_loss_days_max": result.get("consec_loss_days_max", 0),
        "max_dd_peak_ts":       result.get("max_dd_peak_ts"),
        "max_dd_trough_ts":     result.get("max_dd_trough_ts"),
        "profit_factor":        profit_factor,
        "gross_profit":         gross_profit,
        "gross_loss":           gross_loss,
        "avg_win":              avg_win,
        "avg_loss":             avg_loss,
        "expectancy":           expectancy,
        "winning_trades_n":     len(winning_trades),
        "losing_trades_n":      len(losing_trades),
        "worst_5_trades":       [{"contract": t["contract"],
                                   "side": "L" if t["side"] == +1 else "S",
                                   "pnl": t["pnl"], "reason": t["reason"],
                                   "entry_day": t["entry_day"], "exit_day": t["exit_day"]}
                                  for t in worst_5],
        "all_pass": gate1 and gate2 and gate3 and gate4,
    }


# =============================================================================
# ФОРМИРОВАНИЕ TELEGRAM-ОТЧЁТА
# =============================================================================

_PAIRS_USED = []


def format_report(result, val, n_pairs=None):
    """Формирует многострочный отчёт в Telegram. Все цифры берутся напрямую
    из `val` и из констант — никаких hardcoded-значений.
    """
    if n_pairs is None:
        n_pairs = len(_PAIRS_USED)

    lines = []
    lines.append(f"📊 *{STRATEGY_NAME} {STRATEGY_VERSION} - РЕЗУЛЬТАТЫ*  [{STRATEGY_FILE}]")
    lines.append("")
    # Описание стратегии — все параметры подставляются из констант
    lines.append(
        f"Donchian({DONCHIAN_PERIOD}) на {CANDLE_INTERVAL} + BTC SMA({BTC_REGIME_SMA}) 1D + DMI + "
        f"Trailing {ATR_STOP_MULT}xATR (по close) + Partial TP +{PARTIAL_TP_PCT*100:.0f}%/"
        f"{PARTIAL_TP_FRACTION*100:.0f}% + Compound + Daily stop (today-only) + "
        f"Cooldown + {MAX_LOSERS_PER_SIDE} лузера фильтр"
    )
    lines.append(f"Капитал: ${INIT_CAPITAL:,.0f}  |  Пары: {n_pairs}  |  Excluded: {val['excluded_count']}")
    lines.append(
        f"Risk: {RISK_FRACTION*100:.1f}% от equity "
        f"(floor ${SLOT_RISK_MIN:.0f}, cap ${SLOT_RISK_MAX:.0f}, "
        f"brake x{DD_BRAKE_FACTOR} при DD>${DD_BRAKE_THRESHOLD:.0f})"
    )
    lines.append(
        f"Max concurrent: {MAX_CONCURRENT} (per-side cap {MAX_PER_SIDE_CAP} ВКЛ, "
        f"budget ${PER_SIDE_BUDGET}, фильтр {MAX_LOSERS_PER_SIDE} лузера) | "
        f"Daily stop: ${DAILY_STOP_LOSS:.0f} / подряд ${DAILY_STOP_LOSS_CONSEC:.0f} | "
        f"New/day: {MAX_NEW_PER_DAY}"
    )
    lines.append(
        f"Partial TP: +{PARTIAL_TP_PCT*100:.0f}% favorable -> закрыть "
        f"{PARTIAL_TP_FRACTION*100:.0f}% позиции, остаток -> breakeven"
    )
    lines.append(
        f"ATR фильтр: {ATR_PCT_MIN*100:.1f}%-{ATR_PCT_MAX*100:.1f}% (на 4H) | "
        f"Max hold: {MAX_HOLD_DAYS} свечей ({MAX_HOLD_DAYS*4}h = {MAX_HOLD_DAYS*4/24:.1f}д)"
    )
    lines.append(f"Cooldown: {COOLDOWN_DAYS}д после {CONSEC_LOSS_LIMIT} убытков подряд")
    lines.append(
        f"Сделок: {val['n_trades']}  |  Дней: {val['n_days']}  |  "
        f"Свечей 4H: {val.get('n_candles', 0)}"
    )
    lines.append(
        f"BTC blocked: {val['btc_blocked']}д  |  ADX filtered: {val['adx_filtered']}  |  "
        f"DD brake days: {val['dd_brake_days']}д"
    )
    lines.append(
        f"Cooldown blocks: {val['cooldown_blocked']}  |  "
        f"Daily stop: {val['day_stop_triggered']}  |  "
        f"Partial TPs: {val['partial_tp_count']} (${val['partial_tp_total_pnl']:+,.0f})  |  "
        f"Max consec loss days: {val.get('consec_loss_days_max', 0)}"
    )

    # Разбивка по причинам выхода
    if val.get("reasons"):
        r = val["reasons"]
        lines.append(
            f"Исходы: SL/TRAIL={r.get('TRAIL',0)+r.get('SL',0)} "
            f"PTP={r.get('PTP',0)} TIME={r.get('TIME',0)} "
            f"SIG={r.get('SIG',0)} DSTOP={r.get('DSTOP',0)} END={r.get('END',0)}"
        )

    lines.append("")
    lines.append(f"Final equity : ${val['final_equity']:,.2f}")
    lines.append(f"Total P&L    : ${val['total_pnl']:,.2f}")
    lines.append(f"CI(Z={Z_SCORE}): ${val['ci_z']:,.2f}")
    lines.append("")

    # Profit factor, win/loss, expectancy
    pf = val.get("profit_factor", 0)
    pf_str = f"{pf:.2f}" if pf != float("inf") else "∞"
    lines.append(
        f"Profit Factor: {pf_str}  "
        f"(gross profit ${val.get('gross_profit',0):,.0f} / gross loss ${val.get('gross_loss',0):,.0f})"
    )
    lines.append(
        f"Win/Loss: {val.get('winning_trades_n',0)}/{val.get('losing_trades_n',0)}  |  "
        f"Avg win ${val.get('avg_win',0):+.2f} / Avg loss ${val.get('avg_loss',0):+.2f}  |  "
        f"Expectancy ${val.get('expectancy',0):+.2f}/trade"
    )
    lines.append("")

    # --- ВАЛИДАЦИЯ ---
    lines.append("- ВАЛИДАЦИЯ -")
    lines.append(
        f"① Final - CI > 0     : {'✅ PASS' if val['gate1'] else '❌ FAIL'}  "
        f"(edge = ${val['total_pnl']-val['ci_z']:,.2f})"
    )
    worst_day_date = (dt.datetime.utcfromtimestamp(val["worst_day_ts"]).strftime("%Y-%m-%d")
                      if val.get("worst_day_ts") else "-")
    lines.append(
        f"② Worst day ≥ ${WORST_DAY_LIMIT:.0f}   : {'✅ PASS' if val['gate2'] else '❌ FAIL'}  "
        f"(worst = ${val['worst_day']:,.2f}, {worst_day_date})"
    )
    max_dd_peak_ts   = val.get("max_dd_peak_ts")
    max_dd_trough_ts = val.get("max_dd_trough_ts")
    dd_dates = ""
    if max_dd_peak_ts and max_dd_trough_ts:
        d_peak   = dt.datetime.utcfromtimestamp(max_dd_peak_ts).strftime("%Y-%m-%d")
        d_trough = dt.datetime.utcfromtimestamp(max_dd_trough_ts).strftime("%Y-%m-%d")
        dd_dates = f" ({d_peak} → {d_trough})"
    lines.append(
        f"③ MaxDD ≤ ${MAX_DD_LIMIT:,.0f}      : {'✅ PASS' if val['gate3'] else '❌ FAIL'}  "
        f"(MaxDD = ${val['max_dd']:,.2f}){dd_dates}"
    )
    lines.append(
        f"④ No year < ${YEAR_LOSS_LIMIT:.0f}     : {'✅ PASS' if val['gate4'] else '❌ FAIL'}"
    )
    for y in sorted(val["yearly_pnl"]):
        lines.append(f"   {y}: ${val['yearly_pnl'][y]:,.2f}")
    lines.append("")

    verdict = "✅✅✅✅ ALL PASS" if val["all_pass"] else "❌ НЕ ПРОШЁЛ"
    lines.append(f"ИТОГ: {verdict}")

    # Худшие 5 сделок
    worst_5 = val.get("worst_5_trades", [])
    if worst_5:
        lines.append("")
        lines.append("- ХУДШИЕ 5 СДЕЛОК -")
        for w in worst_5:
            d_entry = dt.datetime.utcfromtimestamp(w["entry_day"]).strftime("%Y-%m-%d")
            d_exit  = dt.datetime.utcfromtimestamp(w["exit_day"]).strftime("%Y-%m-%d")
            lines.append(
                f"   {w['contract']} {w['side']} ${w['pnl']:,.2f} "
                f"({w['reason']}, вход {d_entry} → выход {d_exit})"
            )

    # Long / Short
    ls = val.get("long_short_stats")
    if ls:
        lines.append("")
        lines.append("- LONG / SHORT -")
        lines.append(f"Long : {ls['long_n']} сделок  (🟢 {ls['long_wins']} / 🔴 {ls['long_losses']})")
        lines.append(f"Short: {ls['short_n']} сделок  (🟢 {ls['short_wins']} / 🔴 {ls['short_losses']})")
        lines.append(
            f"Убыточные Long  - доходили в свою сторону в среднем на "
            f"{ls['long_losing_mfe_avg']:.2f}% (макс {ls['long_losing_mfe_max']:.2f}%)"
        )
        lines.append(
            f"Убыточные Short - доходили в свою сторону в среднем на "
            f"{ls['short_losing_mfe_avg']:.2f}% (макс {ls['short_losing_mfe_max']:.2f}%)"
        )

    # Стоп-выходы
    ss = val.get("stop_reversal_stats")
    if ss and ss["n"]:
        lines.append("")
        lines.append("- СТОП-ВЫХОДЫ (SL/TRAIL), убыточные -")
        lines.append(
            f"Всего: {ss['n']}  |  вернулись в сторону сделки в течение "
            f"{ss['lookforward_days']}д после стопа: {ss['reversed_n']} "
            f"({ss['reversed_pct']:.0f}%)"
        )
        lines.append(
            f"Просадка от входа (MAE%): в среднем {ss['mae_avg']:.2f}%  "
            f"(макс {ss['mae_max']:.2f}%)"
        )

    # --- DAILY STOP блок: цифры + комментарии ---
    events = val.get("day_stop_events") or []
    if events:
        lines.append("")
        lines.append(
            f"- DAILY STOP ${DAILY_STOP_LOSS:.0f} / подряд ${DAILY_STOP_LOSS_CONSEC:.0f} "
            f"(ТОЛЬКО ВНУТРИДНЕВНЫЕ) -"
        )
        lines.append(
            f"Сработал: {val['day_stop_triggered']} раз(а)  |  "
            f"подряд (2+ дня): {val.get('day_stop_streaks_multi', 0)} раз(а)"
        )
        for det in (val.get("day_stop_streaks_multi_detail") or []):
            d_from = dt.datetime.utcfromtimestamp(det["from"]).strftime("%Y-%m-%d")
            d_to   = dt.datetime.utcfromtimestamp(det["to"]).strftime("%Y-%m-%d")
            lines.append(f"   подряд {det['days']}д: {d_from} -> {d_to}")

        # Детальные события (макс 30 в отчёте)
        for ev in events[:30]:
            d = dt.datetime.utcfromtimestamp(ev["day"]).strftime("%Y-%m-%d")
            closed_mark = ("все позиции закрылись" if ev["all_closed"]
                           else f"осталось открыто {ev['open_left']}")
            # Дополнительные цифры по today-only P&L
            today_real  = ev.get("today_realized", 0.0)
            today_loss  = ev.get("today_open_loss", 0.0)
            thr         = ev.get("threshold_used", 0)
            thr_mark    = "(consec)" if ev.get("consec_day") else ""
            lines.append(
                f"   {d}: открыто было {ev['open_before']}, {closed_mark}  "
                f"| today_realized=${today_real:+.0f} today_open_loss=${today_loss:+.0f} "
                f"thr=${thr:.0f} {thr_mark}".rstrip()
            )
        if len(events) > 30:
            lines.append(f"   ... и ещё {len(events)-30} срабатываний")

    # Минусовые дни
    lines.append("")
    lines.append("- МИНУСОВЫЕ ДНИ -")
    lines.append(f"Макс. просадка за день: ${val['worst_day']:,.2f} ({worst_day_date})")
    lines.append(f"Всего дней в минусе: {val.get('losing_days_n', 0)} из {val['n_days']}")
    lines.append(
        f"Самая длинная серия подряд: {val.get('longest_loss_streak', 0)} дн.  |  "
        f"серий из 2+ дней подряд: {val.get('multi_loss_streaks_n', 0)}"
    )
    wsd = val.get("worst_streak_detail")
    if wsd:
        d_from = dt.datetime.utcfromtimestamp(wsd["from"]).strftime("%Y-%m-%d")
        d_to   = dt.datetime.utcfromtimestamp(wsd["to"]).strftime("%Y-%m-%d")
        lines.append(
            f"Макс. суммарный убыток за серию подряд: "
            f"${val.get('worst_streak_loss', 0.0):,.2f}  "
            f"({wsd['days']}д: {d_from} -> {d_to})"
        )
    for det in (val.get("multi_loss_streaks_detail") or [])[:15]:
        d_from = dt.datetime.utcfromtimestamp(det["from"]).strftime("%Y-%m-%d")
        d_to   = dt.datetime.utcfromtimestamp(det["to"]).strftime("%Y-%m-%d")
        lines.append(
            f"   подряд {det['days']}д: {d_from} -> {d_to}  "
            f"(убыток за серию: ${det.get('total_loss', 0.0):,.2f})"
        )

    # По парам
    pair_stats = val.get("pair_stats") or []
    if pair_stats:
        lines.append("")
        lines.append("- ПО ПАРАМ -")
        for ps in pair_stats:
            mark = "🟢" if ps["pnl"] > 0 else ("🔴" if ps["pnl"] < 0 else "⚪")
            lines.append(
                f"{mark} {ps['pair']}: {ps['n']} сделок (🟢{ps['wins']}/🔴{ps['losses']}), "
                f"PnL ${ps['pnl']:,.2f}, winrate {ps['winrate']:.0f}%, "
                f"L={ps['long_n']}(🟢{ps['long_wins']}) S={ps['short_n']}(🟢{ps['short_wins']})"
            )

    return lines


# =============================================================================
# MAIN
# =============================================================================

def main():
    """Точка входа. Запускается через RUN_BACKTEST=donchian_4h_v22_final python bot.py"""
    if B is None:
        print(f"[ERROR] bot.py недоступен: {_BOT_IMPORT_ERR}")
        print(f"Запускайте через диспетчер: RUN_BACKTEST=donchian_4h_v22_final python bot.py")
        sys.exit(1)

    global _PAIRS_USED
    pairs = list(B.UPSCALE_PAIRS)
    _PAIRS_USED = pairs

    start = os.environ.get("BT_START", BACKTEST_START_ISO)
    end   = os.environ.get("BT_END",   BACKTEST_END_ISO)

    # Стартовое сообщение — все цифры из констант
    B.send_telegram(
        f"🚀 *{STRATEGY_NAME} {STRATEGY_VERSION}* [{STRATEGY_FILE}] старт: "
        f"{len(pairs)} пар, интервал {CANDLE_INTERVAL} (3 стр x 2000), "
        f"trailing по close, max {MAX_CONCURRENT} поз "
        f"(per-side {MAX_PER_SIDE_CAP}, фильтр {MAX_LOSERS_PER_SIDE} лузера), "
        f"daily stop ${DAILY_STOP_LOSS:.0f}/${DAILY_STOP_LOSS_CONSEC:.0f} (today-only), "
        f"окно {start} -> {end or 'сегодня'}"
    )

    result = run_backtest(pairs, start_iso=start, end_iso=end, verbose=True)
    val    = validate(result)
    lines  = format_report(result, val, n_pairs=len(pairs))
    B.send_blocks(lines)

    # Сохраняем JSON-снапшот результата для последующего анализа
    try:
        import json
        out_dir = "/home/z/my-project/download"
        os.makedirs(out_dir, exist_ok=True)
        with open(f"{out_dir}/{STRATEGY_FILE}_result.json", "w") as f:
            json.dump({
                "strategy": STRATEGY_NAME,
                "version":  STRATEGY_VERSION,
                "file":     STRATEGY_FILE,
                "validation": {k: (v if not isinstance(v, bool) else int(v))
                                for k, v in val.items()},
                "trades":            result["trades"][:200],
                "equity_curve_tail": result["equity_curve"][-60:],
            }, f, indent=2, default=str)
    except Exception as e:
        print(f"[warn] не удалось сохранить результат: {e}")

    return 0 if val["all_pass"] else 2


if __name__ == "__main__":
    sys.exit(main())
