# -*- coding: utf-8 -*-
"""
bt_donchian_regime_v48.py - v4.8.1: Prop-Safe + Partial TP (+8%) / Breakeven stop
====================================================================

Запуск через диспетчер:
    RUN_BACKTEST=donchian_regime_v48 python bot.py

v4.8.1: MAX_CONCURRENT возвращено к 6 (8 дало перебор MaxDD).
Остальные параметры v4.8 сохранены (3 входа/день, ATR 6%, 12 exclude).
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

# --- Капитал и риск (v4.8: compound с floor $80) ---
INIT_CAPITAL     = 10_000.0
RISK_FRACTION    = 0.008      # v4.8: 0.8% от equity (risk $80)
SLOT_RISK_MIN    = 80.0       # v4.8: floor $80
SLOT_RISK_MAX    = 200.0      # ceiling $200
MAX_POSITION_PCT = 0.20

# --- DD brake ---
DD_BRAKE_THRESHOLD = 700.0  # v4.8: 7% для проп-счёта (лимит 10% = $1,000)
DD_BRAKE_FACTOR    = 0.5
DD_BRAKE_RECOVERY  = 0.95

# --- v4.5: total limits (PerSide cap УБРАН) ---
MAX_CONCURRENT     = 6       # v4.8.1: возвращено с 8 к 6 (8 дало перебор MaxDD)
MAX_PER_SIDE_CAP   = 6       # v4.8.1: синхрон с MAX_CONCURRENT
PER_SIDE_BUDGET    = 999999  # v4.5: огромное число, per-side не ограничивает
DAILY_STOP_LOSS            = -300.0  # v4.7-risk100-v4: базовый порог (1-й минусовой день)
DAILY_STOP_LOSS_CONSEC     = -100.0  # v4.7-risk100-v4: 2-й минусовой день подряд -> порог -$100

# --- v4.4: Exclude + Cooldown (как в v4.3) ---
# FIX: UPSCALE_PAIRS в bot.py - голые тикеры ("TRX", без _USDT),
# поэтому EXCLUDE_PAIRS тоже должен быть без суффикса - иначе
# "p not in EXCLUDE_PAIRS" никогда не сработает.
EXCLUDE_PAIRS = {
