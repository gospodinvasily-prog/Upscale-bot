# -*- coding: utf-8 -*-
"""
exec_donchian_regime_v42.py — живое исполнение стратегии Donchian Regime v4.4
=============================================================================
(имя файла/модуля и пути файлов состояния оставлены как есть — "v42" здесь
уже просто имя файла, а не номер версии логики; переименовывать не стали,
чтобы не терять накопленный на диске cooldown/tracked-state при деплое)

Запускается из bot.py:
  - импортируется один раз при старте бота
  - check_signals() вызывается по расписанию (раз в сутки, ~00:10 UTC —
    10 мин после закрытия дневной свечи в 00:00 UTC)
  - каждый сигнал передаётся в EXECUTOR.on_signal()

Логика идентична бэктесту bt_donchian_regime_v44.py v4.4:
  - Donchian(20) пробой
  - BTC SMA(50) режим
  - DMI + ATR фильтр (1.5%–5%)
  - Funding |rate| ≤ 0.05%
  - EXCLUDE_PAIRS (8 пар)
  - PER-PAIR COOLDOWN: 3 убытка подряд → блок 30 дней
  - MAX_NEW_PER_DAY: 2 новых входа в сутки
  - ДНЕВНОЙ СТОП -$400: при realized+unrealized убытке дня ≥ $400 (UTC) —
    новые входы блокируются до завтра (старые позиции продолжаем вести)

Выход из позиции (v4.4 — БЕЗ TP1/TP2/TP3, механика сменилась полностью):
  - trailing-стоп: 2×ATR_при_входе от максимума/минимума в пользу сделки
    с момента входа, двигается ТОЛЬКО теснее (как update_trail в бэктесте) —
    подтяжку на бирже делает EXECUTOR.move_stop() на дневном скане
  - выход по развороту сигнала: close < текущий Donchian-low (long) или
    close > текущий Donchian-high (short) — EXECUTOR.close_position()
  - выход по времени: MAX_HOLD_DAYS (15 дней) — EXECUTOR.close_position()
  - ATR фиксируется НА ВХОДЕ и не пересчитывается (как в бэктесте)

Размер позиции и лимит одновременных сделок (оба риск-зависимые):
  - compound sizing: риск = max($80, min($200, equity × 0.8%)) — старт $80
    при equity < $10000, далее растёт вместе с equity (0.8% от неё)
  - equity берётся из Upscale API (кешируется 5 минут)
  - STOP = 2×ATR
  - DD brake: если (peak - equity) > $1200 → риск × 0.5
  - Вход с плечом EXEC_LEVERAGE (по умолчанию 5×, см. upscale_exec.py) —
    маржа на позицию = размер/плечо, это и позволяет держать несколько
    сделок одновременно без перегрузки счёта по марже
  - Лимит В ОДНУ СТОРОНУ = max(1, min(3, $240 // риск)) — при риске $80
    даёт 3, итого максимум 6 одновременно (3 long + 3 short). Если риск
    вырастет вместе с equity — лимит сторон автоматически УМЕНЬШАЕТСЯ,
    чтобы суммарный риск одной стороны не уходил выше ~$240
    (см. compute_max_same_side())

Баланс Upscale:
  - EXECUTOR._snapshot() возвращает текущий equity, дневные лимиты и т.д.
  - Кеш обновляется каждые BALANCE_TTL_SEC секунд
  - При старте и перед каждым сканом — принудительное обновление
  - При невозможности получить баланс — вход БЛОКИРУЕТСЯ

Upscale Basic лимиты ($10k) и аварийная защита (upscale_exec.py) — это
ОТДЕЛЬНЫЙ, более грубый контур поверх дневного стопа -$400 выше (считает
% от лимитов СЧЁТА, а не фиксированные $400 стратегии):
  - Дневной DD: 5% ($500) → стоп НОВЫХ входов при 60% = $300,
    аварийное закрытие ВСЕХ позиций + стоп бота до след. дня UTC при
    90% = $450 (EXEC_DAY_HARD_FRAC, сторож проверяет это каждые 60с —
    не привязано к расписанию сканов)
  - Общий DD: 10% ($1000) → стоп входов при 60% = $600, аварийное
    закрытие при 70% = $700 (ручной /resume, авто-сброс только дневного)

Закрытие сделок (без ручных команд):
  - Стратегия торгует полностью автоматически — ручных /out /stop больше нет.
  - На каждом check_signals() сверяем, какие donchian-позиции исчезли из
    открытых на Upscale с прошлого раза, считаем их PnL по истории ордеров
    и сами обновляем cooldown (3 убытка подряд → блок 30 дней). Работает
    одинаково для любой причины закрытия — стоп (начальный или trailing),
    SIG, TIME.
"""

import os
import sys
import json
import math
import time
import threading
import datetime as dt
from decimal import Decimal
from typing import Optional

try:
    import bot as B
except Exception as e:
    B = None
    _BOT_IMPORT_ERR = e
else:
    _BOT_IMPORT_ERR = None

try:
    import upscale_exec as UE
except Exception as e:
    UE = None
    _UE_IMPORT_ERR = e
else:
    _UE_IMPORT_ERR = None


# =====================================================================
#  КОНСТАНТЫ — синхронизированы с bt_donchian_regime_v44.py v4.4
# =====================================================================

DONCHIAN_PERIOD   = 20
BTC_REGIME_SMA    = 50
DMI_PERIOD        = 14
ATR_PERIOD        = 14
ATR_PCT_MIN       = 0.015
ATR_PCT_MAX       = 0.05
ATR_STOP_MULT     = 2.0     # и начальный стоп, и шаг trailing-стопа (2×ATR от входа)

MAX_NEW_PER_DAY   = 2
MAX_HOLD_DAYS     = 15      # v4.4: принудительный выход по времени, если сделка висит дольше
DAILY_STOP_LOSS   = -400.0  # v4.4: убыток дня (UTC, realized+unrealized) -> блок НОВЫХ входов до завтра

# Compound sizing. Floor поднят с $40 до $80 по запросу — именно с этой
# цифры стартуем сейчас (equity < $10000). RISK_FRACTION не менялся: 0.8%
# от equity при equity=$10000 даёт ровно $80, так что с ростом баланса риск
# продолжит расти ПРОПОРЦИОНАЛЬНО этой же формуле без отдельного переключения.
RISK_FRACTION     = 0.008     # 0.8% от equity
SLOT_RISK_MIN     = 80.0      # мин. $80 — стартовый риск на сделку
SLOT_RISK_MAX     = 200.0     # макс. $200 на сделку
DD_BRAKE_THRESHOLD = 1_200.0  # порог DD-brake
DD_BRAKE_FACTOR   = 0.5       # риск × 0.5 при DD > порога
MAX_POSITION_PCT_OF_EQUITY = 0.20   # потолок размера позиции — 20% equity (как в бэктесте)

# ── Лимит одновременных позиций В ОДНУ СТОРОНУ ──────────────────────────────
# При риске $80/сделку — 3 позиции в одну сторону (anchor $240 = 3×$80),
# итого максимум 6 одновременно (3 long + 3 short — обе стороны сразу открыты
# бывают только если BTC-режим успел смениться, а старые позиции другой
# стороны ещё не закрылись). Если риск на сделку растёт вместе с equity
# (compound sizing выше), число одновременных сделок В СТОРОНУ СНИЖАЕТСЯ —
# так суммарный риск одной стороны остаётся около $240, а не растёт вместе
# с размером позиции без контроля.
SAME_SIDE_RISK_ANCHOR_USD = 240.0   # = 3 × $80 — целевой потолок риска одной стороны
MAX_SAME_SIDE_CEILING     = 3       # никогда больше 3 в одну сторону, даже если риск упадёт
MAX_CONCURRENT_CEILING    = 6       # никогда больше 6 одновременно (обе стороны вместе)


def compute_max_same_side(risk_usd: float) -> int:
    """
    Сколько позиций можно держать одновременно В ОДНУ СТОРОНУ при текущем
    риске на сделку — обратная зависимость: выше риск → меньше сделок,
    чтобы суммарный риск одной стороны не уходил выше SAME_SIDE_RISK_ANCHOR_USD.
    При риске $80 даёт ровно 3 (стартовая настройка).
    """
    if risk_usd <= 0:
        return MAX_SAME_SIDE_CEILING
    n = int(SAME_SIDE_RISK_ANCHOR_USD // risk_usd)
    return max(1, min(MAX_SAME_SIDE_CEILING, n))


EXCLUDE_PAIRS = {
    "TRX", "XLM", "BNB", "UNI", "LTC", "RUNE", "PENDLE", "HBAR",
}

CONSEC_LOSS_LIMIT = 3
COOLDOWN_DAYS     = 30

# Баланс: как часто обновляем из Upscale API
BALANCE_TTL_SEC   = 300       # 5 минут — кеш балансa
BALANCE_FORCE_ON_SCAN = True  # перед каждым check_signals() — принудительное обновление

BTC_CONTRACT = "BTC_USDT"

# Файлы персистентного состояния
COOLDOWN_FILE    = "/tmp/cooldown_donchian_v42.json"
TRADE_STATE_FILE = "/tmp/trade_state_donchian_v42.json"
OPEN_POS_FILE    = "/tmp/open_positions_donchian_v42.json"   # {symbol: {opened_ts, side}} — для авто-детекции закрытия


def _atomic_write_json(path: str, data) -> None:
    """
    Пишет JSON атомарно: во временный файл + os.replace(). Прямая запись
    (open(path, "w") + json.dump) оставляет файл частично записанным, если
    процесс убьют посреди (OOM на Render — ровно то, от чего есть защита в
    других местах этого же файла). Все _load_*() при любой ошибке чтения
    (включая битый JSON) молча возвращают {} — то есть повреждённая запись
    теряет ВСЁ состояние (например, cooldown по паре, заблокированной за
    3 убытка подряд, просто исчезает), а не только последнее изменение.
    os.replace на одной файловой системе — атомарная операция ОС, частично
    записанного результата не бывает.
    """
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


# =====================================================================
#  КЕШ БАЛАНСА
# =====================================================================

_balance_cache = {
    "equity":     None,   # Decimal — текущий эквити
    "peak":       None,   # Decimal — пик эквити (для DD-brake)
    "day_start":  None,   # Decimal — эквити начало дня
    "base":       None,   # Decimal — начало периода
    "day_lim":    None,   # Decimal — лимит дневной просадки $
    "tot_lim":    None,   # Decimal — лимит общей просадки $
    "dd_pct":     None,   # % дневной DD
    "td_pct":     None,   # % общей DD
    "updated_at": 0.0,    # timestamp последнего обновления
    "error":      None,   # строка ошибки если не удалось
    "lock":       threading.Lock(),
}

# Отдельно храним пик equity (между обновлениями не сбрасывается)
_peak_equity_file = "/tmp/peak_equity_dr42.json"


def _load_peak() -> float:
    try:
        if os.path.exists(_peak_equity_file):
            with open(_peak_equity_file) as f:
                return float(json.load(f).get("peak", 0))
    except Exception:
        pass
    return 0.0


def _save_peak(peak: float):
    try:
        _atomic_write_json(_peak_equity_file, {"peak": peak})
    except Exception as e:
        print(f"[exec_dr42] peak save error: {e}")


def refresh_balance(executor=None, force=False) -> bool:
    """
    Обновляет кеш баланса из Upscale API.
    Возвращает True если успешно, False если ошибка.
    force=True — обновить даже если кеш свежий.
    """
    with _balance_cache["lock"]:
        age = time.time() - _balance_cache["updated_at"]
        if not force and age < BALANCE_TTL_SEC and _balance_cache["equity"] is not None:
            return True   # кеш свежий

    # Ищем executor если не передан
    if executor is None and B is not None:
        executor = getattr(B, "EXECUTOR", None)
    if executor is None:
        with _balance_cache["lock"]:
            _balance_cache["error"] = "EXECUTOR не найден"
        return False

    try:
        # _snapshot() читает self.account_id/self.acc — они заполняются в
        # _ensure_account(). Если это первый вызов после старта бота (до
        # первого /up или сигнала), поля ещё пустые — без этого вызова
        # snapshot() упадёт на пустом account_id.
        executor._ensure_account()
        if not executor.client or not executor.account_id:
            with _balance_cache["lock"]:
                _balance_cache["error"] = "нет ключа Upscale или account_id не определён"
            return False
        snap = executor._snapshot()
        if snap is None:
            with _balance_cache["lock"]:
                _balance_cache["error"] = executor._snap_err or "snapshot вернул None"
            return False

        equity = snap["equity"]

        # Пик (не сбрасывается при рестарте)
        stored_peak = _load_peak()
        peak = max(float(equity), stored_peak)
        _save_peak(peak)

        with _balance_cache["lock"]:
            _balance_cache["equity"]     = equity
            _balance_cache["peak"]       = Decimal(str(peak))
            _balance_cache["day_start"]  = snap["day_start"]
            _balance_cache["base"]       = snap["base"]
            _balance_cache["day_lim"]    = snap["day_lim"]
            _balance_cache["tot_lim"]    = snap["tot_lim"]
            _balance_cache["dd_pct"]     = snap["dd_pct"]
            _balance_cache["td_pct"]     = snap["td_pct"]
            _balance_cache["updated_at"] = time.time()
            _balance_cache["error"]      = None
        return True

    except Exception as e:
        with _balance_cache["lock"]:
            _balance_cache["error"] = str(e)[:200]
        print(f"[exec_dr42] refresh_balance error: {e}")
        return False


def get_equity() -> Optional[float]:
    """Возвращает текущий equity из кеша (float) или None."""
    with _balance_cache["lock"]:
        v = _balance_cache["equity"]
    return float(v) if v is not None else None


def is_dd_brake_active() -> bool:
    """DD-brake: возвращает True если просадка от пика > $1200."""
    with _balance_cache["lock"]:
        equity = _balance_cache["equity"]
        peak   = _balance_cache["peak"]
    if equity is None or peak is None:
        return False
    return (float(peak) - float(equity)) > DD_BRAKE_THRESHOLD


def get_day_loss() -> Optional[float]:
    """
    Реализованный + нереализованный убыток СЕГОДНЯ (UTC), по данным Upscale
    (day_start_equity - текущий equity, не ниже 0). Счёт торгует только
    donchian, поэтому это ровно тот показатель, который бэктест v4.4 сравнивает
    с DAILY_STOP_LOSS (там — сумма realized_today+unrealized по своим позициям,
    здесь — то же самое число, посчитанное биржей по всему счёту).
    None, если баланс ещё не прочитан.
    """
    with _balance_cache["lock"]:
        eq = _balance_cache["equity"]
        ds = _balance_cache["day_start"]
    if eq is None or ds is None:
        return None
    return max(0.0, float(ds) - float(eq))


def compute_risk_slot(equity: float, dd_brake: bool = False) -> float:
    """
    Compound sizing:
      base = max($80, min($200, equity × 0.8%))
      DD-brake: base × 0.5
    """
    base = max(SLOT_RISK_MIN, min(SLOT_RISK_MAX, equity * RISK_FRACTION))
    if dd_brake:
        base *= DD_BRAKE_FACTOR
    return base


def balance_summary() -> str:
    """Форматированная строка текущего баланса для отчётов."""
    with _balance_cache["lock"]:
        eq      = _balance_cache["equity"]
        peak    = _balance_cache["peak"]
        ds      = _balance_cache["day_start"]
        base    = _balance_cache["base"]
        day_lim = _balance_cache["day_lim"]
        tot_lim = _balance_cache["tot_lim"]
        dd_pct  = _balance_cache["dd_pct"]
        td_pct  = _balance_cache["td_pct"]
        err     = _balance_cache["error"]
        upd     = _balance_cache["updated_at"]

    if eq is None:
        return f"⚠️ Баланс Upscale недоступен: {err or 'нет данных'}"

    age = int(time.time() - upd)
    dd_brake = (float(peak) - float(eq)) > DD_BRAKE_THRESHOLD if peak else False

    risk_slot = compute_risk_slot(float(eq), dd_brake)
    day_loss  = max(0, float(ds) - float(eq)) if ds else 0
    tot_loss  = max(0, float(base) - float(eq)) if base else 0

    day_stop_hit = day_loss >= abs(DAILY_STOP_LOSS)

    lines = [
        f"💰 <b>Upscale equity: ${float(eq):,.2f}</b> (данные {age}с назад)",
        f"Начало дня: ${float(ds):,.2f} | Убыток дня: ${day_loss:.2f} / лимит ${float(day_lim):,.0f} ({dd_pct}%)"
        + f" | дневной стоп ${abs(DAILY_STOP_LOSS):.0f}" + (" 🔴 АКТИВЕН (новых входов нет)" if day_stop_hit else ""),
        f"Старт периода: ${float(base):,.2f} | Общ. просадка: ${tot_loss:.2f} / лимит ${float(tot_lim):,.0f} ({td_pct}%)",
        f"Пик эквити: ${float(peak):,.2f} | DD от пика: ${float(peak)-float(eq):.2f}",
        f"Risk/сделку: <b>${risk_slot:.1f}</b>" + (" 🔴 DD-brake ×0.5" if dd_brake else " ✅"),
    ]
    return "\n".join(lines)


# =====================================================================
#  COOLDOWN — сохранение на диск
# =====================================================================

def _load_cooldowns() -> dict:
    try:
        if os.path.exists(COOLDOWN_FILE):
            with open(COOLDOWN_FILE) as f:
                raw = json.load(f)
            now = time.time()
            return {k: v for k, v in raw.items() if v > now}
    except Exception as e:
        print(f"[exec_dr42] cooldown load: {e}")
    return {}


def _save_cooldowns(cooldowns: dict):
    try:
        now = time.time()
        _atomic_write_json(COOLDOWN_FILE, {k: v for k, v in cooldowns.items() if v > now})
    except Exception as e:
        print(f"[exec_dr42] cooldown save: {e}")


# =====================================================================
#  АВТО-ОТСЛЕЖИВАНИЕ ЗАКРЫТИЯ ПОЗИЦИЙ (для cooldown без ручных команд)
# =====================================================================
# Старый бот определял закрытие сделки по ручным командам /out /stop.
# donchian_regime_v42 торгует полностью автоматически (EXECUTOR сам ставит
# стоп и TP), поэтому закрытие нужно обнаруживать САМИМ — сверяя список
# открытых позиций Upscale между сканами: символ, который был открыт, но
# исчез из активных позиций, значит закрылся (стопом, TP или вручную).
# PnL такой сделки берём из истории ордеров Upscale за время, что она
# была открыта (orders_history), суммируя realizedPnl.

def _load_tracked_positions() -> dict:
    try:
        if os.path.exists(OPEN_POS_FILE):
            with open(OPEN_POS_FILE) as f:
                return json.load(f)
    except Exception as e:
        print(f"[exec_dr42] tracked positions load: {e}")
    return {}


def _save_tracked_positions(d: dict):
    try:
        _atomic_write_json(OPEN_POS_FILE, d)
    except Exception as e:
        print(f"[exec_dr42] tracked positions save: {e}")


def _pnl_money(pv):
    """Парсинг realizedPnl — поле бывает либо обычным числом-строкой, либо fp9."""
    if pv is None:
        return None
    t = str(pv).strip()
    if not t or t in ("None", "null"):
        return None
    try:
        if "." in t or "e" in t.lower():
            return float(Decimal(t))
        return float(UE.from_fp9(t))
    except Exception:
        return None


def _order_ts(o: dict) -> float:
    for k in ("closedAt", "filledAt", "updatedAt", "createdAt"):
        v = o.get(k)
        if not v:
            continue
        try:
            if isinstance(v, str) and "T" in v:
                return dt.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
            return float(v) / (1000 if float(v) > 1e11 else 1)
        except Exception:
            continue
    return 0.0


def _fetch_realized_pnl_since(executor, symbol: str, since_ts: float) -> float:
    """Суммарный realizedPnl по символу с момента открытия (since_ts), по данным
    Upscale. Запас 60с на рассинхрон часов между открытием и историей ордеров."""
    try:
        m = executor._mk.get(symbol) if executor._mk else None
        if not m:
            return 0.0
        asset = str(m.get("id", "")) or symbol
        data = executor.client.orders_history(executor.account_id, asset, 100)
    except Exception as e:
        print(f"[exec_dr42] pnl history {symbol}: {e}")
        return 0.0
    total = 0.0
    for o in UE._as_list(data):
        if not isinstance(o, dict):
            continue
        ts = _order_ts(o)
        if ts and ts < since_ts - 60:
            continue
        v = _pnl_money(o.get("realizedPnl"))
        if v is not None:
            total += v
    return total


def _detect_closed_positions(executor, open_syms: set, universe: set = None):
    """Сверяет отслеживаемые donchian-позиции с реально открытыми на Upscale.
    Для каждой исчезнувшей — считает PnL и обновляет cooldown через
    on_trade_closed(). Новые позиции (открытые не через этот скан — на
    всякий случай, например после рестарта бота) начинают отслеживаться
    с этого момента, но ТОЛЬКО если символ входит в пары donchian —
    иначе чужая позиция (BTC, другая стратегия, ручная сделка) попала бы
    в cooldown-счётчик donchian по ошибке."""
    tracked = _load_tracked_positions()
    changed = False

    for sym in list(tracked.keys()):
        if sym in open_syms:
            continue
        opened_ts = tracked[sym].get("opened_ts", time.time())
        pnl = _fetch_realized_pnl_since(executor, sym, opened_ts)
        print(f"[exec_dr42] {sym}: позиция закрыта, pnl≈${pnl:.2f}")
        on_trade_closed(sym, pnl)
        del tracked[sym]
        changed = True

    for sym in open_syms:
        if sym not in tracked and (universe is None or sym in universe):
            tracked[sym] = {"opened_ts": time.time(), "side": ""}
            changed = True

    if changed:
        _save_tracked_positions(tracked)


def _track_new_position(symbol: str, side: str, entry: float = None,
                         atr: float = None, stop: float = None):
    """
    v4.4: кроме opened_ts/side, запоминаем entry/atr/stop — без них
    _manage_open_positions() не сможет вести trailing-стоп (нет базы для
    max_favorable и шага 2×ATR). entry/atr/stop берутся из СИГНАЛА в момент
    диспетчеризации (check_signals()), а не из реального исполнения — оно
    асинхронное (on_signal() асинхронный) и в этот момент ещё не завершилось,
    а небольшая разница с реальной ценой входа (проскальзывание) не критична
    для уровня, который и так движется только в выгодную сторону.
    """
    tracked = _load_tracked_positions()
    rec = {"opened_ts": time.time(), "side": side, "hold_days": 0}
    if entry is not None:
        rec["entry"] = entry
    if atr is not None:
        rec["atr"] = atr
    if stop is not None:
        rec["trail_stop"] = stop
        rec["max_favorable"] = entry if entry is not None else stop
    tracked[symbol] = rec
    _save_tracked_positions(tracked)


# =====================================================================
#  v4.4: TRAILING-СТОП / ВЫХОД ПО СИГНАЛУ / ПО ВРЕМЕНИ (замена TP1/TP2/TP3)
# =====================================================================
# Вызывается РАНЬШЕ сканирования новых входов, на каждом check_signals(),
# для всех уже открытых donchian-позиций — независимо от режима BTC и
# дневного стопа (бэктест ведёт открытые позиции каждый день безусловно,
# блокируются только НОВЫЕ входы). Закрытие по TIME/SIG исполняется здесь
# же market-ордером (EXECUTOR.close_position()); дальнейший учёт PnL и
# cooldown — как обычно, на следующем скане через _detect_closed_positions(),
# когда позиция пропадёт из списка открытых на Upscale (тот же путь, что и
# для закрытия по стопу).

def _manage_open_positions(executor, pos_by_sym: dict):
    """pos_by_sym — {symbol: raw_position_dict} ТОЛЬКО по пулу donchian
    (чужие/тестовые позиции сюда не передаём — см. вызов в check_signals())."""
    tracked = _load_tracked_positions()
    changed = False

    for sym, pos in pos_by_sym.items():
        st = tracked.get(sym)
        if not st or st.get("atr") is None or st.get("entry") is None:
            # Позиция открыта, но у нас нет её ATR/entry (например, появилась
            # не через этот модуль — после рестарта бота ДО первого скана,
            # или вручную). Управлять trailing-стопом нечем: исходный стоп,
            # поставленный со входом, остаётся в силе и никуда не делся —
            # просто не подтягивается. Это НЕ хуже старого поведения без
            # трейлинга вовсе, так что молча пропускаем.
            continue

        side = st.get("side") or UE._pos_dir(pos)
        if side not in ("long", "short"):
            continue
        s = 1 if side == "long" else -1

        try:
            cds = _fetch_candles(sym, "1d", 300)
        except Exception as e:
            print(f"[exec_dr42] trailing {sym}: свечи недоступны: {e}")
            continue
        if len(cds) < DONCHIAN_PERIOD + 2:
            continue
        last = cds[-1]

        st["hold_days"] = int(st.get("hold_days", 0)) + 1
        atr_entry = float(st["atr"])
        max_fav = float(st.get("max_favorable", st["entry"]))
        if s == 1:
            max_fav = max(max_fav, last['h'])
            new_stop = max_fav - ATR_STOP_MULT * atr_entry
        else:
            max_fav = min(max_fav, last['l'])
            new_stop = max_fav + ATR_STOP_MULT * atr_entry
        old_stop = float(st.get("trail_stop", new_stop))
        tightened = (new_stop > old_stop) if s == 1 else (new_stop < old_stop)
        trail_stop = new_stop if tightened else old_stop
        st["max_favorable"] = max_fav
        st["trail_stop"] = trail_stop
        changed = True

        info = {"id": UE._pos_id(pos), "mid": UE._pos_market(pos), "dir": side}

        # ── выход по времени ──
        if st["hold_days"] >= MAX_HOLD_DAYS:
            res = executor.close_position(info, reason=f"TIME {st['hold_days']}д")
            if B:
                B.send_telegram(f"⏱️ <b>{sym}</b>: выход по времени ({st['hold_days']} дн) — {res}")
            continue

        # ── выход по развороту сигнала (текущий Donchian(20), НЕ входной) ──
        dc = _donchian(cds, DONCHIAN_PERIOD)
        if dc is not None:
            dc_high, dc_low = dc
            sig_exit = (s == 1 and last['c'] < dc_low) or (s == -1 and last['c'] > dc_high)
            if sig_exit:
                res = executor.close_position(info, reason="SIG разворот")
                if B:
                    B.send_telegram(f"🔁 <b>{sym}</b>: выход по сигналу (разворот Donchian) — {res}")
                continue

        # ── подтяжка trailing-стопа (только если стал теснее) ──
        if tightened:
            res = executor.move_stop(info, trail_stop)
            print(f"[exec_dr42] {sym}: trailing-стоп → {trail_stop:.6g} ({res})")

    if changed:
        _save_tracked_positions(tracked)


# =====================================================================
#  ИНДИКАТОРЫ (идентичны бэктесту)
# =====================================================================

def _atr_daily(candles, period=ATR_PERIOD):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(-period, 0):
        c, prev = candles[i], candles[i - 1]
        trs.append(max(c['h'] - c['l'],
                       abs(c['h'] - prev['c']),
                       abs(c['l'] - prev['c'])))
    return sum(trs) / period


def _donchian(candles, period=DONCHIAN_PERIOD):
    if len(candles) < period + 1:
        return None
    window = candles[-(period + 1):-1]
    return max(c['h'] for c in window), min(c['l'] for c in window)


def _dmi(candles, period=DMI_PERIOD):
    if len(candles) < period * 2 + 1:
        return None, None, None
    plus_dm, minus_dm, trs = [], [], []
    for i in range(-period * 2, 0):
        c, prev = candles[i], candles[i - 1]
        up   = c['h'] - prev['h']
        down = prev['l'] - c['l']
        plus_dm.append(up if up > down and up > 0 else 0)
        minus_dm.append(down if down > up and down > 0 else 0)
        trs.append(max(c['h'] - c['l'],
                       abs(c['h'] - prev['c']),
                       abs(c['l'] - prev['c'])))
    if not trs or sum(trs[-period:]) == 0:
        return None, None, None
    atr_v   = sum(trs[-period:]) / period
    plus_d  = sum(plus_dm[-period:]) / period
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
_CANDLE_CACHE_TS = {}
CANDLE_CACHE_TTL = 3600  # 1 час


_INTERVAL_SEC = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


def _fetch_candles(symbol, interval="1d", limit=300):
    gate_c = symbol if symbol.endswith("_USDT") else f"{symbol}_USDT"
    key = (gate_c, interval, limit)
    if key in _CANDLE_CACHE and (time.time() - _CANDLE_CACHE_TS.get(key, 0)) < CANDLE_CACHE_TTL:
        return _CANDLE_CACHE[key]
    raw = B.api_get("candlesticks", {"contract": gate_c, "interval": interval, "limit": limit})
    parsed = B.parse_candles(raw)
    # Защита от форминг-свечи: если Gate.io включает в ответ ТЕКУЩУЮ, ещё не
    # закрытую свечу последним элементом (bot.py уже явно учитывает это для
    # других таймфреймов в split_closed() — значит, автор знает об этой
    # особенности API хотя бы где-то), вся стратегия сломается: бэктест
    # сравнивает цену ЗАКРЫТИЯ дня с Donchian-каналом, а тут в candles[-1]
    # окажется сегодняшняя свеча, которой 10 минут (скан идёт в 00:10 UTC,
    # сразу после открытия новой дневной свечи) — почти случайная текущая
    # цена вместо цены закрытия. Фильтр безвреден, если Gate форминг-свечу
    # не присылает: тогда последний элемент и так уже закрыт и остаётся.
    interval_sec = _INTERVAL_SEC.get(interval, 86400)
    now_ts = time.time()
    parsed = [c for c in parsed if c["t"] + interval_sec <= now_ts]
    _CANDLE_CACHE[key] = parsed
    _CANDLE_CACHE_TS[key] = time.time()
    return parsed


def _get_btc_regime() -> int:
    cds = _fetch_candles(BTC_CONTRACT, "1d", 200)
    if len(cds) < BTC_REGIME_SMA + 1:
        return 0
    closes = [c['c'] for c in cds]
    s = sum(closes[-BTC_REGIME_SMA:]) / BTC_REGIME_SMA
    return +1 if cds[-1]['c'] > s else -1


def _get_funding_snap() -> dict:
    try:
        return {
            t["contract"]: float(t.get("funding_rate", 0))
            for t in B.api_get("tickers", {})
            if t.get("contract")
        }
    except Exception as e:
        print(f"[exec_dr42] funding: {e}")
        return {}


# =====================================================================
#  ОЦЕНКА СИГНАЛА
# =====================================================================

def _evaluate(candles, btc_regime, funding_snap, symbol) -> Optional[dict]:
    if len(candles) < DONCHIAN_PERIOD + 2:
        return None
    last = candles[-1]
    dc = _donchian(candles, DONCHIAN_PERIOD)
    if dc is None:
        return None
    dc_high, dc_low = dc
    a = _atr_daily(candles[:-1])
    if a is None or a <= 0:
        return None
    atr_pct = a / last['c']
    if not (ATR_PCT_MIN <= atr_pct <= ATR_PCT_MAX):
        return None
    plus_di, minus_di, adx = _dmi(candles[:-1])
    if plus_di is None:
        return None
    fc_key = symbol if symbol.endswith("_USDT") else f"{symbol}_USDT"
    funding = funding_snap.get(fc_key, 0.0)
    long_ok  = (last['c'] > dc_high and btc_regime == +1
                and plus_di > minus_di and abs(funding) <= 0.0005)
    short_ok = (last['c'] < dc_low and btc_regime == -1
                and minus_di > plus_di and abs(funding) <= 0.0005)
    if not long_ok and not short_ok:
        return None
    side = +1 if long_ok else -1
    price = last['c']
    stop     = price - side * ATR_STOP_MULT * a
    stop_pct = abs(price - stop) / price * 100
    return {
        "symbol":    symbol,
        "side":      "long" if side == +1 else "short",
        "price":     price,
        "stop":      stop,
        "stop_pct":  stop_pct,
        "exit_mode": "trailing",  # v4.4: без TP1/TP2/TP3 — см. _manage_open_positions()
        "atr":       a,
        "atr_pct":   atr_pct,
        "dc_high":   dc_high,
        "dc_low":    dc_low,
        "plus_di":   plus_di,
        "minus_di":  minus_di,
        "adx":       adx,
        "funding":   funding,
        "ext_atr":   0.0,   # вход по закрытию свечи, не догоняем
    }


# =====================================================================
#  ГЛАВНАЯ ФУНКЦИЯ — ПРОВЕРКА СИГНАЛОВ
# =====================================================================

def check_signals():
    """
    Вызывается из bot.py один раз в сутки, ~00:10 UTC (10 мин после закрытия дневной свечи в 00:00 UTC).
    Сканирует все пары, находит сигналы, передаёт в EXECUTOR.
    """
    if B is None:
        print(f"[exec_dr42] bot.py недоступен: {_BOT_IMPORT_ERR}")
        return
    if UE is None:
        print(f"[exec_dr42] upscale_exec недоступен: {_UE_IMPORT_ERR}")
        return

    executor = getattr(B, "EXECUTOR", None)
    if executor is None:
        print("[exec_dr42] EXECUTOR не найден")
        return

    # ── 1. Пары (фильтр exclude) — нужны уже сейчас, для детекции закрытий ──
    all_pairs = list(B.UPSCALE_PAIRS)
    pairs     = [p for p in all_pairs if p not in EXCLUDE_PAIRS]

    # ── 2. Открытые позиции на Upscale (по направлениям) + авто-детекция
    # закрытых. ВАЖНО: это должно работать ВСЕГДА, независимо от баланса/
    # режима BTC — иначе cooldown «зависнет» на дни, когда вход блокируется
    # по другой причине, и статистика последовательных убытков будет неверной.
    #
    # КРИТИЧНО: если запрос позиций упал (таймаут/5xx Upscale) — это НЕ «позиций
    # нет», а «не знаем, что открыто». Раньше ошибка тихо проглатывалась, и
    # open_syms оставался пустым множеством — _detect_closed_positions() считал
    # ВСЕ отслеживаемые позиции закрытыми и мог ошибочно сбросить/исказить
    # cooldown по ним. Теперь при ошибке скан этого цикла полностью
    # прерывается — следующий (через сутки, либо при ручном перезапуске)
    # попробует снова на свежих данных.
    open_long, open_short = set(), set()
    pos_by_sym = {}
    positions_ok = False
    try:
        executor._ensure_account()
        if executor.client and executor.account_id:
            pos_list = UE._as_list(executor.client.positions(executor.account_id))
            executor._refresh_markets()
            for p in pos_list:
                if not isinstance(p, dict):
                    continue
                mid = UE._pos_market(p)
                sym = None
                if executor._mk:
                    for s, m in executor._mk.items():
                        if str(m.get("id", "")) == mid or s == mid:
                            sym = s
                            break
                if sym is None:
                    continue
                pos_by_sym[sym] = p
                if UE._pos_dir(p) == "short":
                    open_short.add(sym)
                else:
                    open_long.add(sym)
            positions_ok = True
        else:
            # нет ключа/account_id — это конфигурационная проблема, не сетевая;
            # считаем как "неизвестно", та же защита ниже сработает корректно.
            pass
    except Exception as e:
        print(f"[exec_dr42] позиции: {e}")

    if not positions_ok:
        B.send_telegram(
            "⛔ donchian_regime_v42: не удалось получить список открытых позиций "
            "Upscale — пропускаю весь скан (не трогаю cooldown и не открываю новых "
            "входов, чтобы не исказить учёт закрытых сделок на неполных данных)."
        )
        return

    # Для подсчёта лимитов «в сторону» считаем только позиции из пула donchian —
    # зависшая тестовая BTC-позиция (/uptest) или позиция по исключённой паре не
    # должны отъедать слот у основной стратегии.
    pairs_set = set(pairs)
    open_long_dr42  = {s for s in open_long  if s in pairs_set}
    open_short_dr42 = {s for s in open_short if s in pairs_set}

    open_syms = open_long | open_short

    try:
        _detect_closed_positions(executor, open_syms, universe=pairs_set)
    except Exception as e:
        print(f"[exec_dr42] detect_closed_positions: {e}")

    # ── 2б. v4.4: ведём уже открытые позиции — trailing-стоп / SIG / TIME ──
    # ДО проверки баланса/режима BTC и НЕЗАВИСИМО от них: открытые сделки
    # нужно вести каждый день, даже если сегодня новых входов не будет
    # (нейтральный BTC, дневной стоп, лимит сторон) — ровно как в бэктесте,
    # где секция закрытия позиций безусловна, а проверки ниже решают только
    # про НОВЫЕ входы.
    try:
        pos_by_sym_dr42 = {s: p for s, p in pos_by_sym.items() if s in pairs_set}
        _manage_open_positions(executor, pos_by_sym_dr42)
    except Exception as e:
        print(f"[exec_dr42] manage_open_positions: {e}")

    # ── 3. Баланс (нужен для compound sizing, динамического лимита сторон
    # и дневного стопа -$400) ─────────────────────────────────────────────
    bal_ok = refresh_balance(executor, force=True)
    if not bal_ok:
        msg = f"⛔ donchian_regime_v42: не удалось получить баланс Upscale — входы отменены.\n{_balance_cache.get('error', '')}"
        B.send_telegram(msg)
        return

    equity   = get_equity()
    dd_brake = is_dd_brake_active()
    risk_usd = compute_risk_slot(equity, dd_brake)
    max_same_side = compute_max_same_side(risk_usd)   # напр. $80 → 3
    max_total     = min(MAX_CONCURRENT_CEILING, max_same_side * 2)

    # ── 3б. v4.4: дневной стоп -$400 — блокирует только НОВЫЕ входы, уже
    # открытые позиции продолжаем вести (см. 2б выше, которая уже отработала).
    day_loss = get_day_loss()
    if day_loss is not None and day_loss >= abs(DAILY_STOP_LOSS):
        B.send_telegram(
            f"🔴 donchian_regime_v42: дневной убыток ${day_loss:.0f} ≥ стопа "
            f"${abs(DAILY_STOP_LOSS):.0f} — новые входы заблокированы до завтра (UTC)."
        )
        return

    # ── 4. BTC режим ────────────────────────────────────────────────────
    btc_regime = _get_btc_regime()
    if btc_regime == 0:
        B.send_telegram("⚪ donchian_regime_v42: BTC нейтраль — входов нет.")
        return
    regime_str = "🐂 BULL" if btc_regime == +1 else "🐻 BEAR"

    # ── 5. Funding snapshot ──────────────────────────────────────────────
    funding_snap = _get_funding_snap()

    # Сторона, в которую сегодня можем входить (фильтр по режиму BTC ниже
    # пропускает только эту сторону) — и сколько слотов на ней свободно.
    # Считаем ТОЛЬКО позиции из пула donchian (open_*_dr42) — чужая/тестовая
    # позиция (например, зависшая после /uptest BTC-позиция, или позиция по
    # исключённой паре) не должна отъедать слот у основной стратегии.
    side_today   = "long" if btc_regime == +1 else "short"
    open_on_side = open_long_dr42 if side_today == "long" else open_short_dr42
    open_dr42    = open_long_dr42 | open_short_dr42
    slots_side   = max(0, max_same_side - len(open_on_side))

    if len(open_dr42) >= max_total:
        B.send_telegram(f"⛔ donchian_regime_v42: уже {len(open_dr42)} позиций "
                        f"(лимит {max_total} = {max_same_side}×2 сторонам).")
        return
    if slots_side <= 0:
        B.send_telegram(f"⛔ donchian_regime_v42: уже {len(open_on_side)} {side_today} позиций "
                        f"(лимит {max_same_side} в эту сторону при риске ${risk_usd:.0f}).")
        return

    # ── 6. Cooldown ──────────────────────────────────────────────────────
    cooldowns = _load_cooldowns()
    now_ts    = time.time()

    # ── 7. Сканирование ──────────────────────────────────────────────────
    candidates = []
    errors     = []

    for sym in pairs:
        if sym in cooldowns and cooldowns[sym] > now_ts:
            continue
        if sym in open_syms:
            continue
        try:
            cds = _fetch_candles(sym, "1d", 300)
            sig = _evaluate(cds, btc_regime, funding_snap, sym)
            if sig is None:
                continue
            if btc_regime == +1 and sig["side"] != "long":
                continue
            if btc_regime == -1 and sig["side"] != "short":
                continue
            sig["_di_gap"] = abs(sig["plus_di"] - sig["minus_di"])
            candidates.append(sig)
        except Exception as e:
            errors.append(f"{sym}: {e}")

    if errors:
        B.send_telegram("⚠️ drv42 ошибки " + str(len(errors)) + " пар: "
                        + ", ".join(errors[:5]) + ("..." if len(errors) > 5 else ""))

    # ── 8. Сортировка и лимит ────────────────────────────────────────────
    # Три независимых потолка одновременно: слоты этой стороны (slots_side),
    # общий потолок обеих сторон (max_total - открыто всего), и сколько
    # новых входов в сутки разрешено вообще (MAX_NEW_PER_DAY).
    candidates.sort(key=lambda x: x["_di_gap"], reverse=True)
    slots_free = min(slots_side, max_total - len(open_dr42), MAX_NEW_PER_DAY)
    to_trade   = candidates[:max(0, slots_free)]

    # ── 9. Отчёт в Telegram (баланс/лимиты уже разложены в balance_summary) ──
    stray = len(open_syms) - len(open_dr42)   # позиции НЕ из пула donchian (тест/исключённые)
    B.send_telegram(
        f"🔍 <b>donchian_regime_v42 v4.4</b> | {regime_str}\n"
        + balance_summary() + "\n"
        + f"Risk/сделку: <b>${risk_usd:.1f}</b>" + (" 🔴 DD-brake" if dd_brake else "") + "\n"
        + f"Лимит в сторону: {max_same_side} (итого {max_total}) | "
        + f"Открыто: {len(open_long_dr42)} long / {len(open_short_dr42)} short"
        + (f" (+{stray} чужих)" if stray else "") + "\n"
        + f"Пар: {len(pairs)} (excl: {len(all_pairs)-len(pairs)}) | "
        + f"Cooldown: {len(cooldowns)} | "
        + f"Кандидаты: {len(candidates)} | Входим: {len(to_trade)}"
    )

    if not to_trade:
        B.send_telegram("donchian_regime_v42: сигналов нет.")
        return

    # ── 10. Передача сигналов в executor ─────────────────────────────────
    # ВАЖНО: EXECUTOR общий для всех стратегий бота (УКЛОН использует его же
    # с фиксированным RISK_USD из bot.py). Donchian считает риск по-своему
    # (compound sizing от equity) — риск/лимит позиции передаются ПРЯМО В
    # СИГНАЛЕ (поля risk_usd/max_pos_usd), upscale_exec._handle() их уже умеет
    # читать оттуда с приоритетом над self.risk_usd. Так нет гонки между
    # потоками разных стратегий и не нужно мутировать общий executor.
    max_pos_usd_dr42 = min(float(equity) * MAX_POSITION_PCT_OF_EQUITY, 3000.0)

    for sig in to_trade:
        sym      = sig["symbol"]
        side_str = "🟢 LONG" if sig["side"] == "long" else "🔴 SHORT"
        B.send_telegram(
            f"🎯 <b>СИГНАЛ {sym} {side_str}</b>\n"
            f"Цена: {sig['price']:.6g} | Стоп: {sig['stop']:.6g} ({sig['stop_pct']:.2f}%)\n"
            f"Выход: trailing-стоп 2×ATR / разворот Donchian / {MAX_HOLD_DAYS}д (без TP)\n"
            f"ATR: {sig['atr']:.4g} ({sig['atr_pct']*100:.2f}%)\n"
            f"+DI: {sig['plus_di']:.1f} / -DI: {sig['minus_di']:.1f} | "
            f"Funding: {sig['funding']*100:.4f}% | Risk: ${risk_usd:.1f}"
        )
        sig["risk_usd"]       = risk_usd
        sig["max_pos_usd"]    = max_pos_usd_dr42
        sig["max_same_side"]  = max_same_side   # unified с проверкой в upscale_exec._execute()
        score = int(sig["_di_gap"])
        executor.on_signal(sig, score)
        # Начинаем отслеживать сразу — on_signal() асинхронный (отдельный поток),
        # но даже если ордер в итоге не пройдёт (риск-guard, монеты нет на Upscale
        # и т.п.), следующий check_signals() увидит, что позиции нет, снимет
        # символ с отслеживания без изменения cooldown (pnl будет 0). entry/atr/
        # stop передаём сразу — без них _manage_open_positions() не сможет вести
        # trailing-стоп с завтрашнего скана.
        _track_new_position(sym, sig["side"], entry=sig["price"], atr=sig["atr"], stop=sig["stop"])


# =====================================================================
#  ОБНОВЛЕНИЕ COOLDOWN (вызывается из bot.py при закрытии сделки)
# =====================================================================

def on_trade_closed(symbol: str, pnl: float):
    """
    Вызывается из bot.py при /out или /stop.
    symbol — bare символ (как в UPSCALE_PAIRS), pnl — реализованный USD.
    """
    try:
        if os.path.exists(TRADE_STATE_FILE):
            with open(TRADE_STATE_FILE) as f:
                state = json.load(f)
        else:
            state = {}
    except Exception:
        state = {}

    sym_state = state.get(symbol, {"consec_losses": 0})

    if pnl < 0:
        sym_state["consec_losses"] = sym_state.get("consec_losses", 0) + 1
        if sym_state["consec_losses"] >= CONSEC_LOSS_LIMIT:
            cooldowns = _load_cooldowns()
            until     = time.time() + COOLDOWN_DAYS * 86400
            cooldowns[symbol] = until
            _save_cooldowns(cooldowns)
            until_str = dt.datetime.fromtimestamp(until, dt.timezone.utc).strftime("%Y-%m-%d")
            if B:
                B.send_telegram(
                    f"🚫 <b>COOLDOWN {symbol}</b>: {CONSEC_LOSS_LIMIT} убытка подряд → "
                    f"блок до {until_str} ({COOLDOWN_DAYS} дн)"
                )
            sym_state["consec_losses"] = 0
    else:
        sym_state["consec_losses"] = 0

    state[symbol] = sym_state
    try:
        _atomic_write_json(TRADE_STATE_FILE, state)
    except Exception as e:
        print(f"[exec_dr42] state save: {e}")

    # Обновляем кеш баланса после закрытия (реализован PnL)
    refresh_balance(force=False)


# =====================================================================
#  СТАТУС ДЛЯ /up
# =====================================================================

def status_report() -> str:
    """Статус стратегии — добавляется в /up."""
    cooldowns = _load_cooldowns()
    now_ts    = time.time()
    active_cd = {k: v for k, v in cooldowns.items() if v > now_ts}

    lines = ["", "━━━ donchian_regime_v42 v4.4 ━━━"]

    # Баланс
    lines.append(balance_summary())

    # BTC режим
    if B:
        try:
            r = _get_btc_regime()
            regime_str = "🐂 BULL" if r == +1 else ("🐻 BEAR" if r == -1 else "⚪ НЕЙТРАЛЬ")
            lines.append(f"BTC режим: {regime_str}")
        except Exception as e:
            lines.append(f"BTC режим: ⚠️ {e}")

    lines.append(f"Exclude: {sorted(EXCLUDE_PAIRS)}")
    lines.append(f"Выход: trailing-стоп {ATR_STOP_MULT}×ATR / разворот Donchian / {MAX_HOLD_DAYS}д (без TP)")

    eq = get_equity()
    if eq is not None:
        dd_brake = is_dd_brake_active()
        risk_now = compute_risk_slot(eq, dd_brake)
        same_now = compute_max_same_side(risk_now)
        day_loss = get_day_loss() or 0.0
        lines.append(f"Риск сейчас: ${risk_now:.1f} → лимит {same_now} в сторону "
                     f"(итого {min(MAX_CONCURRENT_CEILING, same_now*2)}) | New/day: {MAX_NEW_PER_DAY}")
        lines.append(f"Дневной стоп: ${day_loss:.0f} / ${abs(DAILY_STOP_LOSS):.0f}"
                     + (" 🔴 АКТИВЕН" if day_loss >= abs(DAILY_STOP_LOSS) else ""))
    else:
        lines.append(f"Лимит в сторону: до {MAX_SAME_SIDE_CEILING} (риск-зависимо) | New/day: {MAX_NEW_PER_DAY}")
    lines.append(f"Cooldown активных: {len(active_cd)}")

    if active_cd:
        for sym, until in sorted(active_cd.items()):
            until_str = dt.datetime.fromtimestamp(until, dt.timezone.utc).strftime("%Y-%m-%d")
            lines.append(f"  🚫 {sym} до {until_str}")

    return "\n".join(lines)


# =====================================================================
#  ФОНОВОЕ ОБНОВЛЕНИЕ БАЛАНСА
# =====================================================================

def start_balance_updater():
    """
    Запускает фоновый поток обновления баланса каждые BALANCE_TTL_SEC секунд.
    Вызвать один раз при старте бота (после создания EXECUTOR).
    """
    def _loop():
        while True:
            time.sleep(BALANCE_TTL_SEC)
            try:
                ok = refresh_balance(force=True)
                if not ok and B:
                    print(f"[exec_dr42] balance update failed: {_balance_cache.get('error')}")
            except Exception as e:
                print(f"[exec_dr42] balance updater: {e}")

    t = threading.Thread(target=_loop, daemon=True, name="dr42-balance")
    t.start()
    print("[exec_dr42] фоновый обновитель баланса запущен (каждые"
          f" {BALANCE_TTL_SEC}с = {BALANCE_TTL_SEC//60} мин)")


# =====================================================================
#  ТОЧКА ВХОДА
# =====================================================================

if __name__ == "__main__":
    print("exec_donchian_regime_v42.py — модуль исполнения стратегии Donchian Regime v4.4")
    print()
    print("Функции для bot.py:")
    print("  check_signals()           — сканировать пары и передать сигналы в EXECUTOR")
    print("  on_trade_closed(sym, pnl) — обновить cooldown при закрытии сделки")
    print("  status_report()           — статус стратегии для /up")
    print("  start_balance_updater()   — запустить фоновый поток обновления баланса")
    print("  balance_summary()         — строка с текущим балансом и риском")
    print("  refresh_balance(force=T)  — принудительно обновить баланс из API")
    print("  get_equity()              — текущий equity float или None")
    print("  is_dd_brake_active()      — True если DD > $1200")
    print("  compute_risk_slot(eq, dd) — risk $ для одной сделки")
    print("  get_day_loss()            — убыток дня (UTC) float или None")
