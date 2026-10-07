
"""
Upscale Bot v8.1 — воронка (Gate.io USDT-фьючерсы):
  ⏳ ЗАРЯД   — сжатие, объём/OI растут, цена стоит (ДО движения): альты на 1h (окно 12ч), BTC на 1h.
               Главное сообщение: в нём готовые ордера Stop Market, которые ставятся ЗАРАНЕЕ.
  ⚡ ПРОБОЙ  — подтверждение: закрытие 1м свечи за уровнем на объёме ≥2× (проверка каждые 20 сек)
  🚀 ИМПУЛЬС — RS Momentum на 5м, только оценка 🟢 8+ (страховка для движений без заряда)

Сделки шлём только в окнах 10:00–11:30 и 14:30–21:00 МСК; вне окон бот работает молча (копит заряды).
В CSV попадают только реально отправленные сигналы; бот сам считает исход (TP1/стоп первым, TP2 до стопа).
Сводка и файлы — в 23:15 МСК.
Зависимости: только requests. Переменные окружения: TELEGRAM_TOKEN, LOG_DIR (необязательно).
"""

import os
import re
import csv
import signal as _signal
import html
import math
import time
import threading
import traceback
import json
import requests
import upscale_exec
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

# ─── CONFIG ───────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID        = "426470592"
BOT_VERSION    = "v10.4"

TRADING_START_MSK = 4          # окно УКЛОНА начинается в 4:00,
                               # а вне часов работы скана нет вовсе, значит первый час УКЛОН не работал
TRADING_END_MSK   = 23         # v9.11: последнее окно кончается в 23:00
# Окна, в которые бот ШЛЁТ сделки (⚡ПРОБОЙ и 🚀ИМПУЛЬС). Вне окон работает молча:
# заряды копятся, watchlist живёт, но сигналы не создаются и в csv не пишутся.
SIGNAL_WINDOWS    = [(4, 0, 23, 0)]   # v9.12: одно окно 04:00–23:00 без разрывов.
                                      # Бэктест: все 24 часа прибыльны, ни одного убыточного;
                                      # разрывы 08:30-10:30 и 11:30-14:30 давали качество выше
                                      # (+0.564R против +0.512R), но суммарно меньше (+248R
                                      # против +392R). Берём непрерывное окно ради количества.
SUMMARY_HHMM      = (23, 15)   # v9.11: сводка сразу после последнего окна (23:00)

# ── Риск на сделку (Upscale: счёт $5000, лимит −$150 в день и −6% = −$300 всего) ──
# v8.4: риск $20 (счёт $10k: профитный день = +0.5% = $50, при риске $5 это +10R); потолок позиции $3000.
# Лимиты Upscale Basic $10k: дневная −5% ($500), максимальная −10% ($1000).
ACCOUNT_USD   = float(os.environ.get("ACCOUNT_USD", "10000"))
RISK_USD      = float(os.environ.get("RISK_USD", "20"))     # сколько теряем, если сработал стоп
MAX_POS_USD   = float(os.environ.get("MAX_POS_USD", "3000"))  # потолок размера позиции
DAY_LOSS_USD  = float(os.environ.get("DAY_LOSS_USD", "150"))  # дневной лимит пропфёрма


# ── 🎯 УКЛОН — вход внутри диапазона заряда по направлению уклона ──────────────
# Сигнал появляется сразу при нахождении заряда (без ожидания пробоя).
# Окно: 4:00–21:00 МСК, кроме 12:00–14:30 (вне него не шлём, но и не копим — не ставим ордера).
BREAKOUT_ENABLED   = False     # v9.1: ПРОБОЙ отдельной сделкой ОТКЛЮЧЁН. Бэктест 60 дней,
                               # 1144 сделки: -0.21R, и ни один из ~30 проверенных вариантов
                               # (окна, объём, сила, цели, удалённость входа, деление позиции)
                               # не вывел в плюс. Пробой остаётся только третьей целью УКЛОНА.
TILT_ENABLED       = True
TILT_MIN_SCORE     = 0         # v9.1: порог УБРАН. Бэктест 60 дней: с порогом 7 — 105 сделок
                               # и +0.264R, без порога — 228 сделок и +0.294R. Фильтр выбрасывал
                               # половину хороших сделок, не улучшая качество. Остаются два
                               # условия: чёткий уклон (не "both") и ход до границы ≥ TILT_MIN_DIST_PCT.
TILT_MIN_DIST_PCT  = 1.5       # v9.9: было 1.0. Бэктест 60 дней — главное не качество
                               # сделки, а размер ПАЧКИ: при 1.0% максимум 14 позиций
                               # в одну сторону за один скан, случаев по 4+ — 77 штук.
                               # При 1.5% максимум 10, случаев 25. Пачка это одна ставка:
                               # при развороте выбивает все разом, а 14 позиций по $20
                               # закрывают дневной лимит $500.
                               # Качество тоже выше: +0.518R против +0.328R, ВР 88% против 81%.
                               # Сделок меньше (602 против 1440 за 60 дн), но 10 в день хватает.
                               # Вариант 2.0% ещё лучше (+0.628R, пачки до 5), но там
                               # 4 сделки в день — для челленджа медленно.
TILT_STOP_PCT      = 1.0       # v8.9: было 0.5. Бэктест 60 дней, 106 сделок: при реальном
                               # проскальзывании (0.25%) тесный стоп уходит в минус (-0.03R),
                               # потому что стоп бот ставит от цены СИГНАЛА, а входит хуже —
                               # дистанция до стопа растёт. При 1% выходит +0.07R.
                               # Побочно: позиция перестаёт упираться в потолок $3000,
                               # и риск становится полные $20 вместо $15.
TILT_TP1_PCT       = 1.0       # v9.0: ВЕРНУЛИ 1.0 (в v8.9 ошибочно поставил 0.5).
                               # Правка делалась по прогону, где стоп откладывался от
                               # фактического входа, а не от цены сигнала. После починки
                               # порядок перевернулся: 1.0% даёт +0.150R против +0.102R у 0.5%.
                               # 0.75% практически вровень (+0.143R), разница в пределах шума.
TILT_TP3_PCT       = 2.0       # v9.1: третья цель ЗА границей коридора. Пробой отдельной
                               # сделкой убыточен (1144 сд, -0.21R), но как продолжение уклона
                               # работает: три цели по трети позиции дали +0.294R против +0.177R
                               # у двух. Значение 2.0 — середина сетки (1.5-4.0 дали почти одно
                               # и то же), берём не край, чтобы не подгонять.
TILT_TP2_MARGIN    = 0.0       # v9.0: было 0.2. Тренд в бэктесте чистый — чем ближе к
                               # границе, тем лучше: 0.0% → +0.122R, 0.2% → +0.102R,
                               # 0.5% → +0.089R. Ставим точно на границу коридора.
TILT_WINDOWS       = [(4, 0, 23, 0)]  # v9.12: то же единое окно, что и у сигналов
# Защита повтора: если уже был стоп в то же направление по монете — не входим,
# пока цена не вышла за стоп (т.е. не прошла дальше и не дала новый шанс).
TILT_LAST_STOP = {}   # {"SYM:long": stop_price, "SYM:short": stop_price}

# ── v8.3: правила отбора сделок (проверены на 90 днях и 103 парах) ──
# Без них: 62 сделки в день, винрейт 68%, просадка −146%.
# С ними:  4.6 сделки в день, винрейт 78%, просадка −4%, худший день −2.8%.
VWAP_MAX_ATR       = 2.5   # фильтр VWAP пары — КАК В РАБОЧЕЙ ВЕРСИИ: VWAP от 00:00 МСК, ATR 15-минуток.
# Переключатель PAIR_VWAP_MODE=aligned включает способ, как считает бэктест bt_btc: скользящий
# VWAP по 97 закрытым 15м свечам и ATR часового заряда, предел VWAP_ALIGNED_MAX_ATR (2.0).
# По умолчанию legacy — поведение не меняется. Единицы у двух способов разные: 2.5 в legacy
# примерно соответствует 5 в aligned, поэтому числа между режимами не переносить.
PAIR_VWAP_MODE       = os.environ.get("PAIR_VWAP_MODE", "legacy")        # legacy | aligned
VWAP_ALIGNED_MAX_ATR = float(os.environ.get("VWAP_ALIGNED_MAX_ATR", "2.0"))
DAILY_MAX_SIGNALS  = 0     # v9.5: ВЫКЛЮЧЕНО (было 15). В бэктесте (+0.32R на 818 сделках)
                           # лимита не было, а ориентир теперь — денежный риск, а не счётчик.
                           # Защита по просадке в исполнителе остаётся: стоп входов при −$300,
                           # аварийное закрытие при −$400.
ONE_PER_SYMBOL_DAY = True  # одна монета — одна сделка в день
MAX_SAME_SIDE_30M  = 5     # v9.6: было 2. Правило писалось под ПРОБОЙ. У УКЛОНА заряды
                           # находятся пачкой раз в 30 мин по природе скана, и одна сторона
                           # у них потому, что рынок разворачивается. На журнале за 30.09
                           # порог 2 съедал 11 сделок из 29 (38%). Совсем снимать не стали:
                           # пять шортов разом — это одна ставка на $100 риска, и при
                           # отскоке выбьет все сразу (бэктест этого не видит, он считает
                           # сделки независимыми).
DAY_STOP_LOSSES    = 5     # после 5 закрытых убытков за день бот замолкает до завтра
                           # (5 × риск $20 = $100 из дневного лимита $150)
MSK = timezone(timedelta(hours=3))

GATE = "https://api.gateio.ws/api/v4/futures/usdt"

# ── Сеть ──
API_RATE_PER_SEC = 12      # общий лимит запросов к Gate (с запасом к публичному лимиту)
SCAN_WORKERS     = 8       # параллельных потоков в основном скане

# ── v8.1: таймфрейм ЗАРЯДа и ПРОБОЯ ──
# Все окна ниже заданы в свечах, поэтому при смене таймфрейма растягиваются автоматически.
# "5m" = как в v8.0 (узкие диапазоны, маленькие цели); "15m" = диапазоны и цели шире; "1h" — ещё шире, сигналов мало.
# v8.8: вернули "1h" — он подтверждён бэктестом v8.3 (90 дней, 103 пары), а 15м был только
# догадкой по одному графику. Меняем за раз одну вещь: в v8.8 правим потолок размаха
# (он подтверждён замерами), таймфрейм оставляем прежним. Проверить 15м отдельно можно
# переменной окружения CHARGE_TF=15m — все окна ниже заданы в ЧАСАХ и пересчитаются сами,
# формула потолка тоже не зависит от TF (ATR 15м × √48 = ATR 1ч × √12).
CHARGE_TF        = os.environ.get("CHARGE_TF", "1h").strip()
TF_MIN           = {"5m": 5, "15m": 15, "30m": 30, "1h": 60}[CHARGE_TF]
REF_TF_MIN       = 60          # таймфрейм, на котором откалиброваны пороги ниже (бэктест v8.3)
ATR_TF_SCALE     = (REF_TF_MIN / TF_MIN) ** 0.5   # 1.0 на 1h; на 15m ≈2.0 — компенсирует меньший ATR свечи
MOMENTUM_ENABLED   = False     # 🚀 ИМПУЛЬС выключен — торгуем только ЗАРЯД → ПРОБОЙ
MOMENTUM_MIN_SCORE = 8         # слать только 🟢 Сильный (8+); 5 — ещё и 🟡 Нормальный

def fmt_minutes(m: float) -> str:
    """180 → «3ч», 90 → «1.5ч», 30 → «30м»."""
    if m >= 2880:
        d = m / 1440
        return f"{d:.0f} дн" if d == int(d) else f"{d:.1f} дн"
    if m >= 60:
        h = m / 60
        return f"{h:.0f}ч" if h == int(h) else f"{h:.1f}ч"
    return f"{m:.0f}м"

# ── RS Momentum (ИМПУЛЬС) ──
# v9.1: скан зарядов раз в CHARGE_SCAN_MIN минут (было — только на закрытии часовой свечи).
# Коридор по-прежнему из закрытых часовых свечей, но сила и уклон зависят от текущей цены,
# OI, фандинга и тейкеров — внутри часа они меняются, и монета может стать зарядом к :30.
# ВАЖНО: вход даём только в НОВЫЕ заряды. Повторная перепроверка старых проверена
# бэктестом и вредна (−0.09R против +0.29R), поэтому её не включаем.
CHARGE_SCAN_MIN      = int(os.environ.get("CHARGE_SCAN_MIN", "30"))
# v10.2: скан каждые 30 минут — на :00 и на :30. В v10.1 был только :30, и если сигнал не
# прошёл или пропущен, следующего шанса приходилось ждать час. Заряды считаются по ЗАКРЫТЫМ
# часовым свечам, поэтому на :00 (час только что закрылся) и на :30 набор зарядов один и тот же,
# меняются цена, уклон BTC и расстояние до границы. На каждом скане проверяются ВСЕ заряды;
# монета, по которой уже вошли, до конца дня заблокирована гейтом «одна монета в день».
CHARGE_SCAN_OFFSET_MIN = int(os.environ.get("CHARGE_SCAN_OFFSET_MIN", "0"))

# ── ФИЛЬТР ПО BTC ──
# Альты ходят за биткоином. Входим в лонг только когда BTC выше своего VWAP на запас
# BTC_VWAP_MARGIN_PCT, в шорт — когда ниже на столько же. Между ними — нейтраль, пропускаем.
# Заряд, у которого уклон пары не определён («both»), берёт направление от BTC.
# Бэктест (60 дн, 103 пары): без фильтра −0.16R, с запасом 0.2% около +0.07R, винрейт 55→67%.
# Но доверительный интервал ±0.20R, то есть плюс НЕ доказан; на чужом периоде не проверен.
BTC_FILTER_ENABLED  = os.environ.get("BTC_FILTER", "1") == "1"
BTC_VWAP_MARGIN_PCT = float(os.environ.get("BTC_VWAP_MARGIN_PCT", "0.2"))
# v10.3: сторона VWAP у САМОЙ ПАРЫ. Лонг — только когда цена пары выше её VWAP, шорт — ниже.
# Запас в % от VWAP; между границами сторона считается неопределённой и вход пропускается.
# Для зарядов без уклона пары направление задаёт BTC, но сторона VWAP пары всё равно должна
# совпасть. В бэктесте bt_btc этот фильтр отдельно НЕ проверялся — ставим по твоему решению.
PAIR_VWAP_SIDE_ENABLED = os.environ.get("PAIR_VWAP_SIDE", "1") == "1"
PAIR_VWAP_SIDE_MARGIN  = float(os.environ.get("PAIR_VWAP_SIDE_MARGIN", "0.0"))
VWAP_WINDOW_BARS    = 97      # ≈ сутки 15м свечей, как в бэктесте
# v9.11: вернули 60 (было 30). Бэктест проверяет заряды на ЗАКРЫТЫХ часовых свечах,
# то есть раз в час. При скане раз в полчаса бот брал входы в середине часа по
# неполной свече — таких сделок в бэктесте нет вообще, и их качество непроверено.
# Торгуем ровно то, что валидировано.
SCAN_INTERVAL        = 5 if MOMENTUM_ENABLED else min(CHARGE_SCAN_MIN, TF_MIN)
BTC_DECORR_THRESHOLD = 1.5     # % раскорр для лонга
BTC_DECORR_SHORT     = -1.5    # % раскорр для шорта
DECORR_WATCH         = 1.0     # % сниженный порог для монет из watchlist ЗАРЯДа
RVOL_THRESHOLD           = 1.2   # минимум для «разгона объёма»
RVOL_EXPLOSION_THRESHOLD = 2.5   # порог одиночного взрывного скачка
RVOL_HOT_THRESHOLD   = 8.0     # горячий объём 🔥🔥
CLOSE_POS_THRESHOLD  = 0.6     # закрытие в верхних 40% для лонга
CLOSE_POS_SHORT      = 0.4     # закрытие в нижних 40% для шорта
FUNDING_HOT_LONG     = 0.03    # % — лонги переполнены (единый порог для меток и оценки)
FUNDING_HOT_SHORT    = -0.03   # % — шорты переполнены
STOP_PCT             = -3.0    # % аварийный потолок стопа
STOP_PCT_BASE        = 1.5     # % базовый стоп
ATR_STOP_MULT        = 1.0
ATR_TP2_MULT         = 2.0
TOP_N                = 2       # максимум импульсов одной стороны за скан
MOMENTUM_COOLDOWN_MIN = 120    # по отчёту v8.0 повторные сигналы хуже первых (47% против 58% TP1)

# ── Свечи / база объёма ──
# v8.7: все окна ниже заданы в ЧАСАХ и переводятся в свечи по текущему TF.
# Раньше они были в свечах и калибровались на 1h; при переходе на 15м они бы сжались вчетверо,
# а база объёма ([-BASE_FROM:-BASE_TO]) начала бы залезать внутрь 12-часового окна заряда —
# «норма» объёма считалась бы по самому накоплению и занижала бы rvol (зарядов стало бы МЕНЬШЕ).
BASE_FROM_H      = 84          # база объёма: закрытые свечи за последние 84ч,
BASE_TO_H        = 12          # заканчиваются 12ч назад — ровно там, где начинается окно заряда
SWING_LOOKBACK_H = 144         # часов истории для свинг-уровней и процентиля BB (6 суток)
BASE_FROM        = max(10, round(BASE_FROM_H * 60 / TF_MIN))
BASE_TO          = max(2,  round(BASE_TO_H  * 60 / TF_MIN))
SWING_LOOKBACK   = max(40, round(SWING_LOOKBACK_H * 60 / TF_MIN))
CANDLES_LIMIT    = max(300, SWING_LOOKBACK + 40)   # хватает и на свинги, и на базу объёма

# ── ЗАРЯД (накопление) ──
# v8.7: ACC_WINDOW теперь считается в часах, а не в свечах — при смене CHARGE_TF ширина
# коридора (hi/lo) остаётся 12 часов, как было на 1h ("точки коридора выбирал как на 1h"),
# а свечи внутри окна мельче — это и должно точнее ловить локальные сжатия внутри окна.
ACC_WINDOW_HOURS  = 12         # реальная ширина коридора в часах (была 12 свечей × 1h)
ACC_WINDOW        = max(4, round(ACC_WINDOW_HOURS * 60 / TF_MIN))   # свечей в окне заряда
WIN_MIN           = ACC_WINDOW * TF_MIN          # окно заряда в минутах (≈ ACC_WINDOW_HOURS*60)
HALF_MIN          = WIN_MIN // 2                 # половина окна (для объёма)
WIN_TXT, HALF_TXT = fmt_minutes(WIN_MIN), fmt_minutes(HALF_MIN)
ACC_FLAT_ATR      = 2.0        # |изменение цены за окно| ≤ 2 × нормальный ATR (порог, откалиброван на 1h)
ACC_FLAT_ATR_EFF  = ACC_FLAT_ATR * ATR_TF_SCALE  # v8.7: применяемый порог — скорректирован под размер свечи
# v8.8: потолок диапазона — ЧИСТО адаптивный, от типичного размаха самой монеты.
# Было жёсткое «не шире 5%»: одна цифра и для BTC, и для мемкоина, и для спокойного рынка,
# и для волатильного. Замерено симуляцией: проходимость такого фильтра падает со 100%
# (волатильность 0.3%/ч) до 8% (2.0%/ч) — отсюда и пропали заряды.
# Стало: потолок = k × ATR% × √(свечей в окне) — ожидаемый размах ИМЕННО этой монеты за окно.
# Для волатильной монеты порог мягче, для спокойной жёстче: монета, которая обычно ходит 2%
# за 12ч, а сейчас прошла 4.5%, больше НЕ считается стоящей на месте (раньше проходила).
# Разброс проходимости по режимам рынка: было 58 п.п., стало 12 п.п. — фильтр почти
# перестал зависеть от режима, чего и добивались.
# ACC_RANGE_FLOOR_PCT — не поведение, а страховка от нулевого/битого ATR.
# Если зарядов станет мало, поднимать через переменную окружения ACC_RANGE_FLOOR (напр. 5.0).
ACC_RANGE_FLOOR_PCT = float(os.environ.get("ACC_RANGE_FLOOR", "1.5"))
ACC_RANGE_ATR_K     = float(os.environ.get("ACC_RANGE_K", "1.6"))   # коэф. ожидаемого размаха
ACC_MAX_RANGE_ABS   = float(os.environ.get("ACC_RANGE_ABS", "9.0")) # жёсткий предел: шире — тренд, не коридор
DIR_SWING_HOURS   = 4          # v8.7: было 4 свечи (=4ч на 1h) — окно для структуры "повыш./пониж. минимумы/максимумы"
DIR_SWING_N       = max(2, round(DIR_SWING_HOURS * 60 / TF_MIN))    # свечей, тоже в реальных часах
ACC_SQUEEZE_PCTL  = 25         # v8.3: 25 вместо 35 — по бэктесту лучший вариант
ACC_TR_RATIO_MAX  = 0.75       # или средний диапазон свечей ≤ 75% от нормы
ACC_RVOL_MIN      = 0.8        # v9.3: было 1.0. Рост монотонный по всему диапазону:
                               # 1.3 → 164 сд и +0.216R, 0.8 → 372 сд и +0.302R.
                               # Край сетки, но безопасный: ниже 0.8 объём уже бессмысленно мал.
                               # Сжатие (25) и предел размаха (9%) НЕ трогаем: прогон показал,
                               # что они почти ничего не отсекают (+89 и +2 сделки), а каждый
                               # лишний снятый фильтр — лишний риск.
ACC_OI_PREFILTER  = 1.5        # или OI за окно ≥ +1.5% (по тикерам)
ACC_MIN_SCORE     = 4          # v9.3: было 5. Прогон 60 дней, 1910 кандидатов:
                               # порог 6 — 39 сд и −0.017R, порог 5 — 275 сд и +0.282R,
                               # порог 4 — 521 сд и +0.328R (максимум таблицы).
                               # Ниже (3 и 2) выборка почти не растёт, качество чуть падает.
ACC_MAX_SCORE     = 12         # максимум шкалы силы (для отображения «сила 7/12»)
ACC_REALERT_DELTA = 2          # повторный алерт, только если сила выросла на 2+

# ── Сроки жизни/оценки — в ЧАСАХ (v8.7), одинаковы на любом таймфрейме ──
WATCH_TTL_HOURS   = 16         # сколько ЗАРЯД ждёт пробоя в watchlist
EVAL_WINDOW_HOURS = 24         # окно объективной оценки заряда по ценам
FOLLOW_HOURS      = 12         # сколько меряем ход после выхода из диапазона
HORIZON_HOURS     = 12         # горизонт оценки исхода сигнала
COOLDOWN_HOURS    = 3          # пауза по монете после пробоя

# ── Профили таймфрейма: у каждого ЗАРЯДа свой (альты — CHARGE_TF, BTC — BTC_CHARGE_TF) ──
BTC_CHARGE_ENABLED = False   # v10.0: BTC не торгуем, он служит только фильтром направления
BTC_CHARGE_TF      = "1h"      # BTC движется медленнее альтов — на 1h диапазоны 1–3%, пробои крупнее

def tf_profile(tf: str) -> dict:
    """v8.7: окна профиля заданы в ЧАСАХ. Раньше были в свечах (N × tf_min), поэтому при
    переходе 1h → 15м все сроки (жизнь заряда, горизонт оценки) сжимались вчетверо, и
    статистика v8.7 стала бы несопоставима с v8.3–8.6. Теперь они одинаковы на любом TF."""
    m = {"5m": 5, "15m": 15, "30m": 30, "1h": 60}[tf]
    win_candles = max(4, round(ACC_WINDOW_HOURS * 60 / m))   # окно в часах для ЭТОГО tf,
    win = win_candles * m                                     # не глобальный ACC_WINDOW (он под CHARGE_TF альтов)
    return {
        "tf": tf, "tf_min": m, "win_min": win, "win_txt": fmt_minutes(win),
        "window": win_candles,
        "dir_swing_n": max(2, round(DIR_SWING_HOURS * 60 / m)),
        "flat_atr_eff": ACC_FLAT_ATR * (REF_TF_MIN / m) ** 0.5,
        "half_txt": fmt_minutes(win // 2),
        "swing_txt": fmt_minutes(SWING_LOOKBACK_H * 60),
        "watch_ttl_min": WATCH_TTL_HOURS * 60,         # сколько живёт в watchlist — 16ч на любом TF
        "eval_window": EVAL_WINDOW_HOURS * 3600,       # оценка заряда по ценам — 24ч
        "follow_min": FOLLOW_HOURS * 60,               # ход после выхода — 12ч
        "horizon": HORIZON_HOURS * 3600,               # оценка сигнала — 12ч
        "cooldown_min": COOLDOWN_HOURS * 60,           # пауза после пробоя — 3ч
    }
ACC_REALERT_MIN   = 30         # повтор из-за смены уклона — не чаще раза в 30 мин (v8.0 спамил)

# ── ПРОБОЙ (быстрый триггер) ──
FAST_INTERVAL_SEC = 20         # опрос watchlist
WATCH_TTL_MIN     = WATCH_TTL_HOURS * 60   # v8.7: 16ч на любом TF (было 16 свечей)
WATCH_MAX         = 15
BREAK_BUFFER      = 0.003      # v8.4: было 0.1% — пол минимального отступа за уровнем (см. BREAK_BUFFER_ATR_MULT ниже)
BREAK_BUFFER_ATR_MULT = 0.15   # v8.4: доп. отступ = 0.15×ATR монеты — защита от снятия ликвидности тонким
                                # проколом уровня; масштабируется под волатильность конкретной монеты
                                # (у мемкоина шум в 0.1-0.3% обычный, у BTC/топов — уже перебор)
# v8.1: ⚡ПРОБОЙ только по ЗАКРЫТИЮ 5м свечи за уровнем (в v8.0 — касание цены на 1м:
# 47% пробоев возвращались в диапазон за 15 мин). Проверка по-прежнему каждые 20с,
# поэтому сигнал приходит через несколько секунд после закрытия свечи.
BREAK_CONFIRM_TF  = "1m"      # v8.2: подтверждение пробоя по закрытию 1м свечи (заряд по-прежнему ищется на 15м)
BREAK_CONFIRM_SEC = 60
BREAK_MIN_RVOL    = 2.0        # v8.3: объём свечи пробоя ≥2× нормы (по бэктесту)
BREAK_STOP_MIN    = 0.8        # % минимальный стоп пробоя (0.6% выбивало шумом)
STOP_MIN_PCT      = 0.8        # % минимальный стоп в ордерах заряда
TP1_MIN_RR        = 0.5        # v8.4: было 1.0 — TP1 не ближе 0.5× расстояния до стопа (цели уменьшены вдвое)
TP2_MIN_RR        = 1.0        # v8.4: было 2.0 — TP2 не ближе 1× расстояния до стопа (цели уменьшены вдвое)
BREAKOUT_COOLDOWN_MIN = COOLDOWN_HOURS * 60  # повторы хуже первых сигналов — пауза 2ч; у BTC своя в профиле

# ── Логирование исходов ──
LOG_DIR          = os.environ.get("LOG_DIR", ".")
SIGNALS_CSV      = os.path.join(LOG_DIR, "signals_v8.csv")
OUTCOMES_CSV     = os.path.join(LOG_DIR, "outcomes_v8.csv")
OUTCOME_HORIZON  = 12 * TF_MIN * 60   # горизонт оценки сигнала (15м → 3ч, цели дальше)
CHARGES_CSV          = os.path.join(LOG_DIR, "charges_v8.csv")
CHARGE_OUTCOMES_CSV  = os.path.join(LOG_DIR, "charge_outcomes_v8.csv")
CHARGE_EVAL_WINDOW   = 24 * TF_MIN * 60   # заряд оцениваем по ценам за это время после алерта (15м → 6ч)
CHARGE_FOLLOW_MIN    = 12 * TF_MIN        # насколько ушла цена после выхода из диапазона (15м → 3ч)
CHARGE_RETURN_MIN    = 15      # возврат внутрь диапазона за 15 мин = ложный выход
EXEC_CSV             = os.path.join(LOG_DIR, "exec_dry_v8.csv")    # что бы открыл авто-слой (dry)
TRADES_CSV           = os.path.join(LOG_DIR, "my_trades_v8.csv")   # мои реальные сделки (команды в чате)
ALT_P = tf_profile(CHARGE_TF)
BTC_P = tf_profile(BTC_CHARGE_TF)
MOM_P = tf_profile("5m")        # ИМПУЛЬС всегда на 5м — его горизонт оценки 1ч

# ─── UPSCALE PAIRS ────────────────────────────────────────────────────────────

# v9.10: монеты, которые не торгуем. Задаётся переменной EXCLUDE_SYMBOLS через запятую,
# например EXCLUDE_SYMBOLS=ENA,POPCAT. Кода менять не надо.
EXCLUDE_SYMBOLS = {x.strip().upper() for x in
                   (os.environ.get("EXCLUDE_SYMBOLS") or "").split(",") if x.strip()}

UPSCALE_PAIRS = [
    "ETH","BNB","XRP","SOL","AAVE","ADA","AERO","ALGO","APT","ARB",
    "ASTER","ATOM","AVAX","AXS","BCH","BERA","BONK","BRETT","BSV",
    "CAKE","CHZ","CRO","CRV","DASH","DATA","DEEP","DEXE","DOGE","DOT",
    "DYDX","EIGEN","ENA","ENS","ETC","FARTCOIN","FET","FIL","FLOKI",
    "GALA","GRAM","GRASS","GRT","HBAR","HYPE","ICP","IMX","INJ","IOTA",
    "JASMY","JTO","JUP","KAIA","KAITO","KAS","LDO","LINEA","LINK","LTC",
    "MANA","MNT","MORPHO","MOVE","NEAR","ONDO","OP","ORDI","PENDLE",
    "PENGU","PEPE","PNUT","POL","POPCAT","PUMP","PYTH","QNT","RAY",
    "RENDER","RUNE","S","SAND","SEI","SHIB","SKY","STRK","STX","SUI",
    "TAO","TIA","TRUMP","TRX","TURBO","UNI","VET","VIRTUAL","WAL",
    "WIF","WLD","XLM","XMR","XTZ","ZEC","ZRO","0G"
]
if EXCLUDE_SYMBOLS:                      # v9.10: монеты из EXCLUDE_SYMBOLS не торгуем
    UPSCALE_PAIRS = [p for p in UPSCALE_PAIRS if p.upper() not in EXCLUDE_SYMBOLS]

# ─── ГЛОБАЛЬНОЕ СОСТОЯНИЕ ────────────────────────────────────────────────────

WATCHLIST: dict = {}          # sym -> данные ЗАРЯДа (уровни, база объёма, свинги...)
LAST_SENT: dict = {}          # (kind, sym, side) -> (ts, score)
OI_TICKER_HIST: list = []     # [(ts, {sym: oi})] — запасной источник OI (≤75 мин)
LAST_BTC = {"chg15": 0.0, "chgwin": 0.0, "ts": 0}
PENDING_OUTCOMES: list = []
PENDING_FILE = os.path.join(LOG_DIR, "pending_v9.json")
GATE_FILE    = os.path.join(LOG_DIR, "daygate_v9.json")
DAY_RESULTS: list = []
CHARGE_PENDING: list = []     # эпизоды зарядов, ждущие оценки
CHARGE_ACTIVE: dict = {}      # sym -> текущий эпизод заряда (для пометки «бот прислал пробой»)
SENT_SIGNALS: list = []       # последние отправленные сигналы — для команд /fill, /out
MY_TRADES: dict = {}          # sym -> моя открытая сделка (из команд в чате)
SLIPPAGE: list = []           # проскальзывание по каждой сделке, %
TG_OFFSET = [0]               # id последнего прочитанного сообщения

# ─── УТИЛИТЫ ──────────────────────────────────────────────────────────────────

def esc(s) -> str:
    return html.escape(str(s), quote=False)

def fnum(x, default=0.0) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default

def pct(a: float, b: float) -> float:
    """Изменение от a к b в %."""
    return (b - a) / a * 100 if a else 0.0

def fmt_usd(x: float) -> str:
    if x >= 1_000_000: return f"${x/1_000_000:.1f}M"
    if x >= 1_000:     return f"${x/1_000:.0f}K"
    return f"${x:.0f}"

def msk_time_str(ts=None) -> str:
    dt = datetime.fromtimestamp(ts, MSK) if ts else datetime.now(MSK)
    return dt.strftime("%H:%M МСК")

class RateLimiter:
    """Равномерно распределяет запросы между потоками."""
    def __init__(self, rate: float):
        self.interval = 1.0 / rate
        self.lock = threading.Lock()
        self.next_ts = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            if self.next_ts < now:
                self.next_ts = now
            delay = self.next_ts - now
            self.next_ts += self.interval
        if delay > 0:
            time.sleep(delay)

LIMITER = RateLimiter(API_RATE_PER_SEC)

def api_get(path: str, params: dict, timeout: int = 8):
    url = f"{GATE}/{path}"
    for attempt in range(2):
        LIMITER.wait()
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                time.sleep(1.5)
                continue
            if r.status_code != 200:
                return None
            return r.json()
        except Exception as e:
            if attempt == 1:
                print(f"[API ERROR] {path} {params.get('contract', '')}: {e}")
    return None

# ─── TELEGRAM ─────────────────────────────────────────────────────────────────

def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
                                     "disable_web_page_preview": True}, timeout=10)
        r.raise_for_status()
    except Exception as e:
        print(f"[TG ERROR] {e}")

def send_document(path: str, caption: str = ""):
    """Отправляет файл в Telegram. На Render диск стирается при каждом деплое —
    так статистика сохраняется у пользователя в чате."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    try:
        with open(path, "rb") as f:
            r = requests.post(url, data={"chat_id": CHAT_ID, "caption": caption},
                              files={"document": (os.path.basename(path), f)}, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print(f"[TG DOC ERROR] {path}: {e}")

_HTML_OK = ("b", "i", "u", "s", "code", "pre", "a")

def _escape_stray(text: str) -> str:
    """Экранирует «<» и «&», которые НЕ являются частью разрешённого тега.
    Telegram отвечает 400 «can\'t parse entities», если встретит одиночный «<» —
    так пропал отчёт bt_pairs со строкой «(ATR < 1.06%)»."""
    out, i, n = [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "&":
            m = re.match(r"&(amp|lt|gt|quot|#\d+);", text[i:])
            out.append(text[i:i + m.end()] if m else "&amp;")
            i += m.end() if m else 1
            continue
        if ch == "<":
            m = re.match(r"</?([a-zA-Z0-9]+)(\s[^<>]*)?/?>", text[i:])
            if m and m.group(1).lower() in _HTML_OK:
                out.append(text[i:i + m.end()])
                i += m.end()
                continue
            out.append("&lt;")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)

def _balance_html(text: str) -> str:
    """Закрывает теги, оставшиеся открытыми в куске. Нужно потому, что в отчётах
    бывают многострочные пояснения вида <i>…три строки…</i>: если разделитель
    режет пачку между ними, обе половины получаются с непарными тегами, и
    Telegram отвечает 400 Bad Request вместо отправки."""
    text = _escape_stray(text)
    for tag in ("b", "i", "code", "pre", "u", "s"):
        opened = text.count(f"<{tag}>") - text.count(f"</{tag}>")
        if opened > 0:
            text += f"</{tag}>" * opened
        elif opened < 0:                      # кусок начался с закрывающего тега
            text = f"<{tag}>" * (-opened) + text
    return text

def send_blocks(blocks: list, limit: int = 3800):
    """Telegram режет сообщения > 4096 символов — собираем блоки в пачки.
    Слишком длинную одиночную строку режем по словам, теги балансируем."""
    buf = ""
    for b in blocks:
        while len(b) > limit:                 # одна строка длиннее лимита
            cut = b.rfind(" ", 0, limit) or limit
            if buf:
                send_telegram(_balance_html(buf))
                buf = ""
            send_telegram(_balance_html(b[:cut]))
            b = b[cut:].lstrip()
        if buf and len(buf) + len(b) + 1 > limit:
            send_telegram(_balance_html(buf))
            buf = ""
        buf = f"{buf}\n{b}" if buf else b
    if buf:
        send_telegram(_balance_html(buf))

# ─── СВЕЧИ ────────────────────────────────────────────────────────────────────

def parse_candles(raw) -> list:
    out = []
    if not isinstance(raw, list):
        return out
    for c in raw:
        try:
            out.append({"t": int(c["t"]), "o": float(c["o"]), "h": float(c["h"]),
                        "l": float(c["l"]), "c": float(c["c"]), "v": fnum(c.get("v", 0))})
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda x: x["t"])
    return out

def split_closed(candles: list, interval_sec: int, now_ts: float):
    """Закрытые свечи определяем по времени, а не по позиции [-2].
    Если по неликвидной монете новая свеча ещё не появилась, [-1] уже закрыта —
    старый код в этом случае сдвигал все окна на одну свечу."""
    closed = [c for c in candles if c["t"] + interval_sec <= now_ts]
    forming = candles[-1] if candles and candles[-1]["t"] + interval_sec > now_ts else None
    price = candles[-1]["c"] if candles else 0.0
    return closed, forming, price

def get_candles(symbol: str, interval: str, limit: int) -> list:
    return parse_candles(api_get("candlesticks", {"contract": f"{symbol}_USDT",
                                                  "interval": interval, "limit": limit}))

# ─── ИНДИКАТОРЫ ───────────────────────────────────────────────────────────────

def calc_ema_simple(prices: list, period: int) -> float:
    if len(prices) < period:
        return prices[-1] if prices else 0
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
    return ema

def calc_atr(highs, lows, closes, period=14) -> list:
    trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
           for i in range(1, len(closes))]
    if not trs:
        return [0]
    if len(trs) < period:
        return [sum(trs)/len(trs)]
    atrs = [sum(trs[:period])/period]
    for tr in trs[period:]:
        atrs.append((atrs[-1]*(period-1) + tr) / period)
    return atrs

def true_ranges(candles: list) -> list:
    """TR, выровненный по индексам свечей (TR[0] = h-l)."""
    trs = []
    for i, c in enumerate(candles):
        if i == 0:
            trs.append(c["h"] - c["l"])
        else:
            pc = candles[i-1]["c"]
            trs.append(max(c["h"] - c["l"], abs(c["h"] - pc), abs(c["l"] - pc)))
    return trs

def trimmed_mean(vals: list, trim: float = 0.1) -> float:
    """Среднее без 10% самых больших значений — устойчиво к редким выбросам."""
    v = sorted(x for x in vals if x > 0)
    if not v:
        return 0.0
    cut = int(len(v) * trim)
    v = v[:len(v) - cut] if cut else v
    return sum(v) / len(v)

def bb_width_percentile(closes: list, period: int = 20, lookback: int = SWING_LOOKBACK):
    """Процентиль текущей ширины Боллинджера среди последних lookback значений.
    Малый процентиль = рынок сжат сильнее обычного."""
    n = len(closes)
    start = max(period - 1, n - lookback)
    widths = []
    for i in range(start, n):
        w = closes[i - period + 1:i + 1]
        m = sum(w) / period
        if m <= 0:
            continue
        sd = math.sqrt(sum((x - m) ** 2 for x in w) / period)
        widths.append(2 * sd / m)
    if len(widths) < 30:
        return None
    cur = widths[-1]
    return sum(1 for x in widths if x <= cur) / len(widths) * 100

def find_swings(highs: list, lows: list, left: int = 2, right: int = 1):
    sh, sl = [], []
    for i in range(left, len(highs) - right):
        if all(highs[i] > highs[i-k] for k in range(1, left+1)) and \
           all(highs[i] > highs[i+k] for k in range(1, right+1)):
            sh.append(highs[i])
        if all(lows[i] < lows[i-k] for k in range(1, left+1)) and \
           all(lows[i] < lows[i+k] for k in range(1, right+1)):
            sl.append(lows[i])
    return sh, sl

# ─── ВРЕМЯ ТОРГОВЛИ ──────────────────────────────────────────────────────────

def is_trading_hours() -> bool:
    return TRADING_START_MSK <= datetime.now(MSK).hour < TRADING_END_MSK

def in_signal_window() -> bool:
    """v8.2: мёртвые зоны убраны — их заменили окна отправки сигналов."""
    now = datetime.now(MSK)
    now_min = now.hour * 60 + now.minute
    return any(h1 * 60 + m1 <= now_min < h2 * 60 + m2 for h1, m1, h2, m2 in SIGNAL_WINDOWS)

def windows_txt() -> str:
    return ", ".join(f"{h1:02d}:{m1:02d}–{h2:02d}:{m2:02d}" for h1, m1, h2, m2 in SIGNAL_WINDOWS)

# ─── GATE: тикеры (funding + OI) ─────────────────────────────────────────────

def get_gate_tickers() -> dict:
    data = api_get("tickers", {}, timeout=10)
    result = {}
    if not isinstance(data, list):
        print("[TICKERS] не получены")
        return result
    for t in data:
        contract = t.get("contract", "")
        if not contract.endswith("_USDT"):
            continue
        sym = contract[:-5]
        oi_raw = t.get("total_size") or t.get("open_interest") or t.get("position_size") or 0
        result[sym] = {
            "funding":    fnum(t.get("funding_rate", 0)) * 100,
            "oi":         fnum(oi_raw),
            "change_24h": fnum(t.get("change_percentage", 0)),
            "last":       fnum(t.get("last", 0)),
        }
    print(f"[TICKERS] Загружено {len(result)} пар")
    return result

def push_oi_snapshot(ticker_data: dict, now_ts: float):
    OI_TICKER_HIST.append((now_ts, {s: d["oi"] for s, d in ticker_data.items()}))
    keep = max(ALT_P["win_min"], BTC_P["win_min"] if BTC_CHARGE_ENABLED else 0) + 20
    while OI_TICKER_HIST and now_ts - OI_TICKER_HIST[0][0] > keep * 60:
        OI_TICKER_HIST.pop(0)

def ticker_oi_change(sym: str, minutes: int):
    """Запасной OI: ищем снимок ровно N минут назад (±2.5 мин).
    Старый код сравнивал «3 скана назад» — после мёртвой зоны это были снимки через разрыв."""
    if len(OI_TICKER_HIST) < 2:
        return None
    now_ts, cur = OI_TICKER_HIST[-1]
    target = now_ts - minutes * 60
    best = min(OI_TICKER_HIST[:-1], key=lambda x: abs(x[0] - target))
    if abs(best[0] - target) > 150:
        return None
    old, new = best[1].get(sym, 0), cur.get(sym, 0)
    if not old or not new:
        return None
    return round(pct(old, new), 2)

# ─── GATE: статистика контракта (OI-история, taker L/S, ликвидации) ─────────

def get_contract_stats(sym: str, win_min: int = WIN_MIN):
    # длинные окна (BTC 1h → 12ч) — по часовым строкам, иначе 5-минутных понадобилось бы 150+
    iv_min = 5 if win_min <= 360 else 60
    data = api_get("contract_stats", {"contract": f"{sym}_USDT", "interval": f"{iv_min}m" if iv_min < 60 else "1h",
                                      "limit": max(16, win_min // iv_min + 4)})
    if not isinstance(data, list) or len(data) < 2:
        return None
    rows = []
    for d in data:
        rows.append({
            "t":     int(fnum(d.get("time", 0))),
            "oi":    fnum(d.get("open_interest", 0)),
            "oiu":   fnum(d.get("open_interest_usd", 0)),
            "taker": fnum(d.get("lsr_taker", 0)),
            "liq_l": fnum(d.get("long_liq_usd", 0)),
            "liq_s": fnum(d.get("short_liq_usd", 0)),
        })
    rows = [r for r in rows if r["t"] > 0]
    if len(rows) < 2:
        return None
    rows.sort(key=lambda r: r["t"])
    # OI в контрактах не зависит от цены — это чистый приток/отток позиций.
    key = "oi" if all(r["oi"] > 0 for r in rows) else "oiu"
    last = rows[-1]

    def oi_change(sec):
        older = [r for r in rows if r["t"] <= last["t"] - sec]
        if not older or not older[-1][key]:
            return None
        return round(pct(older[-1][key], last[key]), 2)

    def taker_avg(sec):
        vals = [r["taker"] for r in rows if r["t"] > last["t"] - sec and 0.05 < r["taker"] < 20]
        if not vals:
            return None
        return round(math.exp(sum(math.log(v) for v in vals) / len(vals)), 2)  # геометрическое среднее

    hour_rows = [r for r in rows if r["t"] > last["t"] - 3600]
    win_rows  = [r for r in rows if r["t"] > last["t"] - win_min * 60]
    fine = iv_min == 5    # 15-минутные значения есть только на 5-минутных строках
    return {
        "oi_15m": oi_change(900) if fine else None,
        "oi_1h":  oi_change(3600),
        "oi_win": oi_change(win_min * 60),
        "taker_15m": taker_avg(900) if fine else None,
        "taker_1h":  taker_avg(3600),
        "taker_win": taker_avg(win_min * 60),
        "liq_long_1h":  sum(r["liq_l"] for r in hour_rows),
        "liq_short_1h": sum(r["liq_s"] for r in hour_rows),
        "liq_long_win":  sum(r["liq_l"] for r in win_rows),
        "liq_short_win": sum(r["liq_s"] for r in win_rows),
    }

def get_trade_delta(sym: str, seconds: int = 120):
    """Доля агрессивных покупок за последние N секунд.
    На фьючерсах Gate size > 0 — покупка тейкером, size < 0 — продажа."""
    data = api_get("trades", {"contract": f"{sym}_USDT", "limit": 1000})
    if not isinstance(data, list):
        return None
    cutoff = time.time() - seconds
    buy = sell = 0.0
    for tr in data:
        # create_time_ms у Gate бывает в секундах с дробной частью (REST) или в мс (WS) —
        # определяем по величине, иначе все сделки отбрасываются и дельта всегда «недоступна»
        t = fnum(tr.get("create_time_ms", 0)) or fnum(tr.get("create_time", 0))
        if t > 1e11:
            t /= 1000
        if t < cutoff:
            continue
        size = fnum(tr.get("size", 0))
        if size > 0: buy += size
        else:        sell += -size
    tot = buy + sell
    return round(buy / tot, 2) if tot > 0 else None

# ─── КОНТЕКСТ РЫНКА: BTC 1D + 4H + 15M + EQH/EQL ────────────────────────────

_market_cache = {"text": "", "updated_at": 0, "h4_bias": "❓"}

def find_eq_levels(levels: list, price: float, above: bool, tolerance: float = 0.15) -> list:
    filtered = [l for l in levels if (l > price if above else l < price)]
    if not filtered:
        return []
    filtered.sort(key=lambda x: abs(x - price))
    groups, used = [], set()
    for i, level in enumerate(filtered):
        if i in used:
            continue
        group = [level]
        for j, other in enumerate(filtered):
            if j != i and j not in used and abs(other - level) / level * 100 <= tolerance:
                group.append(other)
                used.add(j)
        if len(group) >= 2:
            groups.append((sum(group)/len(group), len(group)))
        used.add(i)
    groups.sort(key=lambda x: abs(x[0] - price))
    return groups[:2]

def format_liq_line(eq_highs, eq_lows, price, tf_label):
    parts = []
    eq_highs = [(lvl, cnt) for lvl, cnt in eq_highs if lvl > price * 1.001]
    eq_lows  = [(lvl, cnt) for lvl, cnt in eq_lows  if lvl < price * 0.999]
    if eq_highs:
        lvl, cnt = eq_highs[0]
        parts.append(f"⬆️ EQH: {lvl:,.0f} ({pct(price, lvl):+.1f}%) {'⭐' * min(cnt, 3)}")
    if eq_lows:
        lvl, cnt = eq_lows[0]
        parts.append(f"⬇️ EQL: {lvl:,.0f} ({pct(price, lvl):+.1f}%) {'⭐' * min(cnt, 3)}")
    if eq_highs and eq_lows:
        nearest = "⬆️ вверх" if abs(eq_highs[0][0] - price) < abs(eq_lows[0][0] - price) else "⬇️ вниз"
        parts.append(f"🎯 {nearest}")
    elif eq_highs:
        parts.append("🎯 ⬆️ вверх")
    elif eq_lows:
        parts.append("🎯 ⬇️ вниз")
    return f"   {tf_label}: " + " | ".join(parts) if parts else ""

BTC12_HOURS  = 12         # окно «что делает BTC» — 12 часов
BTC12_FLAT   = 0.5        # |изменение| меньше этого — считаем флетом
BTC12_STRONG = 1.5        # сильное движение: при растущем OI фактор весит вдвое

def get_market_context() -> str:
    """Общий блок BTC + всегда свежая строка статуса 1h (пробой / зарядка) + ликвидность."""
    base = _market_base()
    return base + btc_12h_line() + _market_cache.get("liq_text", "")

def fmt_ago(ts: float) -> str:
    mins = max(0, (time.time() - ts) / 60)
    when = datetime.fromtimestamp(ts, MSK)
    day = "" if when.date() == datetime.now(MSK).date() else when.strftime("%d.%m ")
    return f"{day}{when.strftime('%H:%M')} ({fmt_minutes(mins)} назад)" if mins >= 1 else f"{when.strftime('%H:%M')} (только что)"

def btc_12h_line() -> str:
    """Что BTC сделал за последние 12 часов: цена и открытый интерес."""
    d, oi = _market_cache.get("btc12_chg"), _market_cache.get("btc12_oi")
    if d is None:
        return "\n   BTC 12ч: нет данных"
    if abs(d) < BTC12_FLAT:
        mood = "➡️ флет"
    elif d > 0:
        mood = "⬆️ рост"
    else:
        mood = "⬇️ снижение"
    oi_txt = "нет данных" if oi is None else f"{oi:+.2f}%"
    line = f"\n   BTC 12ч: {mood} <b>{d:+.2f}%</b> | OI 12ч: {oi_txt}"
    if oi is not None and abs(d) >= BTC12_FLAT:
        if oi >= 1:
            line += " — новые позиции"
        elif oi <= -1:
            line += " — на закрытии позиций"
    return line

def btc12_score(is_long: bool, plus: list, minus: list, for_btc: bool = False) -> int:
    """Оценка сигнала против того, что BTC делает 12 часов.
    Рост OI усиливает фактор (движение на новых деньгах), падение OI ослабляет."""
    d, oi = _market_cache.get("btc12_chg"), _market_cache.get("btc12_oi")
    if d is None or for_btc:
        return 0
    if abs(d) < BTC12_FLAT:
        minus.append(f"BTC 12ч в флете ({d:+.2f}%) — направление рынка не помогает")
        return 0
    aligned = (d > 0) == is_long
    weight = 1
    if oi is not None and oi >= 1 and abs(d) >= BTC12_STRONG:
        weight = 2
    if oi is not None and oi <= -1:
        weight = 0          # движение на закрытии позиций — не считаем подтверждением
    if weight == 0:
        (plus if aligned else minus).append(
            f"BTC 12ч {d:+.2f}%, но OI {oi:+.2f}% — движение на закрытии позиций, слабое подтверждение")
        return 0
    if aligned:
        plus.append(f"BTC 12ч в нашу сторону ({d:+.2f}%{', OI ' + format(oi, '+.2f') + '%' if oi is not None else ''})")
        return weight
    minus.append(f"BTC 12ч против ({d:+.2f}%{', OI ' + format(oi, '+.2f') + '%' if oi is not None else ''})")
    return -weight

def _market_base() -> str:
    now_ts = time.time()
    if now_ts - _market_cache["updated_at"] < 900 and _market_cache["text"]:
        return _market_cache["text"]
    try:
        c1d = get_candles("BTC", "1d", 210)
        c4h = get_candles("BTC", "4h", 100)
        try:   # сбой часовых свечей не должен ломать 1D/4H — от 4H зависят оценки сигналов
            c1h = get_candles("BTC", "1h", 80)
            c1h_closed, _, p1h = split_closed(c1h, 3600, now_ts)
            if p1h:
                _market_cache["btc_price"] = p1h
            if len(c1h_closed) >= BTC12_HOURS and p1h:
                _market_cache["btc12_chg"] = pct(c1h_closed[-BTC12_HOURS]["o"], p1h)
            st = get_contract_stats("BTC", BTC12_HOURS * 60)
            _market_cache["btc12_oi"] = st["oi_win"] if st else None
        except Exception as e:
            print(f"[MARKET 1H ERROR] {e}")

        d_bias, price = "❓", 0
        if len(c1d) >= 60:
            closes_1d = [c["c"] for c in c1d]
            price = closes_1d[-1]
            ema50  = calc_ema_simple(closes_1d, 50)
            ema200 = calc_ema_simple(closes_1d, 200) if len(closes_1d) >= 200 else None
            if ema200 and price > ema200 and price > ema50:   d_bias = "🐂 Бычий"
            elif ema200 and price < ema200 and price < ema50: d_bias = "🐻 Медвежий"
            elif price > ema50:                               d_bias = "📈 Выше EMA50"
            else:                                             d_bias = "📉 Ниже EMA50"

        # Живая цена (последняя часовая свеча) — для показа и для расчёта EQH/EQL.
        # Тренд 4H (h4_bias) по-прежнему считаем по ЗАКРЫТОЙ 4H свече — это верно,
        # тренд не должен дёргаться от текущей цены. А вот "какой уровень ещё
        # впереди" и "сколько % до него" обязаны быть от текущей цены, иначе после
        # сильного движения внутри 4H-свечи бот показывает уже пройденный уровень
        # как цель и врёт с расстоянием до него.
        live_price = _market_cache.get("btc_price") or price or 0

        h4_bias, h4_liq = "❓", ""
        if len(c4h) >= 20:
            cl4 = [c["c"] for c in c4h]
            hi4 = [c["h"] for c in c4h]
            lo4 = [c["l"] for c in c4h]
            p4 = cl4[-1]
            if not live_price:
                live_price = p4
            ema20 = calc_ema_simple(cl4, 20)
            rh = [max(hi4[i-3:i]) for i in range(3, len(hi4))]
            rl = [min(lo4[i-3:i]) for i in range(3, len(lo4))]
            hh = rh[-1] > rh[-4] if len(rh) >= 4 else None
            hl = rl[-1] > rl[-4] if len(rl) >= 4 else None
            lh = rh[-1] < rh[-4] if len(rh) >= 4 else None
            ll = rl[-1] < rl[-4] if len(rl) >= 4 else None
            if hh and hl:    h4_bias = "📈 Восходящий"
            elif lh and ll:  h4_bias = "📉 Нисходящий"
            elif p4 > ema20: h4_bias = "↗️ Выше EMA20"
            else:            h4_bias = "↘️ Ниже EMA20"
            sh4, sl4 = find_swings(hi4, lo4, left=2, right=2)
            h4_liq = format_liq_line(find_eq_levels(sh4, live_price, True),
                                      find_eq_levels(sl4, live_price, False), live_price, "4H")
        price = live_price or price

        lines = [f"📊 BTC: {price:,.0f} USDT" if price else "📊 BTC: —",
                 f"   1D: {d_bias}", f"   4H: {h4_bias}"]
        _market_cache["liq_text"] = ("\n💧 BTC ликвидность 4H:\n" + h4_liq) if h4_liq else ""
        # FIX v8: раньше словарь перезаписывался целиком и h4_bias терялся —
        # оценка тренда BTC 4H в анализе всегда была «неопределён».
        _market_cache.update({"text": "\n".join(lines), "updated_at": now_ts, "h4_bias": h4_bias})
        print(f"[MARKET] обновлён, 4H: {h4_bias}")
        return _market_cache["text"]
    except Exception as e:
        print(f"[MARKET ERROR] {e}")
        return _market_cache["text"] or "📊 BTC: данные недоступны"

def h4_score(is_long: bool, plus: list, minus: list) -> int:
    h4 = _market_cache.get("h4_bias", "❓")
    if is_long:
        if "Восходящий" in h4:  plus.append("BTC 4H восходящий — по тренду"); return 1
        if "Нисходящий" in h4:  minus.append("BTC 4H нисходящий — вход против старшего тренда"); return -2
        if "Ниже EMA20" in h4:  minus.append("BTC 4H слабый (ниже EMA20)"); return -1
    else:
        if "Нисходящий" in h4:  plus.append("BTC 4H нисходящий — по тренду"); return 1
        if "Восходящий" in h4:  minus.append("BTC 4H восходящий — вход против старшего тренда"); return -2
        if "Выше EMA20" in h4:  minus.append("BTC 4H сильный (выше EMA20)"); return -1
    minus.append(f"BTC 4H нейтральный ({h4})")
    return 0

# ─── ЛОГИРОВАНИЕ И ИСХОДЫ ────────────────────────────────────────────────────

def _append_csv(path: str, row: dict):
    try:
        header = ",".join(row.keys())
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                first = f.readline().strip()
            if first != header:   # колонки изменились (новая версия) — старый файл в архив
                os.replace(path, f"{path}.old-{int(time.time())}")
        new = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(row.keys()))
            if new:
                w.writeheader()
            w.writerow(row)
    except Exception as e:
        print(f"[CSV ERROR] {path}: {e}")

def log_signal(kind: str, s: dict, side: str, score: int):
    now_ts = time.time()
    sid = f"{int(now_ts)}-{s['symbol']}-{kind}-{side}"
    row = {
        "id": sid, "ver": BOT_VERSION,
        "time_msk": datetime.fromtimestamp(now_ts, MSK).strftime("%Y-%m-%d %H:%M:%S"),
        "kind": kind, "symbol": s["symbol"], "side": side, "score": score,
        "price": s["price"], "stop": s["stop"], "tp1": s["tp1_price"], "tp2": s["tp2_price"],
        "rvol": s.get("rvol", ""), "decorr": round(s.get("decorr", 0), 2),
        "oi_15m": s.get("oi_15m", ""), "oi_1h": s.get("oi_1h", ""), "oi_win": s.get("oi_win", ""),
        "stop_pct": round(s.get("stop_pct", 0), 2), "tp1_pct": round(s.get("tp1_pct", 0), 2),
        "tp2_pct": round(s.get("tp2_pct", 0), 2), "tf": s.get("tf", "5m"),
        "taker": s.get("taker_15m", ""), "funding": round(s.get("funding", 0), 4),
        "ch24": round(s.get("change_24h", 0), 2), "from_charge": s.get("from_charge", False),
        "charge_score": s.get("charge_score", ""), "charge_side": s.get("charge_side", ""),
        "pace": s.get("pace", ""), "delta": s.get("delta", ""),
        "h4": _market_cache.get("h4_bias", ""), "btc15": round(LAST_BTC["chg15"], 2),
        "btc12_chg": _market_cache.get("btc12_chg", ""), "btc12_oi": _market_cache.get("btc12_oi", ""),
    }
    _append_csv(SIGNALS_CSV, row)
    SENT_SIGNALS.append({"symbol": s["symbol"], "side": side, "price": s["price"], "stop": s["stop"],
                         "tp1": s["tp1_price"], "tp2": s["tp2_price"],
                         "stop_pct": abs(s.get("stop_pct", STOP_MIN_PCT)), "ts": now_ts, "score": score})
    del SENT_SIGNALS[:-60]                     # держим последние 60
    PENDING_OUTCOMES.append({"id": sid, "kind": kind, "symbol": s["symbol"], "side": side,
                             "score": score, "entry": s["price"], "stop": s["stop"],
                             "tp1": s["tp1_price"], "tp2": s["tp2_price"], "ts": now_ts,
                             "horizon": s.get("horizon", MOM_P["horizon"])})
    pending_save()

def evaluate_outcome(p: dict):
    raw = api_get("candlesticks", {"contract": f"{p['symbol']}_USDT", "interval": "1m",
                                   "from": int(p["ts"]), "to": int(p["ts"] + p.get("horizon", OUTCOME_HORIZON))})
    # свеча, в которой пришёл сигнал, началась раньше входа — её не учитываем
    candles = [c for c in parse_candles(raw) if c["t"] >= p["ts"] - 1]
    if not candles:
        return None
    is_long = p["side"] == "long"
    entry = p["entry"]
    first, close30, close60 = "none", None, None
    tp2_first, stopped = False, False
    tp2 = p.get("tp2")
    for c in candles:
        stop_hit = c["l"] <= p["stop"] if is_long else c["h"] >= p["stop"]
        tp_hit   = c["h"] >= p["tp1"]  if is_long else c["l"] <= p["tp1"]
        if first == "none":
            if stop_hit:  first = "stop"       # обе цели в одной свече — считаем стоп (консервативно)
            elif tp_hit:  first = "tp1"
        # TP2 до исходного стопа (консервативно, без переноса в безубыток)
        if not stopped and not tp2_first and tp2:
            if stop_hit:
                stopped = True
            elif (c["h"] >= tp2 if is_long else c["l"] <= tp2):
                tp2_first = True
        if close30 is None and c["t"] >= p["ts"] + 1800:
            close30 = c["c"]
        if close60 is None and c["t"] >= p["ts"] + 3600:
            close60 = c["c"]
    hi = max(c["h"] for c in candles); lo = min(c["l"] for c in candles)
    mfe = pct(entry, hi) if is_long else -pct(entry, lo)
    mae = pct(entry, lo) if is_long else -pct(entry, hi)
    sign = 1 if is_long else -1
    r30 = sign * pct(entry, close30) if close30 else None
    r60 = sign * pct(entry, close60) if close60 else None
    r_end = sign * pct(entry, candles[-1]["c"])
    return {"id": p["id"], "ver": BOT_VERSION, "kind": p["kind"], "symbol": p["symbol"], "side": p["side"],
            "score": p["score"], "first_hit": first, "mfe_pct": round(mfe, 2), "mae_pct": round(mae, 2),
            "tp2_before_stop": tp2_first,
            "ret_30m": round(r30, 2) if r30 is not None else "",
            "ret_60m": round(r60, 2) if r60 is not None else "",
            "ret_end": round(r_end, 2), "horizon_min": p.get("horizon", OUTCOME_HORIZON) // 60,
            "date": datetime.fromtimestamp(p["ts"], MSK).strftime("%Y-%m-%d")}

def log_charge(c: dict):
    """Первый алерт заряда открывает эпизод: пишем все признаки и ставим в очередь оценки."""
    now_ts = time.time()
    cid = f"{int(now_ts)}-{c['symbol']}-charge"
    row = {
        "id": cid, "ver": BOT_VERSION,
        "time_msk": datetime.fromtimestamp(now_ts, MSK).strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": c["symbol"], "bias": c["side"], "score": c["score"], "bias_pts": c["bias"],
        "hi": c["hi"], "lo": c["lo"], "range_pct": round(c["rng_pct"], 2), "price": c["price"],
        "max_range": c.get("max_range", ""), "atr_pct": c.get("atr_pct", ""),
        "pos_in_range": round(c["pos"], 2),
        "bb_pctl": round(c["sq_pct"], 1) if c["sq_pct"] is not None else "",
        "tr_ratio": round(c["tr_ratio"], 2), "rvol_half": round(c["rvol_half"], 2), "vol_rising": c["vol_rising"],
        "oi_win": c["oi_win"] if c["oi_win"] is not None else "",
        "oi_15m": c["oi_15m"] if c["oi_15m"] is not None else "",
        "taker_win": c["taker_win"] if c["taker_win"] is not None else "",
        "funding": round(c["funding"], 4), "chg_win": round(c["chg_win"], 2), "ch24": round(c["change_24h"], 2),
        "h4": _market_cache.get("h4_bias", ""), "btc_win": round(LAST_BTC["chgwin"], 2) if c["symbol"] != "BTC" else "",
        "tf": c["P"]["tf"], "window_min": c["P"]["win_min"],
        "btc12_chg": _market_cache.get("btc12_chg", ""), "btc12_oi": _market_cache.get("btc12_oi", ""),
    }
    _append_csv(CHARGES_CSV, row)
    ep = {"id": cid, "symbol": c["symbol"], "bias": c["side"], "score": c["score"], "max_score": c["score"],
          "hi": c["hi"], "lo": c["lo"], "height": c["height"], "ts": now_ts,
          "eval_window": c["P"]["eval_window"], "follow_min": c["P"]["follow_min"], "tf": c["P"]["tf"],
          "bot_signal": "", "side_changes": 0}
    CHARGE_PENDING.append(ep)
    pending_save()
    CHARGE_ACTIVE[c["symbol"]] = ep

def evaluate_charge(ep: dict):
    """Оценка заряда ТОЛЬКО по ценам — независимо от того, прислал ли бот пробой:
    вышла ли цена из диапазона, куда, когда, насколько далеко и не вернулась ли назад.
    Уровни — те, что были в первом алерте (по ним трейдер ставил алерты)."""
    raw = api_get("candlesticks", {"contract": f"{ep['symbol']}_USDT", "interval": "1m",
                                   "from": int(ep["ts"]), "to": int(ep["ts"] + ep["eval_window"])})
    candles = [c for c in parse_candles(raw) if c["t"] >= ep["ts"] - 1]
    if not candles:
        return None
    hi, lo, height = ep["hi"], ep["lo"], ep["height"]
    up_lvl, dn_lvl = hi * (1 + BREAK_BUFFER), lo * (1 - BREAK_BUFFER)
    broke, bi = "none", None
    for i, c in enumerate(candles):
        up, dn = c["h"] >= up_lvl, c["l"] <= dn_lvl
        if up and dn:   # прокол в обе стороны одной минуткой — решаем по закрытию
            broke = "up" if c["c"] > hi else ("down" if c["c"] < lo else "whipsaw")
        elif up:
            broke = "up"
        elif dn:
            broke = "down"
        if broke != "none":
            bi = i
            break
    res = {"id": ep["id"], "ver": BOT_VERSION, "kind": "charge", "symbol": ep["symbol"], "bias": ep["bias"],
           "score": ep["score"], "max_score": ep["max_score"], "broke": broke,
           "break_min": "", "matched_bias": "", "follow_pct": "", "follow_heights": "",
           "reached_1x": "", "returned_15m": "", "adverse_pct": "", "bot_signal": ep["bot_signal"],
           "tf": ep.get("tf", ""), "date": datetime.fromtimestamp(ep["ts"], MSK).strftime("%Y-%m-%d")}
    if broke in ("up", "down"):
        bc = candles[bi]
        res["break_min"] = round((bc["t"] - ep["ts"]) / 60, 1)
        if ep["bias"] != "both":
            res["matched_bias"] = (broke == "up") == (ep["bias"] == "long")
        after = [c for c in candles[bi:] if c["t"] <= bc["t"] + ep["follow_min"] * 60]
        early = [c for c in candles[bi:] if c["t"] <= bc["t"] + CHARGE_RETURN_MIN * 60]
        if broke == "up":
            ext = max(c["h"] for c in after) - hi
            adv = min(c["l"] for c in after) - hi
            res["returned_15m"] = any(c["c"] < hi for c in early)
            res["follow_pct"] = round(pct(hi, hi + ext), 2)
            res["adverse_pct"] = round(pct(hi, hi + adv), 2)
        else:
            ext = lo - min(c["l"] for c in after)
            adv = lo - max(c["h"] for c in after)
            res["returned_15m"] = any(c["c"] > lo for c in early)
            res["follow_pct"] = round(pct(lo, lo - ext) * -1, 2)
            res["adverse_pct"] = round(pct(lo, lo - adv) * -1, 2)
        if height > 0:
            res["follow_heights"] = round(ext / height, 2)
            res["reached_1x"] = ext >= height
    return res

def _note_outcome(res):
    """Закрытый убыток → счётчик стопа дня (v8.3)."""
    try:
        if res and res.get("kind") in ("breakout", "momentum") and res.get("first_hit") == "stop":
            gate_loss()
    except Exception:
        pass

def process_outcomes(max_items: int = 5):
    now_ts = time.time()
    done = 0
    for p in list(PENDING_OUTCOMES):
        if done >= max_items:
            break
        if now_ts < p["ts"] + p.get("horizon", OUTCOME_HORIZON) + 90:
            continue
        PENDING_OUTCOMES.remove(p)
        pending_save()
        try:
            res = evaluate_outcome(p)
        except Exception as e:
            print(f"[OUTCOME ERROR] {p['symbol']}: {e}")
            res = None
        done += 1
        if res:
            _append_csv(OUTCOMES_CSV, res)
            DAY_RESULTS.append(res)
            _note_outcome(res)
    for ep in list(CHARGE_PENDING):
        if done >= max_items:
            break
        if now_ts < ep["ts"] + ep["eval_window"] + 90:
            continue
        CHARGE_PENDING.remove(ep)
        pending_save()
        if CHARGE_ACTIVE.get(ep["symbol"]) is ep:
            CHARGE_ACTIVE.pop(ep["symbol"], None)
        try:
            res = evaluate_charge(ep)
        except Exception as e:
            print(f"[CHARGE OUTCOME ERROR] {ep['symbol']}: {e}")
            res = None
        done += 1
        if res:
            _append_csv(CHARGE_OUTCOMES_CSV, res)
            DAY_RESULTS.append(res)

def send_daily_summary():
    today = datetime.now(MSK).strftime("%Y-%m-%d")
    # берём всё, что оценено с прошлой сводки: заряды оцениваются через CHARGE_EVAL_WINDOW,
    # поэтому поздние вчерашние заряды попадают в сегодняшнюю сводку, а не теряются
    res = list(DAY_RESULTS)
    lines = [f"📒 <b>Итог дня {today}</b> (оценено с прошлой сводки)"]
    if not res:
        lines.append("Оценённых сигналов пока нет.")
    ch = [r for r in res if r["kind"] == "charge"]
    if ch:
        n = len(ch)
        brk = [r for r in ch if r["broke"] in ("up", "down")]
        nb = len(brk)
        line = f"⏳ ЗАРЯД: {n} | вышли из диапазона: {nb} ({nb/n*100:.0f}%)"
        dirn = [r for r in brk if r["matched_bias"] != ""]
        if dirn:
            ok = sum(1 for r in dirn if r["matched_bias"])
            line += f" | по уклону: {ok}/{len(dirn)} ({ok/len(dirn)*100:.0f}%)"
        if nb:
            r1 = sum(1 for r in brk if r["reached_1x"])
            ret = sum(1 for r in brk if r["returned_15m"])
            line += (f" | дошли до ×1 высоты: {r1/nb*100:.0f}% | вернулись внутрь за 15м: {ret/nb*100:.0f}%"
                     f" | ср. ход после выхода {sum(r['follow_pct'] for r in brk)/nb:.2f}%")
        lines.append(line)
    names = {"breakout": "⚡ ПРОБОЙ", "momentum": "🚀 ИМПУЛЬС"}
    for kind in ("breakout", "momentum"):
        rs = [r for r in res if r["kind"] == kind]
        if not rs:
            continue
        n = len(rs)
        tp = sum(1 for r in rs if r["first_hit"] == "tp1")
        st = sum(1 for r in rs if r["first_hit"] == "stop")
        mfe = sum(r["mfe_pct"] for r in rs) / n
        mae = sum(r["mae_pct"] for r in rs) / n
        t2 = sum(1 for r in rs if r.get("tp2_before_stop"))
        lines.append(f"{names[kind]}: {n} сигн. | TP1 первым {tp} ({tp/n*100:.0f}%) | "
                     f"стоп первым {st} ({st/n*100:.0f}%) | TP2 до стопа {t2} ({t2/n*100:.0f}%) | "
                     f"ср. макс. ход {mfe:+.2f}% / просадка {mae:+.2f}%")
    lines.append("📎 Файлы с подробностями — ниже (накопительные, с начала работы после последнего деплоя).")
    send_telegram("\n".join(lines))
    DAY_RESULTS.clear()
    for path, cap in ((SIGNALS_CSV, "Все сигналы со всеми признаками"),
                      (OUTCOMES_CSV, "Исходы сигналов: TP1/стоп первым, макс. ход"),
                      (CHARGES_CSV, "Все ЗАРЯДы со всеми признаками"),
                      (CHARGE_OUTCOMES_CSV, "Исходы ЗАРЯДов: куда вышла цена, ложные выходы"),
                      (EXEC_CSV, "Авто-слой: что бы открыл (dry)")):
        send_document(path, f"{today} — {cap}")

# ─── ОБЩИЕ РАСЧЁТЫ СТОПОВ И ЦЕЛЕЙ ────────────────────────────────────────────

DAY_GATE = {"date": "", "sent": 0, "losses": 0, "syms": set(), "recent": []}

def _day_reset():
    today = datetime.now(MSK).strftime("%Y-%m-%d")
    if DAY_GATE["date"] != today:
        DAY_GATE.update({"date": today, "sent": 0, "losses": 0, "syms": set(), "recent": []})

def gate_allows(sym: str, side: str) -> tuple:
    """Правила v8.3: лимит сигналов в день, одна монета в день, антикластер, стоп дня."""
    _day_reset()
    now_ts = time.time()
    if DAY_STOP_LOSSES and DAY_GATE["losses"] >= DAY_STOP_LOSSES:
        return False, f"стоп дня: уже {DAY_GATE['losses']} закрытых убытка"
    if DAILY_MAX_SIGNALS and DAY_GATE["sent"] >= DAILY_MAX_SIGNALS:
        return False, f"лимит {DAILY_MAX_SIGNALS} сигналов в день исчерпан"
    if ONE_PER_SYMBOL_DAY and sym in DAY_GATE["syms"]:
        return False, "по этой монете сегодня уже был сигнал"
    DAY_GATE["recent"] = [r for r in DAY_GATE["recent"] if now_ts - r[0] <= 2100]   # 35 мин: вход на :00 виден на скане :30
    if MAX_SAME_SIDE_30M and sum(1 for r in DAY_GATE["recent"] if r[1] == side) >= MAX_SAME_SIDE_30M:
        return False, f"уже {MAX_SAME_SIDE_30M} сигнала в {side} за последний скан"
    return True, ""

def gate_save():
    """v9.10: дневной гейт жил только в памяти — после перезапуска обнулялся,
    и бот заново входил в монеты, по которым сегодня уже торговал (так ENA
    набрала четыре входа). Теперь пишем на диск вместе с очередью оценки."""
    try:
        with open(GATE_FILE, "w", encoding="utf-8") as f:
            json.dump({**DAY_GATE, "syms": sorted(DAY_GATE["syms"])}, f, ensure_ascii=False)
    except Exception as e:
        print(f"[GATE] не сохранил: {e}")

def gate_load():
    """Поднимаем дневной гейт с диска, если он за сегодня."""
    try:
        if not os.path.exists(GATE_FILE):
            return 0
        with open(GATE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("date") != datetime.now(MSK).strftime("%Y-%m-%d"):
            return 0
        DAY_GATE.update({"date": d["date"], "sent": int(d.get("sent", 0)),
                         "losses": int(d.get("losses", 0)),
                         "syms": set(d.get("syms") or []),
                         "recent": [tuple(x) for x in (d.get("recent") or [])]})
        return len(DAY_GATE["syms"])
    except Exception as e:
        print(f"[GATE] не восстановил: {e}")
        return 0

def gate_register(sym: str, side: str):
    _day_reset()
    DAY_GATE["sent"] += 1
    DAY_GATE["syms"].add(sym)
    DAY_GATE["recent"].append((time.time(), side))
    gate_save()

def gate_loss():
    """Вызывается, когда бот сам оценил исход сигнала как убыточный."""
    _day_reset()
    DAY_GATE["losses"] += 1
    if DAY_GATE["losses"] == DAY_STOP_LOSSES:
        send_telegram(f"🛑 <b>Стоп дня</b>: {DAY_STOP_LOSSES} закрытых убытка. "
                      f"Сигналы до завтра не шлём.\nПотеря при риске ${RISK_USD:.0f} — "
                      f"около ${DAY_STOP_LOSSES * RISK_USD:.0f} из лимита ${DAY_LOSS_USD:.0f}.")

def day_vwap_atr(sym: str):
    """Дневной VWAP (от 00:00 МСК) и ATR по 15м свечам. Нужен для главного фильтра v8.3."""
    raw = get_candles(sym, "15m", 100)
    if not raw:
        return None, None
    start = datetime.now(MSK).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    pv = vol = 0.0
    trs, prev = [], None
    for c in raw:
        if prev is not None:
            trs.append(max(c["h"] - c["l"], abs(c["h"] - prev), abs(prev - c["l"])))
        prev = c["c"]
        if c["t"] >= start:
            tp = (c["h"] + c["l"] + c["c"]) / 3
            pv += tp * c["v"]; vol += c["v"]
    atr = sum(trs[-14:]) / min(14, len(trs)) if trs else None
    return (pv / vol if vol else None), atr

def vwap_filter(sym: str, entry: float) -> tuple:
    """True, если вход не дальше VWAP_MAX_ATR от дневного VWAP."""
    v, a = day_vwap_atr(sym)
    if not v or not a:
        return True, "VWAP недоступен — фильтр пропущен", None
    d = abs(entry - v) / a
    if d > VWAP_MAX_ATR:
        return False, f"цена в {d:.1f} ATR от дневного VWAP (предел {VWAP_MAX_ATR}) — догоняем движение", d
    return True, f"{d:.1f} ATR от дневного VWAP", d

def position_line(price: float, stop_pct: float) -> str:
    """Размер позиции под фиксированный риск в деньгах: позиция = риск / стоп%."""
    if stop_pct <= 0 or price <= 0:
        return ""
    pos = min(RISK_USD / (stop_pct / 100), MAX_POS_USD)
    qty = pos / price
    qty_txt = f"{qty:,.0f}" if qty >= 100 else (f"{qty:.2f}" if qty >= 1 else f"{qty:.4g}")
    capped = " (упёрлись в потолок)" if pos >= MAX_POS_USD else ""
    return f"   💰 Позиция: <b>${pos:,.0f}</b> ≈ {qty_txt} монет{capped} (риск ${RISK_USD:,.0f})"

def calc_dynamic_stop_pct(atr: float, price: float) -> float:
    """Стоп = max(базовый 1.5%, ATR%), но не дальше аварийного потолка 3%."""
    if price <= 0:
        return STOP_PCT_BASE
    atr_pct = (atr / price) * 100 * ATR_STOP_MULT
    return min(max(STOP_PCT_BASE, atr_pct), abs(STOP_PCT))

def structure_targets(price: float, is_long: bool, swings: list, atr: float):
    """TP1 — ближайший свинг за ценой, TP2 — следующий, иначе ATR (как в v7)."""
    if is_long:
        above = sorted(h for h in swings if h > price * 1.0001)
        if above:
            tp1, l1 = above[0], "следующий хай"
        else:
            tp1, l1 = price + atr, "ATR×1.0"
        nxt = [h for h in above if h > tp1 * 1.001]
        if nxt:
            tp2, l2 = nxt[0], "следующий хай"
        else:
            tp2, l2 = max(price + ATR_TP2_MULT * atr, tp1 * 1.001), f"ATR×{ATR_TP2_MULT}"
    else:
        below = sorted((l for l in swings if l < price * 0.9999), reverse=True)
        if below:
            tp1, l1 = below[0], "следующий лой"
        else:
            tp1, l1 = price - atr, "ATR×1.0"
        nxt = [l for l in below if l < tp1 * 0.999]
        if nxt:
            tp2, l2 = nxt[0], "следующий лой"
        else:
            tp2, l2 = min(price - ATR_TP2_MULT * atr, tp1 * 0.999), f"ATR×{ATR_TP2_MULT}"
    return tp1, l1, tp2, l2

# ═════════════════════════════════════════════════════════════════════════════
#  ⏳ ЗАРЯД — поиск накопления ДО движения
# ═════════════════════════════════════════════════════════════════════════════

def detect_charge(sym, closed, price, baseline, atr_norm, ctx, tick, stats_cache, P=ALT_P):
    # v8.7: окно и пороги берём ИЗ ПРОФИЛЯ — у альтов CHARGE_TF, у BTC свой (BTC_CHARGE_TF).
    # Глобальные ACC_WINDOW/DIR_SWING_N посчитаны под CHARGE_TF и для BTC не подходят.
    W = P.get("window", ACC_WINDOW)
    dir_n = P.get("dir_swing_n", DIR_SWING_N)
    flat_atr_eff = P.get("flat_atr_eff", ACC_FLAT_ATR_EFF)
    WT, HT = P["win_txt"], P["half_txt"]
    if len(closed) < 60 or atr_norm <= 0 or baseline <= 0:
        return None
    win = closed[-W:]
    hi = max(c["h"] for c in win)
    lo = min(c["l"] for c in win)
    if lo <= 0:
        return None
    height = hi - lo
    rng_pct = height / lo * 100
    move = win[-1]["c"] - win[0]["o"]
    chg_win = pct(win[0]["o"], win[-1]["c"])

    # 1) Цена стоит
    # v8.8: потолок размаха подстраивается под волатильность самой монеты.
    atr_pct = atr_norm / price * 100 if price > 0 else 0
    exp_range = ACC_RANGE_ATR_K * atr_pct * (W ** 0.5)      # ожидаемый размах за окно
    max_range = min(ACC_MAX_RANGE_ABS, max(ACC_RANGE_FLOOR_PCT, exp_range))
    if abs(move) > flat_atr_eff * atr_norm or rng_pct > max_range:
        return None
    # 2) Сжатие
    closes = [c["c"] for c in closed]
    sq_pct = bb_width_percentile(closes)
    trs = true_ranges(closed)
    tr_ratio = (sum(trs[-W:]) / W) / atr_norm
    squeezed = (sq_pct is not None and sq_pct <= ACC_SQUEEZE_PCTL) or tr_ratio <= ACC_TR_RATIO_MAX
    if not squeezed:
        return None
    # 3) Объём или OI растут
    half = W // 2
    vol_half  = sum(c["v"] for c in win[-half:]) / half
    vol_prevh = sum(c["v"] for c in win[:half]) / half
    rvol_half = vol_half / baseline
    vol_rising = vol_prevh > 0 and vol_half > vol_prevh * 1.15
    oi_win_tk = ticker_oi_change(sym, P["win_min"])
    if not (rvol_half >= ACC_RVOL_MIN or (oi_win_tk is not None and oi_win_tk >= ACC_OI_PREFILTER)):
        return None

    stats = stats_cache(sym)
    oi_win = stats["oi_win"] if stats and stats["oi_win"] is not None else oi_win_tk
    oi_15m = stats["oi_15m"] if stats and stats["oi_15m"] is not None else ticker_oi_change(sym, 15)
    taker_win = stats["taker_win"] if stats else None
    funding = tick.get("funding", 0)

    score, plus, minus = 0, [], []

    if sq_pct is not None and sq_pct <= 15:
        score += 2; plus.append(f"сильное сжатие (ширина BB в нижних {sq_pct:.0f}% за {P['swing_txt']})")
    elif sq_pct is not None and sq_pct <= ACC_SQUEEZE_PCTL:
        score += 1; plus.append(f"сжатие (ширина BB в нижних {sq_pct:.0f}%)")
    if tr_ratio <= 0.6:
        score += 1; plus.append(f"свечи сузились до {tr_ratio*100:.0f}% от нормы")

    if rvol_half >= 2:
        score += 2; plus.append(f"объём за {HT} {rvol_half:.1f}× нормы при стоящей цене — поглощение")
    elif rvol_half >= ACC_RVOL_MIN:
        score += 1; plus.append(f"объём за {HT} {rvol_half:.1f}× нормы")
    else:
        minus.append(f"объём обычный ({rvol_half:.1f}×) — заряд держится на OI")
    if vol_rising:
        score += 1; plus.append("объём нарастает внутри диапазона")

    if oi_win is None:
        minus.append("нет данных OI — заряд не подтверждён позициями")
    elif oi_win >= 4:
        score += 3; plus.append(f"OI +{oi_win}% за {WT} без движения цены — крупный набор позиций")
    elif oi_win >= 2:
        score += 2; plus.append(f"OI +{oi_win}% за {WT} — позиции набираются")
    elif oi_win >= 1:
        score += 1; plus.append(f"OI слегка растёт (+{oi_win}% за {WT})")
    elif oi_win <= -2:
        score -= 2; minus.append(f"OI падает ({oi_win}%) — это выход из позиций, а не накопление")
    else:
        minus.append(f"OI стоит ({oi_win:+}% за {WT})")

    # ── Направление: кто кого поглощает ──
    bl, bs, dir_notes = 0, 0, []
    pos = min(max((price - lo) / height, 0.0), 1.0) if height > 0 else 0.5
    if pos >= 0.66:   bl += 1; dir_notes.append("цена прижата к верхней границе")
    elif pos <= 0.33: bs += 1; dir_notes.append("цена прижата к нижней границе")
    lows_a  = min(c["l"] for c in win[:dir_n]);  lows_c  = min(c["l"] for c in win[-dir_n:])
    highs_a = max(c["h"] for c in win[:dir_n]);  highs_c = max(c["h"] for c in win[-dir_n:])
    if lows_c > lows_a and highs_c >= highs_a * 0.998:
        bl += 1; dir_notes.append("повышающиеся минимумы (сжатие к сопротивлению)")
    if highs_c < highs_a and lows_c <= lows_a * 1.002:
        bs += 1; dir_notes.append("понижающиеся максимумы (сжатие к поддержке)")
    if sym != "BTC" and P["win_min"] == WIN_MIN:
        btc_w = ctx["btc_chg_win"]
        decorr_w = chg_win - btc_w
        if btc_w <= -0.3 and decorr_w >= 0.3:
            bl += 1; dir_notes.append(f"держится при падении BTC ({btc_w:+.2f}% за {WT}) — скрытая сила")
        if btc_w >= 0.3 and decorr_w <= -0.3:
            bs += 1; dir_notes.append(f"не растёт при росте BTC ({btc_w:+.2f}% за {WT}) — скрытая слабость")
    oi_up = oi_win is not None and oi_win >= 1
    if funding < -0.005 and oi_up:
        bl += 1; dir_notes.append(f"шорты набиваются при фандинге {funding:+.3f}% — топливо для сквиза вверх")
    if funding > FUNDING_HOT_LONG and oi_up:
        bs += 1; dir_notes.append(f"лонги набиваются при фандинге {funding:+.3f}% — топливо для пролива")
    if taker_win is not None:
        if taker_win <= 0.9 and chg_win >= 0:
            bl += 2; dir_notes.append(f"агрессивные продажи (taker L/S {taker_win}) не давят цену — внизу лимитный покупатель")
        elif taker_win >= 1.1 and chg_win <= 0:
            bs += 2; dir_notes.append(f"агрессивные покупки (taker L/S {taker_win}) не поднимают цену — сверху лимитный продавец")
        elif taker_win >= 1.1:
            bl += 1; dir_notes.append(f"покупатели агрессивнее (taker L/S {taker_win})")
        elif taker_win <= 0.9:
            bs += 1; dir_notes.append(f"продавцы агрессивнее (taker L/S {taker_win})")

    bias = bl - bs
    side = "long" if bias >= 2 else ("short" if bias <= -2 else "both")
    if abs(bias) >= 3:
        score += 2; plus.append("направление читается уверенно")
    elif abs(bias) >= 2:
        score += 1
    else:
        minus.append("направление неясно — ждём пробой в любую сторону")

    if side != "both":
        score += h4_score(side == "long", plus, minus)

    if score < ACC_MIN_SCORE:
        return None

    sw = closed[-SWING_LOOKBACK:]
    sh, sl = find_swings([c["h"] for c in sw], [c["l"] for c in sw])
    return {
        "symbol": sym, "side": side, "score": score, "bias": bias, "bl": bl, "bs": bs,
        "hi": hi, "lo": lo, "height": height, "rng_pct": rng_pct, "price": price, "pos": pos,
        "max_range": round(max_range, 2), "atr_pct": round(atr_pct, 3),
        "sq_pct": sq_pct, "tr_ratio": tr_ratio, "rvol_half": rvol_half, "vol_rising": vol_rising,
        "oi_win": oi_win, "oi_15m": oi_15m, "taker_win": taker_win, "funding": funding,
        "change_24h": tick.get("change_24h", 0), "chg_win": chg_win,
        "liq_long_win": stats["liq_long_win"] if stats else 0,
        "liq_short_win": stats["liq_short_win"] if stats else 0,
        "baseline_tf": baseline, "atr": atr_norm, "swing_highs": sh, "swing_lows": sl, "P": P,
        "plus": plus, "minus": minus, "dir_notes": dir_notes,
    }

def order_trigger(level: float, is_long: bool, atr: float = 0.0) -> float:
    """Цена входа (триггер Stop Market). Отступ за уровнем = max(фиксированный % BREAK_BUFFER,
    доля ATR монеты BREAK_BUFFER_ATR_MULT) — защита от снятия ликвидности тонким проколом уровня,
    масштабируется под волатильность конкретной монеты, а не одна цифра на всё.
    Одна формула и для сообщения ЗАРЯДа, и для проверки пробоя —
    иначе ордер срабатывает, а бот молчит (так и вышло в первый день v8.3)."""
    buf = max(level * BREAK_BUFFER, atr * BREAK_BUFFER_ATR_MULT)
    return level + buf if is_long else level - buf


def scan_minutes_txt() -> str:
    """Минуты часа, в которые бот сканирует: «:00 и :30»."""
    mins = [m for m in range(60) if ((m - CHARGE_SCAN_OFFSET_MIN) % 60) % SCAN_INTERVAL == 0]
    return " и ".join(f":{m:02d}" for m in mins)

def tilt_windows_txt() -> str:
    return ", ".join(f"{h1:02d}:{m1:02d}–{h2:02d}:{m2:02d}" for h1, m1, h2, m2 in TILT_WINDOWS)

def in_tilt_window() -> bool:
    """Окно работы УКЛОНА: 4:00-21:00 МСК кроме 12:00-14:30."""
    now = datetime.now(MSK)
    nm = now.hour * 60 + now.minute
    return any(h1 * 60 + m1 <= nm < h2 * 60 + m2 for h1, m1, h2, m2 in TILT_WINDOWS)


def build_tilt(c: dict):
    """Строит сигнал УКЛОН по словарю заряда. Возвращает None если условия не выполнены."""
    if not TILT_ENABLED:
        return None
    side = c["side"]
    if side == "both":
        return None
    if c["score"] < TILT_MIN_SCORE:
        return None
    is_long = side == "long"
    price   = c["price"]
    hi, lo  = c["hi"], c["lo"]
    dist_pct = (hi - price) / price * 100 if is_long else (price - lo) / price * 100
    if dist_pct < TILT_MIN_DIST_PCT:
        return None
    key = f"{c['symbol']}:{side}"
    prev_stop = TILT_LAST_STOP.get(key)
    if prev_stop is not None:
        if is_long and price <= prev_stop:
            return None
        if not is_long and price >= prev_stop:
            return None
    entry = price
    stop  = entry * (1 - TILT_STOP_PCT / 100) if is_long else entry * (1 + TILT_STOP_PCT / 100)
    tp1   = entry * (1 + TILT_TP1_PCT  / 100) if is_long else entry * (1 - TILT_TP1_PCT  / 100)
    margin = entry * TILT_TP2_MARGIN / 100
    tp2   = (hi - margin) if is_long else (lo + margin)
    if is_long and tp2 <= tp1:
        tp2 = tp1 * 1.001
    if not is_long and tp2 >= tp1:
        tp2 = tp1 * 0.999
    tp3 = entry * (1 + TILT_TP3_PCT / 100) if is_long else entry * (1 - TILT_TP3_PCT / 100)
    # цели строго по возрастанию в сторону сделки
    if is_long and tp3 <= tp2:
        tp3 = tp2 * 1.001
    if not is_long and tp3 >= tp2:
        tp3 = tp2 * 0.999
    rr1 = TILT_TP1_PCT / TILT_STOP_PCT
    rr2 = abs(tp2 - entry) / entry * 100 / TILT_STOP_PCT
    rr3 = abs(tp3 - entry) / entry * 100 / TILT_STOP_PCT
    return {
        "kind": "tilt", "symbol": c["symbol"], "side": side, "score": c["score"],
        "price": entry, "stop": stop, "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "stop_pct": TILT_STOP_PCT, "tp1_pct": TILT_TP1_PCT,
        "tp2_pct": abs(tp2 - entry) / entry * 100,
        "dist_pct": dist_pct, "rr1": rr1, "rr2": rr2, "rr3": rr3,
        "tp3_pct": abs(tp3 - entry) / entry * 100,
        "hi": hi, "lo": lo, "atr": c["atr"], "charge_score": c["score"], "charge_side": side,
    }


def format_tilt(t: dict) -> str:
    sym, side = t["symbol"], t["side"]
    arrow    = "LONG" if side == "long" else "SHORT"
    boundary = t["hi"] if side == "long" else t["lo"]
    return (
        f"УКЛОН {sym} {arrow} score={t['score']}\n"
        f"Вход: {t['price']:.6g} | до границы: {t['dist_pct']:.2f}%\n"
        f"Стоп: {t['stop']:.6g} (-{t['stop_pct']}%) | TP1: {t['tp1']:.6g} (+{t['tp1_pct']}%, RR 1:{t['rr1']:.1f})\n"
        f"TP2: {t['tp2']:.6g} (граница коридора, RR 1:{t['rr2']:.1f})\n"
        f"TP3: {t['tp3']:.6g} (за пробоем, RR 1:{t['rr3']:.1f})\n"
        f"По трети позиции на каждую цель, стоп подтягивается после TP1 и TP2"
    )


def log_tilt(t: dict):
    _append_csv(SIGNALS_CSV, {
        "id": f"{int(time.time())}-{t['symbol']}-tilt-{t['side']}",
        "ver": BOT_VERSION, "time_msk": datetime.now(MSK).strftime("%Y-%m-%d %H:%M:%S"),
        "kind": "tilt", "symbol": t["symbol"], "side": t["side"], "score": t["score"],
        "price": t["price"], "stop": t["stop"], "tp1": t["tp1"], "tp2": t["tp2"],
        "rvol": "", "decorr": "", "oi_15m": "", "oi_1h": "", "oi_win": "",
        "stop_pct": t["stop_pct"], "tp1_pct": t["tp1_pct"], "tp2_pct": round(t["tp2_pct"], 2),
        "tf": "1h", "taker": "", "funding": "", "ch24": "", "from_charge": True,
        "charge_score": t["charge_score"], "charge_side": t["charge_side"],
        "pace": "", "delta": "", "h4": "", "btc15": "", "btc12_chg": "", "btc12_oi": "",
    })


def charge_verdict(c: dict) -> str:
    """Короткий вывод по ЗАРЯДу: ставить ордера или ждать."""
    s = c["score"]
    side = {"long": "вверх", "short": "вниз"}.get(c["side"], "в обе стороны")
    facts = []
    if c["sq_pct"] is not None and c["sq_pct"] <= 15:
        facts.append(f"сжатие редкое ({c['sq_pct']:.0f}%)")
    if c["oi_win"] is not None and c["oi_win"] >= 2:
        facts.append(f"OI +{c['oi_win']:.1f}%")
    elif c["oi_win"] is not None and c["oi_win"] <= -1:
        facts.append(f"OI падает ({c['oi_win']:.1f}%)")
    if c["rvol_half"] >= 2:
        facts.append(f"объём {c['rvol_half']:.1f}×")
    if abs(c["change_24h"]) >= 15:
        facts.append(f"разогрета ({c['change_24h']:+.0f}% за сутки)")
    tail = ", ".join(facts[:3])
    # v9.14: вердикт должен совпадать с реальным решением. Раньше писал «Беру по рынку»
    # даже когда УКЛОН отказывался входить — сообщение противоречило само себе.
    takes = False
    if TILT_ENABLED and c["side"] in ("long", "short"):
        is_l = c["side"] == "long"
        bnd = c["hi"] if is_l else c["lo"]
        d = (bnd - c["price"]) / c["price"] * 100 if is_l else (c["price"] - bnd) / c["price"] * 100
        takes = d >= TILT_MIN_DIST_PCT
    if not takes:
        act = "Вход не беру — смотрю дальше."
    elif s >= 9:
        act = f"Беру {side} по рынку, полный размер."
    else:
        act = f"Беру {side} по рынку, веду строго по стопу."
    return f"💬 {act}" + (f" Нравится: {tail}." if tail else "")

def format_charge(c: dict) -> str:
    P = c["P"]; WT, HT = P["win_txt"], P["half_txt"]
    side_txt = {"long": "🟢 ЛОНГ-уклон", "short": "🔴 ШОРТ-уклон", "both": "⚪ Неясно — ждём любой пробой"}[c["side"]]
    filled = max(1, min(5, round(c["score"] / ACC_MAX_SCORE * 5)))
    bat = "🔋" * filled + "▫️" * (5 - filled)
    oi_line = (f"OI {WT}: <b>{c['oi_win']:+.2f}%</b>" if c["oi_win"] is not None else f"OI {WT}: нет данных") + \
              (f" | 15м: {c['oi_15m']:+.2f}%" if c["oi_15m"] is not None else "")
    taker = f"{c['taker_win']}" if c["taker_win"] is not None else "—"
    # Ориентир стопа для пробоя: за уровнем на 1 нормальный ATR, но не дальше противоположной границы
    up_stop = max(c["lo"], c["hi"] - c["atr"])
    dn_stop = min(c["hi"], c["lo"] + c["atr"])
    sq = f"BB в нижних {c['sq_pct']:.0f}%" if c["sq_pct"] is not None else "BB —"
    # v9.14: оставляем только то, что реально участвует в решении. Ликвидации,
    # фандинг, изменение за 24ч и 15-минутный OI в расчёте не используются —
    # это был информационный шум, который мешал читать сообщение.
    lines = [
        f"⏳ <b>ЗАРЯД {c['symbol']}/USDT</b> | {msk_time_str()}",
        f"<b>{side_txt}</b> | сила {bat} {c['score']}/{ACC_MAX_SCORE}",
        f"Коридор {WT}: {c['lo']:.6g} – {c['hi']:.6g} (ширина {c['rng_pct']:.2f}%)",
        f"Цена {c['price']:.6g} — {c['pos']*100:.0f}% диапазона",
        f"Сжатие: {sq} | свечи {c['tr_ratio']*100:.0f}% от нормы | "
        f"объём {c['rvol_half']:.1f}×{' 📈' if c['vol_rising'] else ''}",
    ]
    if c["oi_win"] is not None:
        lines.append(f"OI {WT}: <b>{c['oi_win']:+.2f}%</b>"
                     + (f" | Taker L/S: {taker}" if c["taker_win"] is not None else ""))
    # v8.3: готовые ордера — по бэктесту вход ПО УРОВНЮ даёт +0.21% на сделку,
    # а вход после закрытия свечи (то есть по факту сообщения) — минус.
    # v9.6: ПРОБОЙ отключён, ордера по уровням больше не ставим — блок показывался
    # по инерции и путал: бот по этим ценам не торгует.
    if BREAKOUT_ENABLED:
        lines.append("📥 <b>Ордера (Stop Market, ставить заранее):</b>")
    for want, level, stop_lvl in ((("long", c["hi"], up_stop), ("short", c["lo"], dn_stop))
                                  if BREAKOUT_ENABLED else ()):
        if c["side"] not in (want, "both"):
            continue
        is_long = want == "long"
        trig = order_trigger(level, is_long, c["atr"])         # цена входа = триггер Stop Market
        dist = abs(trig - stop_lvl) / trig * 100
        dist = min(max(dist, STOP_MIN_PCT), abs(STOP_PCT))
        stop_price = trig * (1 - dist / 100) if is_long else trig * (1 + dist / 100)
        h = c["hi"] - c["lo"]
        tp1 = level + h if is_long else level - h
        tp2 = level + 2 * h if is_long else level - 2 * h
        arrow = "🟢 ВВЕРХ" if is_long else "🔴 ВНИЗ"
        lines.append(f"   {arrow}: вход <b>{trig:.6g}</b> (Stop Market)")
        lines.append(f"      стоп {stop_price:.6g} (−{dist:.2f}%) | TP1 {tp1:.6g} | TP2 {tp2:.6g}")
        pl = position_line(trig, dist)
        if pl:
            lines.append(pl.rstrip())
    if TILT_ENABLED and c["side"] in ("long", "short"):
        is_l = c["side"] == "long"
        bnd = c["hi"] if is_l else c["lo"]
        px = c["price"]
        d = (bnd - px) / px * 100 if is_l else (px - bnd) / px * 100
        if d >= TILT_MIN_DIST_PCT:
            st = px * (1 - TILT_STOP_PCT / 100) if is_l else px * (1 + TILT_STOP_PCT / 100)
            t1 = px * (1 + TILT_TP1_PCT / 100) if is_l else px * (1 - TILT_TP1_PCT / 100)
            t3 = px * (1 + TILT_TP3_PCT / 100) if is_l else px * (1 - TILT_TP3_PCT / 100)
            lines.append(f"🎯 <b>УКЛОН — вход по рынку сейчас:</b> {px:.6g}")
            lines.append(f"   стоп {st:.6g} (−{TILT_STOP_PCT}%) | до границы {d:.2f}%")
            lines.append(f"   цели по трети: {t1:.6g} → {bnd:.6g} (граница) → {t3:.6g}")
            lines.append(f"   стоп подтягивается после каждой из первых двух")
            pl = position_line(px, TILT_STOP_PCT)
            if pl:
                lines.append(pl.rstrip())
        else:
            lines.append(f"⏭ УКЛОН не берём: до границы {d:.2f}% — меньше {TILT_MIN_DIST_PCT}%")
    elif TILT_ENABLED:
        lines.append("⏭ УКЛОН не берём: уклон неясен (both)")
    lines.append("🔎 <b>Анализ:</b>")
    for n in c["plus"][:3]:      lines.append(f"  ✅ {esc(n)}")
    for n in c["dir_notes"][:3]: lines.append(f"  🧭 {esc(n)}")
    for n in c["minus"][:3]:     lines.append(f"  ⚠️ {esc(n)}")
    lines.append(charge_verdict(c))
    notes = [f"заряд живёт до {msk_time_str(time.time() + P['watch_ttl_min'] * 60)}"]
    if c["side"] == "both" and BREAKOUT_ENABLED:
        notes.append("направление неясно — ставь обе стороны")
    if c["symbol"] == "BTC":
        notes.append("пробой BTC задаёт направление альтам")
    lines.append("👀 " + " | ".join(notes))
    return "\n".join(lines)

def register_charges(charges: list):
    """Добавляет/обновляет watchlist. Возвращает список зарядов для алерта."""
    now_ts = time.time()
    to_alert = []
    for c in sorted(charges, key=lambda x: x["score"], reverse=True):
        sym = c["symbol"]
        recent_break = [v for k, v in LAST_SENT.items() if k[0] == "breakout" and k[1] == sym
                        and now_ts - v[0] < c["P"]["cooldown_min"] * 60]
        if recent_break:
            continue  # недавно уже был пробой по этой монете
        old = WATCHLIST.get(sym)
        if old and (c["price"] > old["hi"] or c["price"] < old["lo"]):
            # цена уже вышла за старые границы — ждём закрытия 5м свечи по СТАРЫМ уровням;
            # иначе новый диапазон «проглотит» свечу пробоя и сигнал не придёт
            continue
        alts_in = [w for s, w in WATCHLIST.items() if s != "BTC"]
        if not old and sym != "BTC" and len(alts_in) >= WATCH_MAX:   # BTC не занимает и не вытесняет места альтов
            weakest = min(alts_in, key=lambda x: x["score"])
            if weakest["score"] >= c["score"]:
                continue
            WATCHLIST.pop(weakest["symbol"], None)
        entry = dict(c)
        entry["created"] = old["created"] if old else now_ts
        entry["expires"] = now_ts + c["P"]["watch_ttl_min"] * 60
        entry["alert_score"] = old.get("alert_score", -99) if old else -99
        entry["alert_ts"] = old.get("alert_ts", 0) if old else 0
        WATCHLIST[sym] = entry
        side_changed = old is not None and old["side"] != c["side"]
        ep = CHARGE_ACTIVE.get(sym)
        if old is None or ep is None:
            log_charge(c)                       # новый эпизод заряда → журнал
        else:
            ep["max_score"] = max(ep["max_score"], c["score"])
            if side_changed:
                ep["side_changes"] += 1
        side_realert = side_changed and now_ts - entry["alert_ts"] >= ACC_REALERT_MIN * 60
        if c["score"] >= entry["alert_score"] + ACC_REALERT_DELTA or side_realert:
            entry["alert_score"] = c["score"]
            entry["alert_ts"] = now_ts
            to_alert.append(entry)
    return to_alert

# ═════════════════════════════════════════════════════════════════════════════
#  ⚡ ПРОБОЙ — быстрый триггер по watchlist
# ═════════════════════════════════════════════════════════════════════════════

def build_breakout(w: dict, side: str, price: float, pace: float, delta):
    is_long = side == "long"
    level  = w["hi"] if is_long else w["lo"]
    height = w["height"]
    atr    = w["atr"]
    # Стоп: за уровнем пробоя на 1 ATR (но не дальше противоположной границы),
    # расстояние ограничено 0.6%…3% от цены входа.
    if is_long:
        stop_raw = max(w["lo"], level - atr)
        dist = pct(stop_raw, price)
    else:
        stop_raw = min(w["hi"], level + atr)
        dist = pct(price, stop_raw)
    dist = min(max(dist, BREAK_STOP_MIN), abs(STOP_PCT))
    stop = price * (1 - dist / 100) if is_long else price * (1 + dist / 100)

    # Цели: свинги + measured move (высота диапазона от уровня пробоя).
    # v8.1: TP1 не ближе расстояния до стопа (прибыль/риск ≥ 1:1), TP2 — не ближе 2× стопа.
    # В v8.0 первой целью часто становился свинг в 0.3% от входа при стопе 1%+ — цели были меньше риска.
    tp1_min = max(0.3, dist * TP1_MIN_RR)
    tp2_min = max(0.6, dist * TP2_MIN_RR)
    if is_long:
        cands = [(h, "свинг-хай") for h in w["swing_highs"]]
        cands += [(level + height, "высота диапазона ×1"), (level + 2 * height, "высота диапазона ×2")]
        cands = sorted([x for x in cands if x[0] > price * (1 + tp1_min / 100)], key=lambda x: x[0])
    else:
        cands = [(l, "свинг-лой") for l in w["swing_lows"]]
        cands += [(level - height, "высота диапазона ×1"), (level - 2 * height, "высота диапазона ×2")]
        cands = sorted([x for x in cands if 0 < x[0] < price * (1 - tp1_min / 100)], key=lambda x: -x[0])
    if cands:
        tp1, l1 = cands[0]
    else:
        d1 = max(tp1_min / 100 * price, atr)
        tp1, l1 = (price + d1, f"{TP1_MIN_RR:g}× стопа") if is_long else (price - d1, f"{TP1_MIN_RR:g}× стопа")
    lim2 = max(tp2_min / 100 * price, abs(tp1 - price) * 1.3)
    nxt = [x for x in cands if (x[0] >= price + lim2 if is_long else x[0] <= price - lim2)]
    if nxt:
        tp2, l2 = nxt[0]
    else:
        tp2, l2 = (price + lim2, f"{TP2_MIN_RR:g}× стопа") if is_long else (price - lim2, f"{TP2_MIN_RR:g}× стопа")

    ext  = pct(level, price) if is_long else -pct(level, price)   # насколько уже ушли за уровень
    ext_atr = abs(price - level) / atr if atr > 0 else 0          # то же в ATR таймфрейма
    room = pct(price, tp1) if is_long else -pct(price, tp1)
    return {
        "symbol": w["symbol"], "side": side, "price": price, "level": level, "ext": ext, "ext_atr": ext_atr,
        "stop": stop, "stop_pct": dist, "tp1_price": tp1, "tp1_label": l1, "tp2_price": tp2, "tp2_label": l2,
        "tp1_pct": pct(price, tp1), "tp2_pct": pct(price, tp2), "room_pct": room,
        "pace": round(pace, 2), "delta": delta, "charge_score": w["score"], "charge_side": w["side"],
        "charge_created": w["created"], "funding": w["funding"], "change_24h": w["change_24h"],
        "oi_win": w["oi_win"], "oi_15m": w["oi_15m"], "taker_15m": w.get("taker_win"),
        "decorr": 0.0, "rvol": round(pace, 2), "from_charge": True,
        "tf": w["P"]["tf"], "win_txt": w["P"]["win_txt"], "horizon": w["P"]["horizon"],
    }

def analyze_breakout(b: dict):
    is_long = b["side"] == "long"
    score, plus, minus = 0, [], []
    p = b["pace"]   # объём закрытой 5м свечи пробоя к норме
    if p >= 5:     score += 3; plus.append(f"взрывной объём свечи пробоя ({p:.1f}× нормы)")
    elif p >= 3:   score += 2; plus.append(f"сильный объём свечи пробоя ({p:.1f}×)")
    elif p >= 1.5: score += 1; plus.append(f"объём свечи пробоя выше нормы ({p:.1f}×)")
    else:          minus.append(f"объём свечи пробоя на грани ({p:.1f}×) — риск ложного")

    d = b["delta"]
    if d is None:
        minus.append("дельта недоступна — проверь CVD вручную")
    else:
        dd = d if is_long else 1 - d
        if dd >= 0.6:    score += 2; plus.append(f"дельта за свечу {dd*100:.0f}% в нашу сторону — агрессор с нами")
        elif dd >= 0.55: score += 1; plus.append(f"дельта слегка в нашу сторону ({dd*100:.0f}%)")
        elif dd <= 0.45: score -= 2; minus.append(f"дельта против ({dd*100:.0f}%) — пробой без агрессора, похоже на вынос")
        else:            minus.append(f"дельта нейтральная ({dd*100:.0f}%)")

    cs = b["charge_score"]
    if cs >= 9:   score += 2; plus.append(f"сильный заряд перед пробоем ({cs}/{ACC_MAX_SCORE})")
    elif cs >= 7: score += 1; plus.append(f"заряд {cs}/{ACC_MAX_SCORE}")

    if b["charge_side"] == b["side"]:
        score += 1; plus.append("пробой в сторону уклона ЗАРЯДа")
    elif b["charge_side"] != "both":
        score -= 2; minus.append("пробой ПРОТИВ уклона ЗАРЯДа — осторожно")

    ea = b.get("ext_atr", 0)
    if ea <= 0.3:
        score += 1; plus.append(f"вход у самого уровня (+{b['ext']:.2f}%, {ea:.1f} ATR)")
    elif ea > 1.0:
        score -= 1; minus.append(f"цена уже ушла на {b['ext']:.2f}% ({ea:.1f} ATR) за уровень — догоняешь, лучше ретест")

    score += btc12_score(is_long, plus, minus, for_btc=(b["symbol"] == "BTC"))

    score += h4_score(is_long, plus, minus)

    f = b["funding"]
    if is_long and f > FUNDING_HOT_LONG:
        score -= 1; minus.append(f"фандинг перегрет ({f:+.3f}%)")
    if not is_long and f < FUNDING_HOT_SHORT:
        score -= 1; minus.append(f"фандинг против шорта ({f:+.3f}%)")
    if b["room_pct"] < 0.5:
        score -= 1; minus.append(f"до TP1 всего {b['room_pct']:.2f}% — мало места")

    if score >= 7:   v = "🟢 Сильный. Входи."
    elif score >= 4: v = "🟡 Нормальный. Проверь CVD."
    elif score >= 1: v = "🟠 Слабый. Половина позиции или жди ретест."
    else:            v = "🔴 Пропустить / только ретест."
    return score, v, plus, minus

def format_breakout(b: dict, score: int, verdict: str, plus: list, minus: list) -> str:
    is_long = b["side"] == "long"
    head = "⚡ <b>ПРОБОЙ — ЛОНГ</b>" if is_long else "⚡ <b>ПРОБОЙ — ШОРТ</b>"
    if b["symbol"] == "BTC":
        head += f" 🟠 BTC {b['tf']}"
    delta = f"{(b['delta'] if is_long else 1 - b['delta'])*100:.0f}% в нашу сторону" if b["delta"] is not None else "—"
    oi = f"{b['oi_win']:+.2f}%" if b["oi_win"] is not None else "—"
    sign = "-" if is_long else "+"
    lines = [
        f"{head} {b['symbol']}/USDT | {msk_time_str()}",
        f"Уровень {'вверх' if is_long else 'вниз'}: {b['level']:.6g} — {BREAK_CONFIRM_TF} свеча закрылась за ним ({b.get('bar_close', b['price']):.6g})",
        f"Цена сейчас {b['price']:.6g} ({b['ext']:+.2f}% за уровнем)",
        f"Объём свечи пробоя: <b>{b['pace']:.1f}×</b> нормы | Дельта за свечу: {delta}",
        f"Дневной VWAP: {b.get('vwap_txt', '—')}",
        btc_12h_line().strip(),
        f"Из ЗАРЯДа {b['tf']} от {msk_time_str(b['charge_created'])} (сила {b['charge_score']}) | OI {b['win_txt']}: {oi}",
        f"Фандинг: {b['funding']:+.3f}% | 24ч: {b['change_24h']:+.1f}%",
        f"   Вход: <b>{b['price']:.6g}</b>",
        f"   Стоп: {b['stop']:.6g} ({sign}{b['stop_pct']:.2f}%) — за уровнем",
        position_line(b["price"], b["stop_pct"]).rstrip(),
        f"   TP1: {b['tp1_price']:.6g} ({b['tp1_pct']:+.1f}%) — {b['tp1_label']} — 50%",
        f"   TP2: {b['tp2_price']:.6g} ({b['tp2_pct']:+.1f}%) — {b['tp2_label']} — 50%",
        f"📊 Оценка: <b>{score}</b> — {verdict}",
        "🔎 <b>Анализ:</b>",
    ]
    lines += [f"  ✅ {esc(n)}" for n in plus[:3]]
    lines += [f"  ⚠️ {esc(n)}" for n in minus[:3]]
    _hid = max(0, len(plus) - 3) + max(0, len(minus) - 3)
    if _hid:
        lines.append(f"  … ещё {_hid} пункт(ов) в журнале")
    rr = abs(b["tp1_pct"]) / abs(b["stop_pct"]) if b.get("stop_pct") else 0
    d = b.get("delta")
    why = []
    if b["pace"] >= 3:              why.append(f"объём {b['pace']:.1f}×")
    if d is not None and d >= 0.65: why.append(f"агрессор {d*100:.0f}%")
    elif d is not None and d < 0.5: why.append(f"агрессор против ({d*100:.0f}%)")
    if b.get("ext_atr", 0) <= 0.3:  why.append("вход у уровня")
    elif b.get("ext_atr", 0) > 1.0: why.append("вход далеко")
    against = (d is not None and d < 0.5) or b.get("ext_atr", 0) > 1.5
    if score >= 8 and not against:
        act = "Беру полный размер."
    elif score >= 4 and not against:
        act = "Беру половину, добор после закрепления."
    elif against:
        act = ("Осторожно: агрессор против — только половина и короткий стоп."
               if score >= 6 else "Пропускаю: агрессор против пробоя.")
    else:
        act = "Пропускаю: слишком много против."
    lines.append(f"💬 {act} Прибыль к риску {rr:.1f} к 1"
                 + (f", {', '.join(why[:3])}." if why else "."))
    notes = ["после TP1 стоп в безубыток",
             f"возврат 15м свечи за {b['level']:.6g} — повод выйти раньше стопа"]
    if b.get("ext_atr", 0) > 1.0:
        notes.append("вход далеко от уровня")
    lines.append("👀 " + " | ".join(notes))
    return "\n".join(lines)

def fast_check():
    """⚡ПРОБОЙ по watchlist: 5м свеча ЗАКРЫЛАСЬ за уровнем заряда на объёме ≥ нормы.
    Опрос каждые 20с, но каждая монета проверяется один раз на каждую новую закрытую 5м свечу."""
    now_ts = time.time()
    for sym in [s for s, w in WATCHLIST.items() if w["expires"] < now_ts]:
        print(f"[WATCH] {sym}: заряд истёк")
        WATCHLIST.pop(sym, None)

    bar = BREAK_CONFIRM_SEC
    boundary = int(now_ts // bar * bar)          # время закрытия последней завершённой 5м свечи
    for sym, w in list(WATCHLIST.items()):
        if w.get("checked_bar") == boundary:
            continue                              # эту свечу уже проверили — ждём следующую
        try:
            candles = get_candles(sym, BREAK_CONFIRM_TF, 4)
            if not candles:
                continue
            closed, _, price = split_closed(candles, bar, time.time())
            if not closed or price <= 0:
                continue
            last = closed[-1]
            if last["t"] + bar < boundary:
                continue                          # биржа ещё не отдала только что закрытую свечу — повтор через 20с
            w["checked_bar"] = boundary

            close = last["c"]
            up   = close > order_trigger(w["hi"], True, w["atr"])
            down = close < order_trigger(w["lo"], False, w["atr"])
            if not up and not down:
                continue
            side = "long" if up else "short"

            # норма объёма на одну свечу подтверждения (1м), пересчитанная из нормы свечи заряда
            bar_min  = BREAK_CONFIRM_SEC / 60
            base_bar = w["baseline_tf"] * bar_min / w["P"]["tf_min"]
            rvol_bar = last["v"] / base_bar if base_bar > 0 else 0
            # Уровень взят — значит, выставленный заранее ордер уже сработал.
            # Даже если фильтр не пускает сигнал, МОЛЧАТЬ нельзя: позиция открыта.
            reasons = []
            if rvol_bar < BREAK_MIN_RVOL:
                reasons.append(f"объём свечи {rvol_bar:.1f}× < {BREAK_MIN_RVOL}× — пробой на пустом месте")
            vw_ok, vw_txt, vw_d = vwap_filter(sym, price)   # v8.3: главный фильтр — не догонять
            if not vw_ok:
                reasons.append(vw_txt)
            gate_ok_, gate_why = gate_allows(sym, side)
            if not gate_ok_:
                reasons.append(gate_why)
            if not in_signal_window():
                reasons.append(f"вне окна отправки ({windows_txt()} МСК)")
            if reasons:
                print(f"[BREAKOUT] {sym} {side}: уровень взят, но {'; '.join(reasons)}")
                # v9.6: предупреждение имело смысл, пока ты ставил отложенные ордера
                # по сообщению заряда. ПРОБОЙ отключён, ордеров нет — сообщать не о чем.
                if BREAKOUT_ENABLED and not LAST_SENT.get(("warn", sym, side)):
                    LAST_SENT[("warn", sym, side)] = (time.time(), 0)
                    arrow = "🟢 вверх" if side == "long" else "🔴 вниз"
                    send_telegram(
                        f"⚠️ <b>{sym}/USDT — ордер сработал, но сигнал НЕ подтверждён</b>\n"
                        f"Уровень {arrow}: {w['hi'] if side == 'long' else w['lo']:.6g}, цена {price:.6g}\n"
                        "Почему не подтверждаю:\n" + "\n".join(f"  • {esc(r)}" for r in reasons) + "\n"
                        "Что делать: позиция уже открыта ордером. Стоп оставь как был; "
                        "если цена не идёт — закрывай руками, не жди целей.")
                WATCHLIST.pop(sym, None)
                continue

            if not BREAKOUT_ENABLED:
                print(f"[BREAKOUT] {sym} {side}: уровень взят, но ПРОБОЙ отключён (v9.1)")
                WATCHLIST.pop(sym, None)
                continue
            delta = get_trade_delta(sym, bar)     # агрессор за время свечи пробоя
            b = build_breakout(w, side, price, rvol_bar, delta)
            b["bar_close"] = close
            b["vwap_txt"], b["vwap_atr"] = vw_txt, vw_d
            score, verdict, plus, minus = analyze_breakout(b)
            send_telegram(format_breakout(b, score, verdict, plus, minus))
            log_signal("breakout", b, side, score)
            EXECUTOR.on_signal(b, score)          # авто-слой: сбой тут не влияет на сигнал
            gate_register(sym, side)
            now_ts = time.time()
            LAST_SENT[("breakout", sym, side)] = (now_ts, score)
            if sym == "BTC":
                LAST_BTC.setdefault("price_at_break", {})[side] = price
            if sym in CHARGE_ACTIVE:
                CHARGE_ACTIVE[sym]["bot_signal"] = f"{side}:{score}"
                CHARGE_ACTIVE.pop(sym, None)     # эпизод закрыт пробоем, но оценка по ценам остаётся в очереди
            WATCHLIST.pop(sym, None)
            print(f"[BREAKOUT] {sym} {side} close={close:.6g} rvol={rvol_bar:.1f} score={score}")
        except Exception as e:
            print(f"[FAST ERROR] {sym}: {e}")

# ═════════════════════════════════════════════════════════════════════════════
#  🚀 ИМПУЛЬС — RS Momentum (логика v7 + исправления)
# ═════════════════════════════════════════════════════════════════════════════

def analyze_signal(s: dict, is_long: bool, btc_chg: float) -> tuple:
    score, plus, minus = 0, [], []

    rvol = s.get("rvol", 0)
    if s.get("signal_mode") == "accumulation":
        if rvol >= 3:     score += 2; plus.append(f"разгон объёма сильный ({rvol}x)")
        elif rvol >= 1.5: score += 1; plus.append(f"разгон объёма умеренный ({rvol}x)")
        else:             minus.append(f"разгон объёма слабый ({rvol}x, у порога 1.2x)")
    else:
        if rvol >= 10:   score += 3; plus.append(f"взрывной объём ({rvol}x)")
        elif rvol >= 5:  score += 2; plus.append(f"сильный объём ({rvol}x)")
        elif rvol >= 3:  score += 1; plus.append(f"объём выше среднего ({rvol}x)")
        else:            minus.append(f"взрыв слабый ({rvol}x, у порога 2.5x)")

    oi = s.get("oi_15m")
    if oi is None:
        minus.append("нет данных OI")
    elif oi > 1.5:
        score += 2; plus.append(f"OI растёт (+{oi}% за 15м) — новые деньги")
    elif oi > 0.5:
        score += 1; plus.append(f"OI слегка растёт (+{oi}%)")
    elif oi < -1:
        score -= 2; minus.append(f"OI падает ({oi}%) — движение на закрытии позиций, не новый вход")
    else:
        minus.append(f"OI стоит ({oi:+}%) — нет подтверждения новыми деньгами")

    tk = s.get("taker_15m")
    if tk:
        side_tk = tk if is_long else 1 / tk
        if side_tk >= 1.2:    score += 1; plus.append(f"тейкеры давят в нашу сторону (L/S {tk})")
        elif side_tk <= 0.85: score -= 1; minus.append(f"тейкеры против (L/S {tk}) — движение без агрессора")

    cp = s.get("close_position", 0.5)
    eff_cp = cp if is_long else (1 - cp)
    if eff_cp >= 0.90:   score += 2; plus.append("свеча закрылась у края — контроль полный")
    elif eff_cp >= 0.75: score += 1; plus.append("свеча закрылась уверенно")
    elif eff_cp < 0.60:  score -= 1; minus.append("слабое закрытие свечи — нет полного контроля стороны")
    else:                minus.append("закрытие свечи среднее")

    decorr = abs(s.get("decorr", 0))
    if decorr >= 3:   score += 2; plus.append(f"сильный раскорр ({decorr:.1f}%)")
    elif decorr >= 2: score += 1; plus.append(f"умеренный раскорр ({decorr:.1f}%)")
    else:             minus.append(f"раскорр небольшой ({decorr:.1f}%)")

    if s.get("from_charge"):
        score += 2; plus.append("монета из ЗАРЯДа — импульс вышел из накопления")

    ch24 = s.get("change_24h", 0)
    if is_long:
        if ch24 < 5:     score += 1; plus.append(f"монета не разогрета ({ch24:+.1f}% за 24ч)")
        elif ch24 > 25:  score -= 2; minus.append(f"монета перегрета ({ch24:+.1f}% за 24ч) — риск разворота")
        elif ch24 > 15:  score -= 1; minus.append(f"монета разогрета ({ch24:+.1f}% за 24ч)")
        else:            minus.append(f"рост 24ч нейтральный ({ch24:+.1f}%)")
    else:
        if ch24 > 15:    score += 1; plus.append(f"перегрета ({ch24:+.1f}%) — шорт логичен")
        elif ch24 < -10: score += 1; plus.append(f"уже сильно падает ({ch24:+.1f}%) — тренд на нашей стороне")
        else:            minus.append(f"рост 24ч нейтральный ({ch24:+.1f}%)")

    funding = s.get("funding", 0)
    if is_long:
        if funding < -0.005:             score += 1; plus.append(f"фандинг отрицательный ({funding:+.3f}%) — шорты платят")
        elif funding > FUNDING_HOT_LONG: score -= 1; minus.append(f"фандинг перегрет ({funding:+.3f}%) — лонги переполнены")
        else:                            minus.append(f"фандинг нейтральный ({funding:+.3f}%)")
    else:
        if funding > 0.02:                score += 1; plus.append(f"лонги перегружены ({funding:+.3f}%) — топливо для шорта")
        elif funding < FUNDING_HOT_SHORT: score -= 1; minus.append(f"фандинг против шорта ({funding:+.3f}%)")
        else:                             minus.append(f"фандинг нейтральный ({funding:+.3f}%)")

    if "хай" in s.get("tp1_label", "") or "лой" in s.get("tp1_label", ""):
        score += 1; plus.append("есть структурный уровень для TP1")
    else:
        minus.append("TP1 по ATR — впереди нет свинг-уровня")

    if s.get("room_pct", 1) < 0.3:
        score -= 2; minus.append(f"цена почти у TP1 ({s.get('room_pct', 0):.2f}%) — сигнал запоздал")

    score += btc12_score(is_long, plus, minus)

    score += h4_score(is_long, plus, minus)

    if score >= 8:
        v = "🟢 Сильный" + (f" — {', '.join(plus[:2])}" if plus else "") + ". Входи."
    elif score >= 5:
        v = "🟡 Нормальный" + (f" — {plus[0]}" if plus else "") + (f"; но {minus[0]}" if minus else "") + ". Проверь CVD."
    elif score >= 2:
        v = "🟠 Слабый" + (f" — {', '.join(minus[:2])}" if minus else "") + ". Уменьши позицию."
    else:
        v = "🔴 Пропустить" + (f" — {', '.join(minus[:3])}" if minus else "")
    return score, v, plus, minus

def momentum_tips(s: dict, is_long: bool) -> list:
    tips = []
    oi = s.get("oi_15m")
    h4 = _market_cache.get("h4_bias", "")
    if oi is not None and oi < -1:
        tips.append("Движение на закрытии чужих позиций (OI падает) — быстро выдыхается, TP1 фиксируй без жадности.")
    if oi is not None and oi > 1.5 and ((is_long and s["funding"] > FUNDING_HOT_LONG) or
                                         (not is_long and s["funding"] < FUNDING_HOT_SHORT)):
        tips.append("Толпа набивается при горячем фандинге — риск обратного сквиза, стоп не отодвигать.")
    if s.get("near_wall") or s.get("near_floor"):
        tips.append(f"До уровня меньше 0.5% — лучше вход после пробоя {s['tp1_price']:.6g} и закрепления на 1м.")
    if s.get("signal_mode") == "explosion" and s.get("rvol", 0) >= RVOL_HOT_THRESHOLD:
        tips.append("Климакс-объём: если следующая 5м свеча закроется за серединой сигнальной — импульс исчерпан.")
    if is_long and s.get("change_24h", 0) > 15:
        tips.append("Монета разогрета за сутки — вход только на откате, размер меньше.")
    tk = s.get("taker_15m")
    if tk and ((is_long and tk < 0.9) or (not is_long and tk > 1.1)):
        tips.append("Цена идёт, а агрессор против — возможен вынос стопов, жди подтверждения дельтой.")
    if (is_long and "Нисходящий" in h4) or (not is_long and "Восходящий" in h4):
        tips.append("Против тренда BTC 4H — половина размера и быстрый TP1.")
    if s.get("from_charge"):
        tips.append("Импульс вышел из ЗАРЯДа — граница диапазона теперь опора, ретест = второй шанс входа.")
    tips.append("Проверь дельту/CVD на 1м: подтверждение — агрессор в сторону сделки.")
    return tips[:4]

def apply_marks(s: dict, is_long: bool):
    s["near_wall"]  = is_long and s["room_pct"] < 0.5
    s["near_floor"] = (not is_long) and s["room_pct"] < 0.5
    quality, marks = 0, ""
    if s["rvol"] >= RVOL_HOT_THRESHOLD:    quality += 2; marks += "🔥🔥"
    elif s["rvol"] >= RVOL_THRESHOLD * 2:  quality += 1; marks += "🔥"
    if is_long and s["funding"] > FUNDING_HOT_LONG:       quality -= 1; marks += "⚠️фандинг"
    if not is_long and s["funding"] < FUNDING_HOT_SHORT:  quality -= 1; marks += "⚠️фандинг"
    if s["near_wall"]:  quality -= 1; marks += "🧱стена"
    if s["near_floor"]: quality -= 1; marks += "🧱пол"
    if s.get("from_charge"): marks += "⏳"
    s["quality"], s["marks"] = quality, marks

def build_momentum(sym, closed, price, baseline, ctx, tick, stats_cache):
    """RS Momentum на закрытых 5м свечах. closed[-1] = последняя закрытая
    (в v7 это было candles[-2])."""
    # ── Условие 1: ВЗРЫВ — лучшая из 3 последних закрытых свечей ──
    best_candle, best_rvol = None, 0.0
    for c in closed[-3:]:
        r = round(c["v"] / baseline, 2)
        if r > best_rvol:
            best_rvol, best_candle = r, c
    signal_mode, rvol = None, 0.0
    if best_rvol >= RVOL_EXPLOSION_THRESHOLD:
        signal_mode, rvol = "explosion", best_rvol
    else:
        # ── Условие 2: РАЗГОН ОБЪЁМА (в v7 называлось «накопление») ──
        v_prev, v_last = closed[-2]["v"], closed[-1]["v"]
        avg2 = round((v_prev + v_last) / (2 * baseline), 2)
        if v_last > v_prev * 1.10 and avg2 >= RVOL_THRESHOLD:
            signal_mode, rvol, best_candle = "accumulation", avg2, closed[-1]
    if signal_mode is None:
        return None

    # Раскорр: одно и то же 15-минутное окно для альта и BTC (FIX v8)
    alt_chg_window = pct(closed[-3]["o"], closed[-1]["c"])
    decorr = alt_chg_window - ctx["btc_chg_15"]
    watch_side = ctx["watch"].get(sym)
    thr_long  = DECORR_WATCH if watch_side in ("long", "both") else BTC_DECORR_THRESHOLD
    thr_short = -DECORR_WATCH if watch_side in ("short", "both") else BTC_DECORR_SHORT

    rng = best_candle["h"] - best_candle["l"]
    close_position = (best_candle["c"] - best_candle["l"]) / rng if rng > 0 else 0.5

    is_long = None
    if decorr >= thr_long and close_position >= (CLOSE_POS_THRESHOLD if signal_mode == "explosion" else 0.4):
        is_long = True
    elif decorr <= thr_short and close_position <= (CLOSE_POS_SHORT if signal_mode == "explosion" else 0.6):
        is_long = False
    if is_long is None:
        return None

    recent = closed[-100:]
    atrs = calc_atr([c["h"] for c in recent], [c["l"] for c in recent], [c["c"] for c in recent], 14)
    atr = atrs[-1] if atrs else 0
    sw = closed[-SWING_LOOKBACK:]
    sh, sl = find_swings([c["h"] for c in sw], [c["l"] for c in sw])
    tp1, l1, tp2, l2 = structure_targets(price, is_long, sh if is_long else sl, atr)

    dyn = calc_dynamic_stop_pct(atr, price)
    stop = price * (1 - dyn / 100) if is_long else price * (1 + dyn / 100)
    room = pct(price, tp1) if is_long else -pct(price, tp1)

    stats = stats_cache(sym)
    oi_15m = stats["oi_15m"] if stats and stats["oi_15m"] is not None else ticker_oi_change(sym, 15)
    oi_1h  = stats["oi_1h"]  if stats and stats["oi_1h"]  is not None else ticker_oi_change(sym, 60)

    s = {
        "symbol": sym, "side": "long" if is_long else "short", "price": price, "decorr": decorr,
        "decorr_thr": thr_long if is_long else thr_short, "stop_pct": dyn, "stop": stop,
        "alt_chg": alt_chg_window, "rvol": rvol, "signal_mode": signal_mode,
        "close_position": close_position, "funding": tick.get("funding", 0),
        "change_24h": tick.get("change_24h", 0), "oi_15m": oi_15m, "oi_1h": oi_1h,
        "taker_15m": stats["taker_15m"] if stats else None,
        "liq_long_1h": stats["liq_long_1h"] if stats else 0,
        "liq_short_1h": stats["liq_short_1h"] if stats else 0,
        "room_pct": room, "tp1_price": tp1, "tp1_pct": pct(price, tp1), "tp1_label": l1,
        "tp2_price": tp2, "tp2_pct": pct(price, tp2), "tp2_label": l2,
        "from_charge": watch_side == "both" or watch_side == ("long" if is_long else "short"),
        "charge_score": ctx["watch_score"].get(sym, ""), "charge_side": watch_side or "",
    }
    apply_marks(s, is_long)
    return s

def get_fresh_price(symbol: str):
    data = api_get("tickers", {"contract": f"{symbol}_USDT"}, timeout=5)
    if isinstance(data, list) and data:
        return fnum(data[0].get("last", 0)) or None
    return None

def refresh_signal(s: dict, is_long: bool) -> dict:
    """Свежая цена перед отправкой: вход, стоп, комната, TP% и метки пересчитываются."""
    fresh = get_fresh_price(s["symbol"])
    if not fresh:
        return s
    old = s["price"]
    s["price"] = fresh
    sp = s.get("stop_pct", STOP_PCT_BASE)
    s["stop"] = fresh * (1 - sp / 100) if is_long else fresh * (1 + sp / 100)
    s["room_pct"] = pct(fresh, s["tp1_price"]) if is_long else -pct(fresh, s["tp1_price"])
    s["tp1_pct"] = pct(fresh, s["tp1_price"])
    s["tp2_pct"] = pct(fresh, s["tp2_price"])
    apply_marks(s, is_long)   # FIX v8: в v7 метки «стена/пол» не обновлялись после refresh
    print(f"  [REFRESH] {s['symbol']}: {old:.6g} → {fresh:.6g}")
    return s

def format_momentum(s: dict, i: int, is_long: bool, score: int, verdict: str, plus, minus) -> str:
    medals = ["🥇", "🥈", "🥉"]
    medal = medals[i] if i < len(medals) else "▪️"
    marks = f" {s['marks']}" if s["marks"] else ""
    mode = "🚀 Взрыв" if s["signal_mode"] == "explosion" else "📊 Разгон объёма"
    oi = s.get("oi_15m")
    if oi is None:
        oi_txt = "⏳ OI нет данных"
    elif oi > 1:
        oi_txt = f"📈 OI 15м +{oi}% — новые {'лонги' if is_long else 'шорты'} ✅"
    elif oi < -1:
        oi_txt = f"📉 OI 15м {oi}% — {'шорты' if is_long else 'лонги'} закрываются ⚠️"
    else:
        oi_txt = f"➡️ OI 15м {oi:+}% (стоит)"
    if s.get("oi_1h") is not None:
        oi_txt += f" | 1ч {s['oi_1h']:+.2f}%"
    tk = s.get("taker_15m")
    tk_txt = f"{tk} ({'покупатели' if tk > 1 else 'продавцы'} агрессивнее)" if tk else "—"
    sign = "-" if is_long else "+"
    lines = [
        f"{medal} <b>{s['symbol']}/USDT</b>{marks}",
        f"   RVOL: <b>{s['rvol']}x</b> {mode} ✅ Gate.io",
        f"   Раскорр: <b>{s['decorr']:+.2f}%</b> vs BTC (порог {abs(s['decorr_thr']):.1f}%) | Альт 15М: {s['alt_chg']:+.2f}%",
        f"   Закрытие свечи: {s['close_position']*100:.0f}% | Фандинг: {s['funding']:+.3f}%",
        f"   📈 Рост 24ч: {s['change_24h']:+.2f}% | VWAP: {s.get('vwap_txt', '—')}",
        f"   {oi_txt}",
        f"   Taker L/S 15м: {tk_txt}",
    ]
    if s.get("liq_short_1h") or s.get("liq_long_1h"):
        lines.append(f"   Ликвидации 1ч: шортов {fmt_usd(s['liq_short_1h'])} / лонгов {fmt_usd(s['liq_long_1h'])}")
    lines += [
        f"   Комната до {'хая' if is_long else 'лоя'}: {s['room_pct']:.2f}%",
        f"   Вход: <b>{s['price']:.6g}</b>",
        f"   Стоп: {s['stop']:.6g} ({sign}{s['stop_pct']:.2f}%)",
        position_line(s["price"], s["stop_pct"]).rstrip(),
        f"   TP1: {s['tp1_price']:.6g} ({s['tp1_pct']:+.1f}%) — {s['tp1_label']} — 50%",
        f"   TP2: {s['tp2_price']:.6g} ({s['tp2_pct']:+.1f}%) — {s['tp2_label']} — 50%",
        f"   📊 Оценка: <b>{score}</b>",
        f"   💡 {esc(verdict)}",
        "   🔎 <b>Анализ:</b>",
    ]
    lines += [f"     ✅ {esc(n)}" for n in plus]
    lines += [f"     ⚠️ {esc(n)}" for n in minus]
    lines.append("   👀 <b>На что смотреть:</b>")
    lines += [f"     • {esc(t)}" for t in momentum_tips(s, is_long)]
    return "\n".join(lines)

# ═════════════════════════════════════════════════════════════════════════════
#  ОСНОВНОЙ СКАН (каждые 5 минут)
# ═════════════════════════════════════════════════════════════════════════════

def scan_symbol(sym: str, ctx: dict) -> dict:
    out = {"momentum": None, "charge": None}
    tick = ctx["tickers"].get(sym, {})
    cache = {}
    def stats_cache(s):
        if s not in cache:
            cache[s] = get_contract_stats(s)
        return cache[s]

    def load(tf: str, tf_min: int):
        candles = get_candles(sym, tf, CANDLES_LIMIT)
        closed, _, price = split_closed(candles, tf_min * 60, ctx["now_ts"])
        if len(closed) < 60 or price <= 0:
            return None
        baseline = trimmed_mean([c["v"] for c in closed[-BASE_FROM:-BASE_TO]])
        if baseline <= 0:
            return None
        return closed, price, baseline

    data_tf = None
    if ctx["do_charge"]:
        data_tf = load(CHARGE_TF, TF_MIN)
        if data_tf:
            closed, price, baseline = data_tf
            tr_slice = true_ranges(closed)[-BASE_FROM:-BASE_TO]
            atr_norm = sum(tr_slice) / len(tr_slice) if tr_slice else 0
            out["charge"] = detect_charge(sym, closed, price, baseline, atr_norm, ctx, tick, stats_cache)

    if ctx["do_momentum"]:
        # ИМПУЛЬС всегда на 5м свечах; если заряд тоже на 5м — свечи уже загружены
        data5 = data_tf if (TF_MIN == 5 and ctx["do_charge"]) else load("5m", 5)
        if data5:
            closed5, price5, baseline5 = data5
            out["momentum"] = build_momentum(sym, closed5, price5, baseline5, ctx, tick, stats_cache)
    return out

def scan_btc_charge(ctx: dict):
    """Отдельный ЗАРЯД для BTC на своём таймфрейме (по умолчанию 1h)."""
    P = BTC_P
    candles = get_candles("BTC", P["tf"], CANDLES_LIMIT)
    closed, _, price = split_closed(candles, P["tf_min"] * 60, ctx["now_ts"])
    if len(closed) < 60 or price <= 0:
        return None
    baseline = trimmed_mean([c["v"] for c in closed[-BASE_FROM:-BASE_TO]])
    if baseline <= 0:
        return None
    tr_slice = true_ranges(closed)[-BASE_FROM:-BASE_TO]
    atr_norm = sum(tr_slice) / len(tr_slice) if tr_slice else 0
    stats = {}
    def stats_cache(s):
        if s not in stats:
            stats[s] = get_contract_stats(s, P["win_min"])
        return stats[s]
    return detect_charge("BTC", closed, price, baseline, atr_norm, ctx, ctx["tickers"].get("BTC", {}), stats_cache, P)

# ═════════════════════════════════════════════════════════════════════════════
#  ₿ ФИЛЬТР ПО BTC И РЕШЕНИЕ О ВХОДЕ (v10.0)
# ═════════════════════════════════════════════════════════════════════════════

RU_SIDE = {"long": "лонг", "short": "шорт"}

def vwap_rolling(candles: list):
    """VWAP по последним VWAP_WINDOW_BARS закрытым 15м свечам (≈ сутки). Как в бэктесте."""
    seg = candles[-VWAP_WINDOW_BARS:]
    vol = sum(c["v"] for c in seg)
    if not seg or vol <= 0:
        return None
    return sum((c["h"] + c["l"] + c["c"]) / 3 * c["v"] for c in seg) / vol

def btc_bias(now_ts=None) -> dict:
    """Уклон BTC: цена последней ЗАКРЫТОЙ 15м свечи относительно своего VWAP.
    bias: long / short / neutral, либо None, если данных нет (тогда входы блокируются)."""
    now_ts = now_ts or time.time()
    raw = get_candles("BTC", "15m", VWAP_WINDOW_BARS + 8)
    closed, _, price = split_closed(raw, 900, now_ts)
    if len(closed) < 40:
        return {"bias": None, "dev": 0.0, "price": price, "vwap": None}
    vw = vwap_rolling(closed)
    if not vw:
        return {"bias": None, "dev": 0.0, "price": price, "vwap": None}
    dev = (closed[-1]["c"] - vw) / vw * 100
    m = BTC_VWAP_MARGIN_PCT
    bias = "long" if dev > m else "short" if dev < -m else "neutral"
    return {"bias": bias, "dev": dev, "price": price, "vwap": vw}

def btc_line(b: dict) -> str:
    if not b or b.get("bias") is None:
        return "₿ BTC: данных нет — входы заблокированы"
    ico = {"long": "🟢 лонг", "short": "🔴 шорт", "neutral": "⚪ нейтраль"}[b["bias"]]
    side = "выше" if b["dev"] >= 0 else "ниже"
    return (f"₿ BTC: {ico} ({abs(b['dev']):.2f}% {side} своего VWAP, "
            f"запас {BTC_VWAP_MARGIN_PCT}%)")

def _pair_vwap_aligned(sym: str, price: float, atr: float):
    """Способ как в бэктесте: скользящий VWAP по 97 закрытым 15м свечам, ATR ЧАСОВОГО заряда."""
    raw = get_candles(sym, "15m", VWAP_WINDOW_BARS + 8)
    closed, _, _ = split_closed(raw, 900, time.time())
    vw = vwap_rolling(closed) if len(closed) >= 40 else None
    if not vw or not atr or atr <= 0:
        return True, "н/д", None
    d = abs(price - vw) / atr
    return d <= VWAP_ALIGNED_MAX_ATR, f"{d:.1f}", d

def pair_vwap_dist(sym: str, price: float, atr: float):
    """Фильтр «не догонять»: цена пары не дальше предела от VWAP.
    legacy — прежний фильтр рабочей версии (vwap_filter); aligned — как в бэктесте."""
    if PAIR_VWAP_MODE == "aligned":
        return _pair_vwap_aligned(sym, price, atr)
    ok, _txt, d = vwap_filter(sym, price)
    return ok, (f"{d:.1f}" if d is not None else "н/д"), d

def pair_vwap_side(sym: str):
    """Сторона пары относительно её VWAP по ПОСЛЕДНЕЙ ЗАКРЫТОЙ 15м свече.
    Возвращает (сторона, отклонение в %): long / short / neutral / None, если данных нет."""
    raw = get_candles(sym, "15m", VWAP_WINDOW_BARS + 8)
    closed, _, _ = split_closed(raw, 900, time.time())
    if len(closed) < 40:
        return None, 0.0
    vw = vwap_rolling(closed)
    if not vw:
        return None, 0.0
    dev = (closed[-1]["c"] - vw) / vw * 100
    m = PAIR_VWAP_SIDE_MARGIN
    return ("long" if dev > m else "short" if dev < -m else "neutral"), dev

def tilt_decision(c: dict, btc: dict) -> dict:
    """Решение по ОДНОМУ заряду. status: enter / skip. Порядок проверок: окно → BTC →
    расстояние до границы и защита после стопа → VWAP пары. Лимиты и «одна монета в день»
    проверяются отдельно, уже по отсортированному списку (от них зависит порядок)."""
    sym = c["symbol"]
    if sym == "BTC":
        return {"status": "skip", "group": "BTC не торгуем", "detail": ""}
    if not in_tilt_window():
        return {"status": "skip", "group": f"вне окна ({tilt_windows_txt()} МСК)", "detail": ""}
    side, from_btc = c["side"], False
    if BTC_FILTER_ENABLED:
        b = btc.get("bias") if btc else None
        if b is None:
            return {"status": "skip", "group": "нет данных BTC", "detail": ""}
        if b == "neutral":
            return {"status": "skip", "group": "BTC нейтрален", "detail": ""}
        if side in ("long", "short"):
            if side != b:
                return {"status": "skip", "group": f"против BTC (у BTC {RU_SIDE[b]})",
                        "detail": RU_SIDE[side]}
        else:
            side, from_btc = b, True          # уклон пары неясен — направление от BTC
    elif side == "both":
        return {"status": "skip", "group": "уклон неясен", "detail": ""}

    if PAIR_VWAP_SIDE_ENABLED:
        ps, pdev = pair_vwap_side(sym)
        if ps is None:
            return {"status": "skip", "group": "нет данных VWAP пары", "detail": ""}
        if ps == "neutral":
            return {"status": "skip", "group": "пара на своём VWAP", "detail": f"{pdev:+.2f}%"}
        if ps != side:
            where = "выше" if pdev > 0 else "ниже"
            return {"status": "skip",
                    "group": f"пара {where} своего VWAP, а вход в {RU_SIDE[side]}",
                    "detail": f"{pdev:+.2f}%"}
    tilt = build_tilt(dict(c, side=side))
    if tilt is None:
        is_l = side == "long"
        bnd = c["hi"] if is_l else c["lo"]
        d = (bnd - c["price"]) / c["price"] * 100 if is_l else (c["price"] - bnd) / c["price"] * 100
        if f"{sym}:{side}" in TILT_LAST_STOP:
            return {"status": "skip", "group": "после стопа цена не ушла дальше", "detail": ""}
        if d < TILT_MIN_DIST_PCT:
            return {"status": "skip", "group": f"до границы меньше {TILT_MIN_DIST_PCT}%",
                    "detail": f"{d:.2f}%"}
        return {"status": "skip", "group": "условия не выполнены", "detail": ""}
    ok, txt, dv = pair_vwap_dist(sym, c["price"], c.get("atr"))
    if not ok:
        lim = VWAP_ALIGNED_MAX_ATR if PAIR_VWAP_MODE == "aligned" else VWAP_MAX_ATR
        return {"status": "skip", "group": f"VWAP пары дальше {lim} ATR", "detail": txt}
    tilt["from_btc"] = from_btc
    tilt["charge_side"] = c["side"]            # в журнале видно, что уклон пары был неясен
    return {"status": "enter", "tilt": tilt, "side": side}

def process_entries(charges: list):
    """v10.0: ВСЕ заряды текущего скана проходят проверку входа заново. Раньше в УКЛОН
    попадали только НОВЫЕ или усилившиеся заряды (так работал register_charges), а уже
    известные заряды повторно не проверялись — поэтому часть пар «с уклоном» никогда не
    рассматривалась. Теперь заряд, не прошедший час назад, проверяется снова."""
    btc = btc_bias() if BTC_FILTER_ENABLED else None
    cands, skips = [], {}

    def add_skip(group, sym, detail=""):
        skips.setdefault(group, []).append(f"{sym}" + (f" ({detail})" if detail else ""))

    for c in charges:
        try:
            d = tilt_decision(c, btc)
        except Exception as e:
            print(f"[TILT] {c.get('symbol')}: ошибка решения — {e}")
            add_skip("ошибка проверки", c.get("symbol", "?"))
            continue
        if d["status"] == "skip":
            print(f"[TILT] {c['symbol']}: {d['group']} {d['detail']}")
            add_skip(d["group"], c["symbol"], d["detail"])
        else:
            cands.append(d["tilt"])

    # при BTC-фильтре все входы идут в ОДНУ сторону — это структурная концентрация.
    # Лимит «≤5 в сторону» срабатывает по порядку, поэтому сильнее запас хода — раньше.
    cands.sort(key=lambda t: -t["dist_pct"])
    entered = []
    for t in cands:
        sym, side = t["symbol"], t["side"]
        ok, why = gate_allows(sym, side)
        if not ok:
            add_skip(why, sym)
            continue
        print(f"[TILT] {sym} {side} dist={t['dist_pct']:.2f}% from_btc={t['from_btc']}")
        extra = ["", btc_line(btc)] if btc else []
        if t["from_btc"]:
            extra.append("↳ уклон пары был неясен — направление взято от BTC")
        send_blocks((format_tilt(t) + "\n" + "\n".join(extra)).split("\n"))
        log_tilt(t)
        gate_register(sym, side)
        TILT_LAST_STOP[f"{sym}:{side}"] = t["stop"]
        EXECUTOR.on_signal({
            "symbol": sym, "side": side, "price": t["price"], "stop": t["stop"],
            "stop_pct": t["stop_pct"], "tp1_price": t["tp1"], "tp2_price": t["tp2"],
            "tp3_price": t["tp3"],   # три цели по трети — без этого исполнитель делил бы пополам
            "atr": t["atr"], "ext_atr": 0.0,
        }, t["score"])
        entered.append(t)

    # одно сообщение-сводка на весь скан: что взяли и почему не взяли остальное
    L = [f"🔎 <b>Скан {msk_time_str()}</b> | зарядов {len(charges)} | "
         f"вход {len(entered)} | пропуск {len(charges) - len(entered)}"]
    if btc is not None:
        L.append(btc_line(btc))
    if entered:
        L.append("✅ <b>Вход:</b> " + ", ".join(
            f"{t['symbol']} {RU_SIDE[t['side']]}" + ("*" if t["from_btc"] else "") for t in entered))
        if any(t["from_btc"] for t in entered):
            L.append("   <i>* уклон пары неясен, направление от BTC</i>")
    for group, syms in sorted(skips.items(), key=lambda kv: -len(kv[1])):
        L.append(f"⏭ {group}: {', '.join(syms)}")
    send_blocks(L)
    return entered


def run_scan(do_charge: bool = True, do_btc: bool = False):
    now_ts = time.time()
    do_momentum = MOMENTUM_ENABLED
    print(f"[SCAN] Старт {msk_time_str()}")

    n_win5 = WIN_MIN // 5
    btc = get_candles("BTC", "5m", n_win5 + 8)
    btc_closed, _, btc_price = split_closed(btc, 300, now_ts)
    if len(btc_closed) < n_win5 + 1:
        print("[SCAN] BTC свечи не получены")
        return None
    btc_chg_15 = pct(btc_closed[-3]["o"], btc_closed[-1]["c"])
    btc_chg_win = pct(btc_closed[-n_win5]["o"], btc_closed[-1]["c"])   # тот же отрезок, что окно заряда
    btc_chg_1h = pct(btc_closed[-12]["o"], btc_closed[-1]["c"]) if len(btc_closed) >= 12 else btc_chg_win
    LAST_BTC.update({"chg15": btc_chg_15, "chgwin": btc_chg_win, "ts": now_ts, "price": btc_price})
    print(f"[SCAN] BTC 15М: {btc_chg_15:+.2f}% | {WIN_TXT}: {btc_chg_win:+.2f}%")

    tickers = get_gate_tickers()
    if tickers:
        push_oi_snapshot(tickers, now_ts)
    market_ctx = get_market_context()

    ctx = {"now_ts": now_ts, "btc_chg_15": btc_chg_15, "btc_chg_1h": btc_chg_1h, "btc_chg_win": btc_chg_win,
           "tickers": tickers, "watch": {s: w["side"] for s, w in WATCHLIST.items()},
           "watch_score": {s: w["score"] for s, w in WATCHLIST.items()},
           "do_charge": do_charge, "do_momentum": do_momentum}

    results = []
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        futures = [(ex.submit(scan_symbol, sym, ctx), sym) for sym in UPSCALE_PAIRS]
        for f, sym in futures:
            try:
                results.append(f.result())
            except Exception as e:
                print(f"  [ERROR] {sym}: {e}")
    print(f"[SCAN] готово за {time.time() - now_ts:.1f}с")

    # ── ⏳ ЗАРЯДЫ ──
    charges = [r["charge"] for r in results if r["charge"]]
    if do_btc and BTC_CHARGE_ENABLED:
        try:
            bc = scan_btc_charge(ctx)
            print(f"[BTC {BTC_CHARGE_TF}] заряд: {'score=' + str(bc['score']) + ' ' + bc['side'] if bc else 'нет'}")
            if bc:
                charges.append(bc)
        except Exception as e:
            print(f"  [ERROR] BTC {BTC_CHARGE_TF}: {e}")
    # v8.3: заряды снова приходят отдельными сообщениями — в них готовые ордера,
    # которые надо успеть поставить ДО движения. Часовой дайджест остаётся сводкой.
    # register_charges по-прежнему ведёт список наблюдения и журнал зарядов, но РЕШЕНИЕ о входе
    # теперь принимается по ВСЕМ зарядам скана, а не только по новым (см. process_entries).
    register_charges(charges) if (do_charge or do_btc) else []
    if do_charge:
        process_entries(charges)

    # ── 🚀 ИМПУЛЬС ── (только в окно отправки: вне окна сигнал не создаётся и не логируется)
    if not in_signal_window():
        print(f"[SCAN] вне окна отправки ({windows_txt()} МСК) — ИМПУЛЬС не шлём (заряды/уклон не затронуты)")
        return len(charges), btc_chg_15
    longs  = [r["momentum"] for r in results if r["momentum"] and r["momentum"]["side"] == "long"]
    shorts = [r["momentum"] for r in results if r["momentum"] and r["momentum"]["side"] == "short"]
    sent = 0
    for group, is_long in ((longs, True), (shorts, False)):
        if not group:
            continue
        # FIX v8: сортируем по полной оценке, а не по упрощённой quality
        for s in group:
            s["score"] = analyze_signal(s, is_long, btc_chg_15)[0]
        # v8.1: только 🟡/🟢 и не дублируем свежий ⚡ПРОБОЙ по той же монете
        def recent_breakout(sym):
            return any(k[0] == "breakout" and k[1] == sym and now_ts - v[0] < BREAKOUT_COOLDOWN_MIN * 60
                       for k, v in LAST_SENT.items())
        group = [s for s in group if s["score"] >= MOMENTUM_MIN_SCORE and not recent_breakout(s["symbol"])]
        group.sort(key=lambda x: (x["score"], x["quality"], abs(x["decorr"])), reverse=True)
        side = "long" if is_long else "short"
        top = []
        for s in group:
            last = LAST_SENT.get(("momentum", s["symbol"], side))
            if last and now_ts - last[0] < MOMENTUM_COOLDOWN_MIN * 60 and s["score"] < last[1] + 2:
                continue
            ok, why = gate_allows(s["symbol"], side)        # v8.3: те же правила, что у пробоя
            if not ok:
                print(f"[MOMENTUM] {s['symbol']} {side}: пропуск — {why}")
                continue
            vw_ok, vw_txt, _ = vwap_filter(s["symbol"], s["price"])
            if not vw_ok:
                print(f"[MOMENTUM] {s['symbol']} {side}: пропуск — {vw_txt}")
                continue
            s["vwap_txt"] = vw_txt
            top.append(s)
            if len(top) >= TOP_N:
                break
        if not top:
            continue
        top = [refresh_signal(s, is_long) for s in top]
        header = (f"📡 <b>{'🚀 ИМПУЛЬС — ЛОНГ' if is_long else '🚀 ИМПУЛЬС — ШОРТ'}</b> | {msk_time_str()}\n"
                  f"{market_ctx}\n")
        blocks = [header]
        for i, s in enumerate(top):
            score, verdict, plus, minus = analyze_signal(s, is_long, btc_chg_15)
            blocks.append(format_momentum(s, i, is_long, score, verdict, plus, minus))
            log_signal("momentum", s, side, score)
            gate_register(s["symbol"], side)
            LAST_SENT[("momentum", s["symbol"], side)] = (now_ts, score)
            sent += 1
        send_blocks(blocks)

    print(f"[SCAN] Лонгов: {len(longs)} | Шортов: {len(shorts)} | Зарядов: {len(charges)} | Watchlist: {len(WATCHLIST)}")
    return sent + len(charges), btc_chg_15

# ─── КОМАНДЫ В ЧАТЕ ───────────────────────────────────────────────────────────
# Бот читает сообщения в том же чате, чтобы сравнить «что обещал сигнал»
# с «что реально получилось»: цену исполнения, факт входа и результат.

TG_READ = {"fails": 0, "quiet_until": 0.0, "conflicts": 0}

def tg_drop_webhook():
    """Если у бота стоит webhook, getUpdates не работает (ошибка 409).
    Снимаем его один раз при старте — команды в чате должны читаться."""
    if not TELEGRAM_TOKEN:
        return
    try:
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook",
                         params={"drop_pending_updates": "false"}, timeout=10)
        if r.ok and r.json().get("ok"):
            print("[TG] webhook снят (если был) — команды читаются через getUpdates")
    except Exception as e:
        print(f"[TG] не удалось снять webhook: {e}")

def tg_updates():
    """Новые сообщения из чата (короткий опрос, никаких доп. настроек не нужно)."""
    if not TELEGRAM_TOKEN or time.time() < TG_READ["quiet_until"]:
        return []
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    try:
        r = requests.get(url, params={"offset": TG_OFFSET[0] + 1, "timeout": 0, "limit": 20}, timeout=10)
        r.raise_for_status()
        data = r.json()
        TG_READ["fails"] = 0
        TG_READ["conflicts"] = 0
        out = []
        for u in data.get("result", []) if data.get("ok") else []:
            TG_OFFSET[0] = max(TG_OFFSET[0], u.get("update_id", 0))
            msg = u.get("message") or u.get("channel_post") or {}
            if str(msg.get("chat", {}).get("id")) != str(CHAT_ID):
                continue
            doc = msg.get("document")
            if doc and "pending" in (doc.get("file_name") or "").lower():
                out.append(("__doc__", doc.get("file_id")))   # файл очереди оценки
                continue
            text = (msg.get("text") or "").strip()
            if text:
                out.append(text)
        return out
    except requests.HTTPError as e:
        # 409 = сообщения уже читает другой экземпляр бота. При деплое Render
        # ненадолго держит два контейнера — это нормально и проходит само.
        if getattr(e.response, "status_code", 0) == 409:
            TG_READ["conflicts"] += 1
            TG_READ["quiet_until"] = time.time() + 60
            if TG_READ["conflicts"] in (1, 5, 20):
                print(f"[TG] 409: команды читает другой экземпляр бота "
                      f"(обычно старый контейнер при деплое). Попытка {TG_READ['conflicts']}.")
            if TG_READ["conflicts"] == 20:
                send_telegram("⚠️ Команды в чате не читаются уже 20 минут: похоже, "
                              "запущено два экземпляра бота. Проверь, что в Render "
                              "работает только один сервис.")
        return []
    except Exception as e:
        # не спамим логом: после трёх неудач подряд молчим 10 минут
        TG_READ["fails"] += 1
        if TG_READ["fails"] <= 3:
            print(f"[TG READ ERROR] {e}")
        else:
            TG_READ["quiet_until"] = time.time() + 600
            TG_READ["fails"] = 0
            print("[TG READ] команды временно отключены на 10 мин (ошибки чтения)")
        return []

def last_signal_for(sym: str):
    best = None
    for s in SENT_SIGNALS:
        if s["symbol"] == sym.upper() and (best is None or s["ts"] > best["ts"]):
            best = s
    return best

MY_TRADE_COLS = ("symbol", "side", "score", "how", "fill", "sig_price", "slip_pct",
                 "exit", "tp1_done", "pnl_pct", "pnl_usd", "pos_usd", "note")

def log_my_trade(row: dict):
    """Набор столбцов фиксирован: иначе строка с лишним полем (например, причина
    пропуска) заставляет файл пересоздаться, и прежние сделки уезжают в архив."""
    full = {k: row.get(k, "") for k in MY_TRADE_COLS}
    full["ver"] = BOT_VERSION
    full["time_msk"] = msk_time_str()
    full["date"] = datetime.now(MSK).strftime("%Y-%m-%d")
    _append_csv(TRADES_CSV, full)

def cmd_fill(sym, price):
    sig = last_signal_for(sym)
    if not sig:
        return f"Не нашёл сигнала по {sym.upper()} — команда работает после ⚡ПРОБОЯ."
    slip = (price - sig["price"]) / sig["price"] * 100 * (1 if sig["side"] == "long" else -1)
    MY_TRADES[sym.upper()] = {"sym": sym.upper(), "side": sig["side"], "fill": price,
                              "sig_price": sig["price"], "stop": sig["stop"], "tp1": sig["tp1"],
                              "tp2": sig["tp2"], "stop_pct": sig["stop_pct"], "slip": slip,
                              "score": sig["score"], "ts": time.time(), "tp1_done": False}
    SLIPPAGE.append(slip)
    avg = sum(SLIPPAGE) / len(SLIPPAGE)
    worse = " (хуже, чем в сигнале)" if slip > 0 else ""
    return (f"✅ <b>{sym.upper()}</b>: в сигнале {sig['price']:.6g}, у тебя {price:.6g}\n"
            f"Проскальзывание {slip:+.3f}%{worse}\n"
            f"Среднее за {len(SLIPPAGE)} сделок: {avg:+.3f}% | в бэктест заложено 0.050%")

def close_my_trade(sym, exit_price=None, how="out"):
    t = MY_TRADES.pop(sym.upper(), None)
    if not t:
        return f"По {sym.upper()} нет открытой сделки — сначала /fill."
    if exit_price is None:
        exit_price = t["stop"] if how == "stop" else t["tp1"]
    sign = 1 if t["side"] == "long" else -1
    part = (exit_price - t["fill"]) / t["fill"] * 100 * sign
    if t["tp1_done"]:
        tp1_pct = (t["tp1"] - t["fill"]) / t["fill"] * 100 * sign
        pnl_pct = tp1_pct * 0.5 + (0.0 if how == "stop" else part * 0.5)
    else:
        pnl_pct = part
    pos = min(RISK_USD / (t["stop_pct"] / 100), MAX_POS_USD) if t["stop_pct"] else 0
    pnl_usd = pos * pnl_pct / 100
    log_my_trade({"symbol": t["sym"], "side": t["side"], "score": t["score"], "fill": t["fill"],
                  "sig_price": t["sig_price"], "slip_pct": round(t["slip"], 3), "exit": exit_price,
                  "how": how, "tp1_done": t["tp1_done"], "pnl_pct": round(pnl_pct, 3),
                  "pnl_usd": round(pnl_usd, 2), "pos_usd": round(pos)})
    if pnl_usd <= 0:
        gate_loss()
        if how == "stop" and t.get("kind") == "tilt":
            TILT_LAST_STOP[f"{t['sym']}:{t['side']}"] = exit_price
    mark = "🟢" if pnl_usd > 0 else "🔴"
    return (f"{mark} <b>{t['sym']}</b> закрыта по {exit_price:.6g}\n"
            f"{pnl_pct:+.2f}% от входа ≈ <b>${pnl_usd:+.2f}</b> (позиция ${pos:,.0f})")

def cmd_stat():
    rows = []
    try:
        with open(TRADES_CSV, encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r.get("how") != "skip"]
    except Exception:
        pass
    skipped = 0
    try:
        with open(TRADES_CSV, encoding="utf-8") as f:
            skipped = sum(1 for r in csv.DictReader(f) if r.get("how") == "skip")
    except Exception:
        pass
    if not rows:
        return ("Сделок пока нет.\nПосле входа: /fill SEI 0.28462 | после выхода: /out SEI 0.2901"
                + (f"\nПропущено сигналов: {skipped}" if skipped else ""))
    today = datetime.now(MSK).strftime("%Y-%m-%d")
    def block(rs, name):
        if not rs:
            return f"{name}: сделок нет"
        pnl = sum(float(r["pnl_usd"]) for r in rs)
        win = sum(1 for r in rs if float(r["pnl_usd"]) > 0)
        return f"{name}: {len(rs)} сд. | в плюс {win} ({win / len(rs) * 100:.0f}%) | итог <b>${pnl:+.2f}</b>"
    lines = ["📊 <b>Мои сделки</b>",
             block([r for r in rows if r["date"] == today], "Сегодня"),
             block(rows, "Всего")]
    slips = [float(r["slip_pct"]) for r in rows if r.get("slip_pct")]
    if slips:
        lines.append(f"Проскальзывание: среднее {sum(slips) / len(slips):+.3f}%, "
                     f"худшее {max(slips, key=abs):+.3f}% (в бэктесте 0.050%)")
    if skipped:
        lines.append(f"Пропущено сигналов: {skipped}")
    if MY_TRADES:
        lines.append("Сейчас открыты: " + ", ".join(MY_TRADES))
    return "\n".join(lines)

def cmd_watch():
    if not WATCHLIST:
        return "В зарядке пусто."
    out = ["⏳ <b>В зарядке:</b>"]
    for s, w in sorted(WATCHLIST.items(), key=lambda x: -x[1]["score"]):
        side = {"long": "🟢", "short": "🔴"}.get(w["side"], "⚪")
        out.append(f"   {side} {s} {w['P']['tf']} | сила {w['score']}/{ACC_MAX_SCORE} | "
                   f"{w['lo']:.6g}–{w['hi']:.6g}")
    return "\n".join(out)

HELP_TEXT = (
    "<b>📊 СТАТИСТИКА С БИРЖИ</b>\n"
    "/hist — результаты закрытых сделок: по дням, по монетам, в единицах риска\n"
    "        <i>/hist 30 — за 30 дней (по умолчанию 7). Эти данные не теряются при перезапуске</i>\n"
    "/up — версия, режим, эквити, открытые позиции\n"
    "/risk — баланс, просадка, до лимитов\n"
    "/riskraw — то же сырым ответом API (для разбора проблем)\n"
    "/btc — уклон биткоина сейчас: от него зависит, лонги или шорты берём\n"
    "\n<b>⚙️ УПРАВЛЕНИЕ</b>\n"
    "/halt — пауза: новых входов не будет, открытые позиции и их ордера остаются\n"
    "/resume — снять паузу\n"
    "/closeall — закрыть все позиции на демо\n"
    "/uptest — тест связи: открыть и сразу закрыть BTC на демо\n"
    "\n<b>📒 ЖУРНАЛЫ</b>\n"
    "/log — прислать все файлы прямо сейчас\n"
    "/restore — вернуть очередь оценки (перешли боту файл pending_v9.json)\n"
    "/watch — что сейчас в зарядке\n"
    "\n<b>✍️ РУЧНОЙ УЧЁТ</b> <i>(если вмешиваешься в сделку руками)</i>\n"
    "/fill SEI 0.28462 — реальная цена входа\n"
    "/tp1 SEI — забрал первую цель\n"
    "/out SEI 0.2901 — закрыл остаток\n"
    "/stop SEI — выбило стопом\n"
    "/skip SEI причина — сигнал пропустил\n"
    "/stat — мои сделки и проскальзывание")


# ── Авто-слой Upscale (v0: dry, ордера не отправляет). AUTO_TRADE=off|dry в переменных Render ──
EXECUTOR = upscale_exec.Executor(send_telegram, lambda row: _append_csv(EXEC_CSV, row), RISK_USD, MAX_POS_USD, UPSCALE_PAIRS + ["BTC"])

def handle_command(text: str) -> str:
    parts = text.replace(",", ".").split()
    if not parts or not parts[0].startswith("/"):
        return ""
    cmd = parts[0].lower().split("@")[0]
    arg = parts[1].upper() if len(parts) > 1 else ""
    num = None
    if len(parts) > 2:
        try:
            num = float(parts[2])
        except ValueError:
            num = None
    if cmd == "/fill":
        return cmd_fill(arg, num) if arg and num is not None else "Формат: /fill SEI 0.28462"
    if cmd == "/tp1":
        t = MY_TRADES.get(arg)
        if not t:
            return f"По {arg} нет открытой сделки."
        t["tp1_done"] = True
        return f"✅ {arg}: половина по TP1 {t['tp1']:.6g}. Стоп в безубыток — {t['fill']:.6g}."
    if cmd == "/out":
        return close_my_trade(arg, num, "out") if arg else "Формат: /out SEI 0.2901"
    if cmd == "/up":
        return EXECUTOR.status()
    if cmd == "/btc":
        b = btc_bias()
        return btc_line(b) + (f"\nVWAP {b['vwap']:.2f}, цена {b['price']:.2f}" if b.get("vwap") else "")
    if cmd in ("/hist", "/history", "/trades"):
        days = int(arg) if arg.isdigit() else 7      # /hist 30 — за 30 дней
        return EXECUTOR.history(max(1, min(60, days)))
    if cmd == "/risk":
        return EXECUTOR.risk_summary()
    if cmd == "/riskraw":
        return EXECUTOR.risk_dump()
    if cmd == "/uptest":
        return EXECUTOR.selftest()
    if cmd == "/closeall":
        return EXECUTOR.closeall()
    if cmd == "/halt":
        return EXECUTOR.halt()
    if cmd == "/resume":
        return EXECUTOR.resume()
    if cmd == "/stop":
        return close_my_trade(arg, num, "stop") if arg else "Формат: /stop SEI"
    if cmd == "/skip":
        sig = last_signal_for(arg)
        log_my_trade({"symbol": arg, "side": sig["side"] if sig else "", "how": "skip",
                      "score": sig["score"] if sig else "", "pnl_pct": 0, "pnl_usd": 0,
                      "note": " ".join(parts[2:])})
        return f"➖ {arg}: отмечен как пропущенный."
    if cmd == "/stat":
        return cmd_stat()
    if cmd == "/watch":
        return cmd_watch()
    if cmd == "/restore":
        return ("♻️ Пришли файл pending_v9.json (он приходит вместе с журналами) "
                "ОДНИМ сообщением, и я подниму очередь оценки. Либо просто "
                "перешли его сюда — я подхвачу сам.")
    if cmd in ("/log", "/files", "/journal"):
        today = datetime.now(MSK).strftime("%Y-%m-%d")
        sent = 0
        for path, cap in ((SIGNALS_CSV, "сигналы"), (OUTCOMES_CSV, "исходы сигналов"),
                          (CHARGES_CSV, "заряды"), (CHARGE_OUTCOMES_CSV, "исходы зарядов"),
                          (TRADES_CSV, "мои сделки"), (EXEC_CSV, "авто-слой")):
            if os.path.exists(path) and os.path.getsize(path) > 0:
                send_document(path, f"{today} — {cap}")
                sent += 1
        return f"📒 Отправлено файлов: {sent}" if sent else "📒 Журналы пока пустые."
    if cmd in ("/help", "/start"):
        return HELP_TEXT
    return ""

def resend_pending_charges():
    """Заряды, найденные вне окна, досылаются при его открытии — чтобы успеть
    поставить ордера до пробоя."""
    if not in_signal_window():
        return
    now_ts = time.time()
    pend = [w for w in WATCHLIST.values()
            if not w.get("alerted", True) and w["expires"] > now_ts]
    if not pend:
        return
    pend.sort(key=lambda w: -w["score"])
    send_telegram(f"🔔 <b>Окно открылось</b> — заряды, найденные раньше ({len(pend)} шт.):")
    for w in pend:
        send_blocks(format_charge(w).split("\n"))
        w["alerted"] = True

def poll_commands():
    for text in tg_updates():
        if isinstance(text, tuple) and text[0] == "__doc__":
            send_telegram(restore_from_telegram(text[1]))
            continue
        try:
            answer = handle_command(text)
        except Exception as e:
            traceback.print_exc()
            answer = f"Ошибка команды: {esc(str(e))}"
        if answer:
            print(f"[CMD] {text}")
            send_telegram(answer)

# ─── СТАТУС ───────────────────────────────────────────────────────────────────

def send_status(signal_count=0, btc_chg=None):
    """Часовой дайджест: что в зарядке и куда уклон + что делает BTC за 12ч.
    Отдельных алертов по каждому ЗАРЯДу в v8.2 нет."""
    now = datetime.now(MSK)
    lines = [f"🤖 <b>Upscale Bot {BOT_VERSION}</b> | {now.strftime('%H:%M МСК')}",
             get_market_context().lstrip("\n")]
    if WATCHLIST:
        lines.append(f"\n⏳ <b>В зарядке ({len(WATCHLIST)}):</b> "
                     f"<i>найдены за последние {WATCH_TTL_HOURS}ч; решение по входу "
                     f"принималось в момент находки</i>")
        for s, w in sorted(WATCHLIST.items(), key=lambda x: -x[1]["score"]):
            side = {"long": "🟢 уклон вверх", "short": "🔴 уклон вниз"}.get(w["side"], "⚪ уклон неясен")
            tf = w["P"]["tf"] if "P" in w else CHARGE_TF
            lines.append(f"   {s} {tf} — {side} | сила {w['score']}/{ACC_MAX_SCORE} | "
                         f"{w['lo']:.6g}–{w['hi']:.6g} ({w['rng_pct']:.2f}%)")
    else:
        lines.append("\n⏳ В зарядке: пусто")
    lines.append(f"\n📨 Сигналы: {windows_txt()} МСК" +
                 ("  ✅ сейчас окно" if in_signal_window() else "  ⏸ сейчас вне окна"))
    lines.append(f"Ожидают оценки: сигналов {len(PENDING_OUTCOMES)}, зарядов {len(CHARGE_PENDING)}")
    send_telegram("\n".join(lines))

# ─── ГЛАВНЫЙ ЦИКЛ ─────────────────────────────────────────────────────────────

def pending_save():
    """Очередь оценки живёт в памяти, а на Render файловая система стирается при
    КАЖДОМ перезапуске. Из-за этого исходы сигналов терялись безвозвратно — поэтому
    очередь пишем в файл и шлём его в телеграм вместе с журналами. Восстановить
    автоматически бот не может (свои же сообщения ему недоступны), но файл можно
    прислать обратно командой /restore — см. ниже."""
    try:
        with open(PENDING_FILE, "w", encoding="utf-8") as f:
            json.dump({"signals": PENDING_OUTCOMES, "charges": CHARGE_PENDING,
                       "saved_at": int(time.time()), "ver": BOT_VERSION}, f, ensure_ascii=False)
    except Exception as e:
        print(f"[PENDING] не сохранил: {e}")

def pending_load_local():
    """Пробуем поднять очередь с диска: помогает, когда процесс перезапустился
    без пересборки контейнера."""
    try:
        if not os.path.exists(PENDING_FILE):
            return 0
        with open(PENDING_FILE, encoding="utf-8") as f:
            d = json.load(f)
        now = time.time()
        n = 0
        for p in d.get("signals", []):
            if p.get("ts", 0) + p.get("horizon", OUTCOME_HORIZON) > now and \
               not any(x["id"] == p.get("id") for x in PENDING_OUTCOMES):
                PENDING_OUTCOMES.append(p)
                n += 1
        for ep in d.get("charges", []):
            if ep.get("ts", 0) + ep.get("eval_window", 24 * 3600) > now and \
               not any(x["id"] == ep.get("id") for x in CHARGE_PENDING):
                CHARGE_PENDING.append(ep)
                n += 1
        return n
    except Exception as e:
        print(f"[PENDING] не восстановил: {e}")
        return 0

def restore_from_telegram(file_id: str) -> str:
    """Скачиваем присланный файл очереди и поднимаем из него оценку."""
    try:
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile",
                         params={"file_id": file_id}, timeout=10)
        p = r.json()["result"]["file_path"]
        data = requests.get(f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{p}", timeout=20)
        tmp = os.path.join(LOG_DIR, "_restore.json")
        with open(tmp, "wb") as f:
            f.write(data.content)
        return pending_restore_from_file(tmp)
    except Exception as e:
        return f"⚠️ Не смог скачать файл: {e}"

def pending_restore_from_file(path: str) -> str:
    """Восстановление из присланного файла (команда /restore)."""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:
        return f"⚠️ Не смог прочитать файл: {e}"
    now, n = time.time(), 0
    for p in d.get("signals", []):
        if p.get("ts", 0) + p.get("horizon", OUTCOME_HORIZON) > now and \
           not any(x["id"] == p.get("id") for x in PENDING_OUTCOMES):
            PENDING_OUTCOMES.append(p)
            n += 1
    for ep in d.get("charges", []):
        if ep.get("ts", 0) + ep.get("eval_window", 24 * 3600) > now and \
           not any(x["id"] == ep.get("id") for x in CHARGE_PENDING):
            CHARGE_PENDING.append(ep)
            n += 1
    pending_save()
    return (f"♻️ Восстановлено записей: {n} (сигналов {len(PENDING_OUTCOMES)}, "
            f"зарядов {len(CHARGE_PENDING)}). Истёкшие пропущены.")

def send_logs(caption_prefix: str):
    """Отправляет журналы в Telegram. Вызывается и по сводке, и ПЕРЕД перезапуском —
    на Render файлы стираются при каждом деплое, иначе статистика теряется."""
    today = datetime.now(MSK).strftime("%Y-%m-%d %H:%M")
    for path, cap in ((SIGNALS_CSV, "сигналы"), (OUTCOMES_CSV, "исходы сигналов"),
                      (CHARGES_CSV, "заряды"), (CHARGE_OUTCOMES_CSV, "исходы зарядов"),
                      (TRADES_CSV, "мои сделки"), (EXEC_CSV, "авто-слой"), (PENDING_FILE, "ОЧЕРЕДЬ ОЦЕНКИ — пришли обратно и набери /restore")):
        send_document(path, f"{caption_prefix} {today} — {cap}")

def _on_shutdown(signum, frame):
    """Render присылает SIGTERM перед перезапуском — успеваем сохранить статистику."""
    print(f"[SHUTDOWN] сигнал {signum}: отправляю журналы перед остановкой")
    try:
        send_telegram("♻️ <b>Бот перезапускается</b> — отправляю журналы, "
                      "чтобы статистика не потерялась при деплое.")
        send_logs("перед перезапуском")
    except Exception as e:
        print(f"[SHUTDOWN ERROR] {e}")
    raise SystemExit(0)

def main():
    tg_drop_webhook()
    _signal.signal(_signal.SIGTERM, _on_shutdown)
    _signal.signal(_signal.SIGINT, _on_shutdown)
    # Разовый прогон бэктеста: в Render добавить переменную окружения RUN_BACKTEST=1,
    # дождаться результатов в Telegram, затем убрать переменную (иначе он будет
    # запускаться при каждом перезапуске). После бэктеста бот продолжает работать как обычно.
    bt_mode = (os.environ.get("RUN_BACKTEST") or "").strip().lower()
    _g = gate_load()
    if _g:
        print(f"[GATE] поднял дневной гейт: {_g} монет уже торговались сегодня")
    _restored = pending_load_local()
    if _restored:
        print(f"[PENDING] поднял с диска записей: {_restored}")
    print(f"[BACKTEST] RUN_BACKTEST={bt_mode!r} → " +
          ("сравнение стратегий (bt_compare.py)" if bt_mode in ("compare", "2", "cmp")
           else "РАЗБОР ПО ПАРАМ (bt_pairs.py)" if bt_mode in ("pairs", "17")
           else "ФИЛЬТР ПО BTC (bt_btc.py)" if bt_mode in ("btc", "16")
           else "ПРОБОЙ VWAP (bt_vbreak.py)" if bt_mode in ("vbreak", "15")
           else "VWAP КАК МАГНИТ (bt_vwap.py)" if bt_mode in ("vwap", "14")
           else "СТОП И ПОДТЯЖКА (bt_stop.py)" if bt_mode in ("stop", "13")
           else "ТАЙМФРЕЙМЫ ЗАРЯДА (bt_tf.py)" if bt_mode in ("tf", "12")
           else "АУДИТ ДОПУЩЕНИЙ (bt_audit.py)" if bt_mode in ("audit", "11")
           else "СХЕМА ВХОДА (bt_entry.py)" if bt_mode in ("entry", "10")
           else "ОСЛАБЛЕНИЯ (bt_loose.py)" if bt_mode in ("loose", "9")
           else "ДИАГНОСТИКА зарядов (bt_why.py)" if bt_mode in ("why", "8")
           else "ДЛИННЫЙ бэктест (bt_long.py)" if bt_mode in ("long", "7")
           else "ПЕРЕБОР параметров (bt_sweep.py)" if bt_mode in ("sweep2", "6", "params")
           else "СДЕЛКИ по мелким свечам (bt_trades2.py)" if bt_mode in ("trades2", "5")
           else "СДЕЛКИ (bt_trades.py)" if bt_mode in ("trades", "4", "trade")
           else "потолок диапазона (bt_range.py)" if bt_mode in ("range", "3", "rng")
           else "перебор настроек (backtest.py)" if bt_mode in ("1", "true", "yes", "on", "sweep")
           else "новые стратегии (bt_new.py)" if bt_mode == "new"
           else "dip-harvester v1 (bt_final.py)" if bt_mode == "final"
           else "dip-harvester v2 денежная (bt_final2.py)" if bt_mode == "final2"
           else "новые сигналы (bt_signals.py)" if bt_mode == "signals"
           else "не запускаю"))
    if bt_mode in ("1", "true", "yes", "on", "sweep", "compare", "2", "cmp", "range", "3", "rng", "trades", "4", "trade", "trades2", "5", "sweep2", "6", "params", "long", "7", "why", "8", "loose", "9", "entry", "10", "audit", "11", "tf", "12", "stop", "13", "vwap", "14", "vbreak", "15", "btc", "16", "pairs", "17", "new", "final", "final2", "signals"):
        # Защита от повторов: если контейнер перезапустится (нехватка памяти, сбой,
        # деплой), бэктест не начнётся заново — метка о запуске лежит рядом с логами.
        mark = os.path.join(LOG_DIR, "backtest_done.txt")
        today = datetime.now(MSK).strftime("%Y-%m-%d")
        done = ""
        try:
            with open(mark, encoding="utf-8") as f:
                done = f.read().strip()
        except Exception:
            pass
        if done == today:
            print(f"[BACKTEST] сегодня уже запускался ({done}) — пропуск")
            send_telegram("ℹ️ Бэктест сегодня уже отрабатывал — повторно не запускаю.\n"
                          "Если нужен ещё один прогон, убери и снова добавь RUN_BACKTEST.")
        else:
            try:
                with open(mark, "w", encoding="utf-8") as f:
                    f.write(today)
            except Exception:
                pass
            try:
                if bt_mode in ("pairs", "17"):
                    import bt_pairs
                    bt_pairs.main()        # разбор по парам и группам
                elif bt_mode in ("btc", "16"):
                    import bt_btc
                    bt_btc.main()          # фильтр по уклону BTC
                elif bt_mode in ("vbreak", "15"):
                    import bt_vbreak
                    bt_vbreak.main()       # сигнал по пробою VWAP
                elif bt_mode in ("vwap", "14"):
                    import bt_vwap
                    bt_vwap.main()         # VWAP как магнит
                elif bt_mode in ("stop", "13"):
                    import bt_stop
                    bt_stop.main()         # шаг 1: стоп и подтяжка
                elif bt_mode in ("tf", "12"):
                    import bt_tf
                    bt_tf.main()           # на каком ТФ искать заряд
                elif bt_mode in ("audit", "11"):
                    import bt_audit
                    bt_audit.main()        # аудит допущений бэктеста
                elif bt_mode in ("entry", "10"):
                    import bt_entry
                    bt_entry.main()        # новая схема входа с подтверждением
                elif bt_mode in ("loose", "9"):
                    import bt_loose
                    bt_loose.main()        # что даст ослабление условий заряда
                elif bt_mode in ("why", "8"):
                    import bt_why
                    bt_why.main()          # почему монета не стала зарядом
                elif bt_mode in ("long", "7"):
                    import bt_long
                    bt_long.main()         # длинная история, только 1h
                elif bt_mode in ("sweep2", "6", "params"):
                    import bt_sweep
                    bt_sweep.main()        # перебор параметров (пункты 4-7)
                elif bt_mode in ("trades2", "5"):
                    import bt_trades2
                    bt_trades2.main()      # v3: сделка по мелким свечам
                elif bt_mode in ("trades", "4", "trade"):
                    import bt_trades
                    bt_trades.main()       # v8.8: полноценный бэктест сделок
                elif bt_mode in ("range", "3", "rng"):
                    import bt_range
                    bt_range.main()        # v8.8: проверка адаптивного потолка диапазона
                elif bt_mode in ("compare", "2", "cmp"):
                    import bt_compare
                    bt_compare.main()      # сравнение стратегий
                elif bt_mode == "new":
                    import bt_new
                    bt_new.main()          # новые стратегии: funding fade, OI div, weekly
                elif bt_mode == "final":
                    import bt_final
                    bt_final.main()        # dip-harvester v1 (старая метрика)
                elif bt_mode == "final2":
                    import bt_final2
                    bt_final2.main()       # dip-harvester v2: денежная метрика, исправлена
                elif bt_mode == "signals":
                    import bt_signals
                    bt_signals.main()      # новые сигналы: liq cascade, xmom, vol anomaly
                else:
                    import backtest
                    backtest.main()        # перебор настроек нашей стратегии
            except MemoryError:
                traceback.print_exc()
                send_telegram("⚠️ Бэктесту не хватило памяти. Уменьши BT_PAIRS (например 30) "
                              "или BT_DAYS.\nБот работает в обычном режиме.")
            except Exception as e:
                traceback.print_exc()
                send_telegram(f"⚠️ Бэктест не отработал: {esc(str(e))}\nБот продолжает работу в обычном режиме.")

    start_lines = [
        f"🚀 <b>Upscale Bot {BOT_VERSION}</b> | режим: <b>{EXECUTOR.mode}</b>"
        + {"dry": " (только сообщения)", "demo": " (ордера на ДЕМО-счёт)"}.get(EXECUTOR.mode, ""),
        "",
        f"⏳ <b>ЗАРЯД</b> {CHARGE_TF} — сжатие при стоящей цене, коридор {WIN_TXT}, "
        f"скан в {scan_minutes_txt()} — на каждом проверяются ВСЕ заряды заново",
        f"   сила ≥{ACC_MIN_SCORE}/{ACC_MAX_SCORE} | объём ≥{ACC_RVOL_MIN}× | "
        f"размах ≤{ACC_RANGE_ATR_K}×ATR×√окно (до {ACC_MAX_RANGE_ABS}%)",
        "",
        f"🎯 <b>УКЛОН</b> — вход по рынку на скане (ПРОБОЙ отключён)",
        (f"₿ <b>Фильтр BTC:</b> выше своего VWAP на ≥{BTC_VWAP_MARGIN_PCT}% → только лонги, "
         f"ниже → только шорты, между → пропуск. Уклон пары неясен — берём направление BTC")
        if BTC_FILTER_ENABLED else "₿ Фильтр BTC выключен",
        (f"📍 <b>Сторона VWAP пары:</b> лонг только когда цена пары выше её VWAP"
         + (f" на ≥{PAIR_VWAP_SIDE_MARGIN}%" if PAIR_VWAP_SIDE_MARGIN else "")
         + ", шорт — ниже. Касается и зарядов без уклона")
        if PAIR_VWAP_SIDE_ENABLED else "📍 Сторона VWAP пары не проверяется",
        f"   ход до границы ≥{TILT_MIN_DIST_PCT}% | VWAP пары ≤"
        f"{VWAP_ALIGNED_MAX_ATR if PAIR_VWAP_MODE == 'aligned' else VWAP_MAX_ATR} ATR",
        f"   стоп {TILT_STOP_PCT}% | цели по трети: {TILT_TP1_PCT}% → граница → {TILT_TP3_PCT}%",
        f"   стоп подтягивается после первой и второй цели",
        "",
        f"🛡 <b>Ограничения:</b> "
        + ("1 монета в день | " if ONE_PER_SYMBOL_DAY else "")
        + (f"≤{MAX_SAME_SIDE_30M} в одну сторону | " if MAX_SAME_SIDE_30M else "")
        + f"стоп дня после {DAY_STOP_LOSSES} убытков",
        f"💰 Риск ${RISK_USD:.0f} на сделку | дневной лимит ${DAY_LOSS_USD:.0f}",
    ]
    if EXCLUDE_SYMBOLS:
        start_lines.append(f"🚫 Не торгуем: {', '.join(sorted(EXCLUDE_SYMBOLS))}")
    start_lines += [
        "",
        f"📨 Окно сигналов: {windows_txt()} МСК | сводка {SUMMARY_HHMM[0]:02d}:{SUMMARY_HHMM[1]:02d}",
        f"📊 Пар: {len(UPSCALE_PAIRS)}",
        "💬 /help — все команды | /up статус | /halt пауза",
    ]
    send_telegram("\n".join(start_lines))

    last_signal_count, last_btc = 0, None
    status_sent_hour = -1
    last_scan_key = None
    last_fast = 0.0
    last_outcomes = 0.0
    summary_sent_date = None

    while True:
        try:
            poll_commands()          # команды из чата (/fill, /out, /stat) — в любое время суток
            now_msk = datetime.now(MSK)

            # часовой дайджест: пары в зарядке + BTC 12ч (заменил алерты по каждому ЗАРЯДу)
            # v10.0: часовой дайджест «В зарядке (N)» убран — он путал (накопительный список за
            # 16 часов) и дублировал сводку скана, которая теперь приходит после каждого :30.
            # Список наблюдения по-прежнему доступен командой /watch.

            today = now_msk.strftime("%Y-%m-%d")
            if (now_msk.hour, now_msk.minute) >= SUMMARY_HHMM and summary_sent_date != today:
                send_daily_summary()
                summary_sent_date = today

            if is_trading_hours():
                # v10.0: сетка сканов сдвинута на CHARGE_SCAN_OFFSET_MIN (по умолчанию :30)
                shifted = (now_msk.minute - CHARGE_SCAN_OFFSET_MIN) % 60
                aligned = (now_msk.hour, now_msk.minute)
                # весь первый минутный отрезок после границы, а не только первые 15 сек:
                # если fast_check затянулся из-за таймаута API, скан не пропадёт на 5 минут
                if shifted % SCAN_INTERVAL == 0 and aligned != last_scan_key:
                    last_scan_key = aligned
                    result = run_scan(do_charge=(shifted % CHARGE_SCAN_MIN == 0),
                                      do_btc=False)
                    if result:
                        last_signal_count, last_btc = result
                    last_fast = time.time()
                elif WATCHLIST and time.time() - last_fast >= FAST_INTERVAL_SEC:
                    resend_pending_charges()     # досылаем заряды, найденные вне окна
                    fast_check()
                    last_fast = time.time()

            if time.time() - last_outcomes >= 60:
                process_outcomes()
                last_outcomes = time.time()

        except Exception as e:
            print(f"[LOOP ERROR] {e}")
            traceback.print_exc()
        time.sleep(2)

if __name__ == "__main__":
    main()
