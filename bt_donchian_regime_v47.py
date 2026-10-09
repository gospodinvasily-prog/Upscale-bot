# -*- coding: utf-8 -*-
"""
bt_donchian_4h_v16.py - Donchian Breakout на 4H (v1.6: 2× больше позиций)
=========================================================================
ЗАПУСК: RUN_BACKTEST=donchian_4h_v16 python bot.py

=== TEST BUILD v1.6 (4H Donchian) ===
Основа: v1.5 (4H Donchian, ALL PASS, P&L $9,347, но 2026 год провалился до $447)

ЗАДАЧА v1.6: выжать максимум при сохранении ALL PASS.
  - В v1.5 worst day = -$293, лимит -$500 → есть $207 запаса
  - В v1.5 2026 год = $447 (близко к лимиту -$500)
  - В v1.3 2026 год = $7,484 (PerSide cap выключен, до 4 в сторону)
  - Цель: вернуть 2026 к $3-5K, оставив worst day ≤ -$450

ИЗМЕНЕНИЯ vs v1.5 (4 ПРАВКИ — 2× больше позиций в сторону):
  ① MAX_PER_SIDE_CAP = 6 (было 3) — 2× больше в каждую сторону
  ② MAX_CONCURRENT = 10 (было 5) — больше параллельности
  ③ PER_SIDE_BUDGET = 2000 (было 1000) — cap=6 держится при росте equity
  ④ MAX_NEW_PER_DAY = 10 (было 6) — больше входов в день

ЧТО ОСТАЛОСЬ КАК В v1.5 (БЕЗ ИЗМЕНЕНИЙ):
  - DAILY_STOP_LOSS = -450 (главный предохранитель)
  - DAILY_STOP_LOSS_CONSEC = -300
  - DD_BRAKE_THRESHOLD = 900, DD_BRAKE_FACTOR = 0.4
  - Trailing по close, ATR_STOP_MULT = 4.5
  - Пагинация 3 страницы × 2000 свечей
  - Compound sizing $80-$200 (0.8% от equity)
  - Partial TP +8%/50% + breakeven
  - EXCLUDE_PAIRS 14 пар
  - Сортировка по ATR%
  - 3 метрики: MaxDD dates, Profit Factor, Worst 5 trades

ЛОГИКА:
  - Worst day в v1.5 был -$293, увеличиваем позиции в 2× → ожидаем ~-$586
  - Но DAILY_STOP_LOSS=-450 сработает раньше и закроет убыточные позиции
  - Реальный worst day будет ~-$440/-$470 (близко к лимиту, но PASS)
  - 2026 год вернётся к $3-5K (больше позиций в трендах = больше прибыли)
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


# ===== КОНСТАНТЫ =====
STRATEGY_NAME    = "Donchian 4H"
STRATEGY_VERSION = "v1.6-TEST"
STRATEGY_FILE    = "bt_donchian_4h_v16"

INIT_CAPITAL     = 10_000.0
RISK_FRACTION    = 0.008
SLOT_RISK_MIN    = 80.0
SLOT_RISK_MAX    = 200.0
MAX_POSITION_PCT = 0.20

DD_BRAKE_THRESHOLD = 900.0
DD_BRAKE_FACTOR    = 0.4
DD_BRAKE_RECOVERY  = 0.85

# v1.6 правки ①②③④: 2× больше позиций
MAX_CONCURRENT     = 10        # было 5 → стало 10
MAX_PER_SIDE_CAP   = 6          # было 3 → стало 6 (2× в каждую сторону)
PER_SIDE_BUDGET    = 2000      # было 1000 → стало 2000 (cap=6 держится при equity $25K+)
MAX_NEW_PER_DAY    = 10         # было 6 → стало 10 (больше входов в день)

DAILY_STOP_LOSS            = -450.0
DAILY_STOP_LOSS_CONSEC     = -300.0

EXCLUDE_PAIRS = {
    "TRX", "XLM", "BNB", "UNI",
    "LTC", "RUNE", "PENDLE", "HBAR",
    "KAIA", "STX", "IOTA", "ARB", "GRT", "CRV",
}
CONSEC_LOSS_LIMIT = 4
COOLDOWN_DAYS     = 14

CANDLE_INTERVAL  = "4h"
DONCHIAN_PERIOD  = 20
BTC_REGIME_SMA   = 50
DMI_PERIOD       = 14
ADX_THRESHOLD    = 20.0
ATR_PERIOD       = 14
ATR_PCT_MIN      = 0.006
ATR_PCT_MAX      = 0.020

ATR_STOP_MULT    = 4.5
MAX_HOLD_DAYS    = 35

PARTIAL_TP_PCT      = 0.08
PARTIAL_TP_FRACTION = 0.50

STOP_REVERSAL_LOOKFORWARD_DAYS = 15

COMM_TAKER       = 0.0005
SLIPPAGE         = 0.0002
FUNDING_TIMES_UTC = (0, 8, 16)

Z_SCORE          = 2.64
WORST_DAY_LIMIT  = -500.0
MAX_DD_LIMIT     = 2_000.0
YEAR_LOSS_LIMIT  = -500.0

BTC_CONTRACT      = "BTC_USDT"
BACKTEST_START_ISO = "2023-01-01"
BACKTEST_END_ISO   = ""
