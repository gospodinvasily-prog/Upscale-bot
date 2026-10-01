"""
Слой исполнения для Upscale API (v1: dry + отправка ордеров ТОЛЬКО на демо-счёт).

Умеет: читать счета/рынки/позиции; считать план сделки; в режиме dry писать «что бы открыл»;
в режиме demo — открывать market со стопом сразу, ставить TP1/TP2 отдельными take-ордерами
через TP_DELAY_SEC (правило 60с); самопроверка /uptest; kill-switch (/halt) и /closeall.
Жёсткая защита: ордера уходят только если тип счёта == demo.
Сигналы в телеграм от этого модуля не зависят: любая ошибка здесь гасится.

Переменные окружения:
  AUTO_TRADE          off | dry | demo  (demo = реальные ордера, но только на демо-счёте)
  UPSCALE_API_KEY     ключ (только в Render, в чат не присылать)
  UPSCALE_AUTH_SCHEME "" (ключ как есть) или "Bearer"
  UPSCALE_ACCOUNT_ID  id демо-счёта (если пусто — берём единственный из списка)
  EXEC_LEVERAGE       плечо для расчёта маржи (по умолчанию 5)
  EXEC_MAX_CHASE_ATR  не входить, если цена ушла от уровня дальше N ATR (0.3)
  EXEC_TP_DELAY_SEC   когда можно ставить TP после входа (65, правило 60с)
  EXEC_MAX_OPEN       максимум одновременных позиций (3)
  EXEC_MAX_TRADES_DAY максимум входов за сутки UTC (8)
  EXEC_BREAKEVEN      on|off — перевод стопа в безубыток после TP1 (по умолчанию on)
  EXEC_DAY_SOFT_FRAC / EXEC_DAY_HARD_FRAC   доля дневного лимита: стоп входов (0.6) / аварийное закрытие (0.8)
  EXEC_TOT_SOFT_FRAC / EXEC_TOT_HARD_FRAC   то же для максимальной просадки (0.6 / 0.7)
  EXEC_DAY_GAIN_CAP_PCT  потолок дневного плюса в % (правило 30%), 0 = выключено
"""
import os
import re
import time
import threading
import traceback
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, InvalidOperation

import requests

EXEC_VERSION  = "1.9"   # смотри в /up и /uptest: так видно, какой файл реально запущен
BASE_URL      = os.environ.get("UPSCALE_API_URL", "https://api.upscale.trade")
FP            = Decimal(10) ** 9
LEVERAGE      = Decimal(os.environ.get("EXEC_LEVERAGE", "5"))
MARGIN_BUFFER = Decimal(os.environ.get("EXEC_MARGIN_BUFFER", "0.005"))  # запас (замер: amount $100 ×5 → notional $499.80, маржа $99.96)
MAX_CHASE_ATR = float(os.environ.get("EXEC_MAX_CHASE_ATR", "0.3"))
TP_DELAY_SEC  = int(os.environ.get("EXEC_TP_DELAY_SEC", "65"))
MAX_OPEN      = int(os.environ.get("EXEC_MAX_OPEN", "0"))       # 0 = без лимита, ориентир только на риск
MAX_TRADES_DAY = int(os.environ.get("EXEC_MAX_TRADES_DAY", "0"))  # 0 = без лимита, ориентир только на риск
# Защита по просадке — доли от лимитов счёта (при 5%/10%: стоп входов 3%/6%, аварийное закрытие 4%/7%)
DAY_SOFT_FRAC = Decimal(os.environ.get("EXEC_DAY_SOFT_FRAC", "0.6"))
DAY_HARD_FRAC = Decimal(os.environ.get("EXEC_DAY_HARD_FRAC", "0.8"))
TOT_SOFT_FRAC = Decimal(os.environ.get("EXEC_TOT_SOFT_FRAC", "0.6"))
TOT_HARD_FRAC = Decimal(os.environ.get("EXEC_TOT_HARD_FRAC", "0.7"))
DAY_GAIN_CAP_PCT = Decimal(os.environ.get("EXEC_DAY_GAIN_CAP_PCT", "0"))   # >0: не входить, если плюс за день ≥ N% (правило 30%); 0 = выкл
WATCHDOG_SEC  = int(os.environ.get("EXEC_WATCHDOG_SEC", "60"))
# v1.6: перевод стопа в безубыток после срабатывания TP1.
# Отменить исходный stopTriggerPrice (выставленный вместе со входом) API не позволяет,
# поэтому безубыток ставится ДОПОЛНИТЕЛЬНЫМ stop-ордером. Он всегда ближе к цене, чем
# исходный, поэтому срабатывает первым и закрывает позицию — исходный остаётся не у дел.
BREAKEVEN_ENABLED = os.environ.get("EXEC_BREAKEVEN", "on").strip().lower() not in ("off", "0", "no")
BREAKEVEN_OFFSET  = Decimal(os.environ.get("EXEC_BREAKEVEN_OFFSET", "0.0005"))  # +0.05% от входа: покрыть комиссию
BREAKEVEN_POLL    = int(os.environ.get("EXEC_BREAKEVEN_POLL", "20"))            # как часто смотреть, сработал ли TP1
BREAKEVEN_MAX_MIN = int(os.environ.get("EXEC_BREAKEVEN_MAX_MIN", "720"))        # сколько всего следить, мин
MARKETS_TTL   = 30 * 60


# ── fp9: числа как строки ×10⁹, только Decimal ───────────────────────────────
def to_fp9(x) -> str:
    d = Decimal(str(x))
    return str(int((d * FP).to_integral_value(rounding=ROUND_DOWN)))

def from_fp9(s) -> Decimal:
    try:
        return Decimal(str(s)) / FP
    except InvalidOperation:
        return Decimal(0)


class UpscaleError(Exception):
    pass


class UpscaleClient:
    """Только чтение. Названия полей в ответах заранее неизвестны — возвращаем как есть."""

    def __init__(self, key: str, scheme: str = "", base: str = BASE_URL, min_gap: float = 0.5):
        self.key, self.scheme, self.base, self.min_gap = key, scheme, base.rstrip("/"), min_gap
        self._last = 0.0
        self._lock = threading.Lock()

    def _get(self, path: str, params: dict = None):
        with self._lock:                     # свой троттлинг: лимиты запросов пока неизвестны
            wait = self.min_gap - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
        auth = f"{self.scheme} {self.key}".strip()
        r = requests.get(self.base + path, params=params, timeout=10,
                         headers={"Authorization": auth, "Accept": "application/json"})
        if r.status_code == 429:
            raise UpscaleError("429: лимит запросов")
        if r.status_code != 200:
            raise UpscaleError(f"{r.status_code} на {path}: {r.text[:200]}")
        return r.json()

    def _post(self, path: str, body: dict):
        with self._lock:
            wait = self.min_gap - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
        auth = f"{self.scheme} {self.key}".strip()
        r = requests.post(self.base + path, json=body, timeout=15,
                          headers={"Authorization": auth, "Accept": "application/json",
                                   "Content-Type": "application/json",
                                   "x-idempotency-key": str(uuid.uuid4())})
        if r.status_code == 429:
            raise UpscaleError("429: лимит запросов")
        if not (200 <= r.status_code < 300):
            raise UpscaleError(f"{r.status_code} на {path}: {r.text[:300]}")
        try:
            return r.json()
        except Exception:
            return {}

    def order(self, body: dict):   return self._post("/orders", body)
    def close_all(self, acc_id):   return self._post(f"/positions/{acc_id}/close-all", {})

    def accounts(self):            return self._get("/accounts/with-risk-status")
    def markets(self, acc_id):     return self._get("/v2/markets", {"accountId": acc_id})
    def positions(self, acc_id):   return self._get(f"/positions/{acc_id}/active")
    def risk_status(self, acc_id): return self._get(f"/accounts/{acc_id}/risk-status")


# ── разбор ответов (формат неизвестен → защитно) ─────────────────────────────
def _as_list(data, _depth=0):
    """Достаёт список объектов из ответа любой формы: список, {data:[...]}, {data:{markets:[...]}}, {SYM:{...}}."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and _depth < 3:
        for k in ("data", "items", "results", "accounts", "markets", "positions", "rows", "list"):
            if k in data:
                got = _as_list(data[k], _depth + 1)
                if got:
                    return got
        vals = list(data.values())
        if vals and all(isinstance(v, dict) for v in vals):      # словарь вида {"BTC": {...}, ...}
            return [dict(v, _key=k) for k, v in data.items()]
        for v in vals:                                            # единственный вложенный список
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    return []

def market_price(m: dict):
    """Текущая цена рынка по данным Upscale. Бот считает уровни по свечам Gate,
    а торгует здесь — расхождение площадок выглядело как проскальзывание 0.2-0.8%.
    Берём цену оттуда, где реально исполняемся."""
    if not isinstance(m, dict):
        return None
    for src in (m.get("state"), m):
        if not isinstance(src, dict):
            continue
        for k in ("price", "markPrice", "lastPrice", "indexPrice", "oraclePrice", "mark"):
            v = src.get(k)
            if v is None:
                continue
            try:
                p = from_fp9(v) if str(v).isdigit() and len(str(v)) > 9 else Decimal(str(v))
                if p > 0:
                    return p
            except Exception:
                continue
    return None


def _norm_sym(s: str) -> str:
    s = re.sub(r"[^A-Z0-9]", "", str(s).upper())
    for suf in ("PERP", "USDT", "USDC", "USD"):
        if s.endswith(suf) and len(s) > len(suf):
            s = s[: -len(suf)]
    return s

def _strings(d, depth=0):
    """Все короткие строки объекта (в т.ч. из вложенных словарей) — кандидаты в тикеры."""
    if isinstance(d, str):
        if 1 <= len(d) <= 24 and re.search(r"[A-Za-z]", d) and not re.fullmatch(r"[0-9a-fA-F-]{20,}", d):
            yield d
    elif isinstance(d, dict) and depth < 2:
        for k, v in d.items():
            if k in ("id", "accountId", "userId"):
                continue
            yield from _strings(v, depth + 1)

def market_index(markets_raw) -> dict:
    """{'SEI': market_dict, ...}. Основной путь — config.baseAsset (формат Upscale);
    запасной (любые поля) — только для рынков без config."""
    idx = {}
    for m in _as_list(markets_raw):
        if not isinstance(m, dict):
            continue
        cfg = m.get("config")
        if isinstance(cfg, dict) and isinstance(cfg.get("baseAsset"), str):
            idx.setdefault(_norm_sym(cfg["baseAsset"]), m)
            continue
        for f in ("symbol", "name", "ticker", "baseSymbol", "baseAsset", "base", "asset", "pair", "_key"):
            if isinstance(m.get(f), str):
                idx.setdefault(_norm_sym(m[f]), m)
        for st in _strings(m):
            idx.setdefault(_norm_sym(st), m)
    return idx

def find_market(idx: dict, sym: str):
    """Тикер бота -> рынок Upscale. У мемов на Upscale приставка 1000 (PEPE -> 1000PEPE)."""
    k = _norm_sym(sym)
    return idx.get(k) or idx.get("1000" + k)

def describe(raw, n=350) -> str:
    """Как выглядит ответ — для отладки формата в /up."""
    if isinstance(raw, list):
        head = f"список из {len(raw)}"
        first = raw[0] if raw else None
    elif isinstance(raw, dict):
        head = "объект, ключи: " + ", ".join(list(raw.keys())[:12])
        first = next((v for v in raw.values() if isinstance(v, (list, dict)) and v), None)
    else:
        return f"{type(raw).__name__}: {str(raw)[:n]}"
    return head + (f"\nпервый элемент: {str(first)[:n]}" if first is not None else "")

def pick(d: dict, *names):
    for n in names:
        if isinstance(d, dict) and d.get(n) is not None:
            return d[n]
    return None


# ── расчёт плана сделки (чистая функция) ─────────────────────────────────────
def build_plan(price, stop_pct, side, stop, tp1, tp2, risk_usd, max_pos_usd,
               leverage=LEVERAGE, margin_buffer=MARGIN_BUFFER) -> dict:
    price, stop_pct = Decimal(str(price)), Decimal(str(stop_pct))
    pos = min(Decimal(str(risk_usd)) / (stop_pct / 100), Decimal(str(max_pos_usd)))
    margin = pos / leverage * (1 + margin_buffer)
    return {
        "pos_usd": pos,
        "size_base": pos / price,
        "margin_usd": margin,
        "real_risk_usd": pos * stop_pct / 100,
        "leverage": leverage,
        "amount_fp9": to_fp9(margin),
        "stop_fp9": to_fp9(stop),
        "tp1_fp9": to_fp9(tp1),
        "tp2_fp9": to_fp9(tp2),
        "tp_delay_sec": TP_DELAY_SEC,
    }


def chase_skip(ext_atr, limit=MAX_CHASE_ATR):
    """Цена уже ушла от уровня дальше limit ATR — не догоняем."""
    if ext_atr is None:
        return None
    if ext_atr > limit:
        return f"цена ушла от уровня на {ext_atr:.2f} ATR (> {limit})"
    return None


# ── тела ордеров (чистые функции) ────────────────────────────────────────────
def open_body(account_id, market_id, direction, margin_usd, leverage, stop=None, take=None) -> dict:
    """market-вход. amount = сумма резерва (маржа) в котируемой валюте, стоп сразу со входом."""
    body = {"accountId": account_id, "marketId": market_id, "type": "market",
            "direction": direction, "amount": to_fp9(margin_usd), "leverage": to_fp9(leverage)}
    if stop is not None:
        body["stopTriggerPrice"] = to_fp9(stop)
    if take is not None:
        body["takeTriggerPrice"] = to_fp9(take)
    return body

def _check_amount(amount_fp9):
    """v1.8: не отправляем ордер с нулевым/отрицательным объёмом — биржа вернёт
    amount_not_positive, а позиция останется без цели."""
    n = int(amount_fp9)
    if n <= 0:
        raise UpscaleError(f"объём ордера не положительный ({n}) — ордер не отправлен")
    return str(n)


def take_body(account_id, market_id, direction, position_id, amount_fp9, trigger_price) -> dict:
    """take-ордер по позиции: amount = размер в базовом активе (fp9), trigger 0 = по рынку."""
    return {"accountId": account_id, "marketId": market_id, "type": "take", "direction": direction,
            "positionId": position_id, "amount": _check_amount(amount_fp9),
            "triggerPrice": to_fp9(trigger_price)}

def stop_body(account_id, market_id, direction, position_id, amount_fp9, trigger_price) -> dict:
    """stop-ордер по открытой позиции (безубыток). Формат тот же, что у take, отличается type."""
    return {"accountId": account_id, "marketId": market_id, "type": "stop", "direction": direction,
            "positionId": position_id, "amount": _check_amount(amount_fp9),
            "triggerPrice": to_fp9(trigger_price)}

def breakeven_price(entry, side, offset=None) -> Decimal:
    """Цена безубытка: вход плюс небольшой отступ в сторону прибыли, чтобы покрыть комиссию."""
    off = BREAKEVEN_OFFSET if offset is None else Decimal(str(offset))
    e = Decimal(str(entry))
    return e * (1 + off) if side == "long" else e * (1 - off)

def _pos_id(p):     return str(pick(p, "idx", "positionId", "id", "txId") or "")

def _pos_market(p):
    for k in ("marketId", "market"):
        v = p.get(k)
        if isinstance(v, dict):
            v = v.get("id")
        if v:
            return str(v)
    return ""

def _pos_dir(p):    return str(pick(p, "direction", "side") or "").lower()

def _pos_size(p):
    """Размер позиции в fp9, всегда ПОЛОЖИТЕЛЬНЫЙ (направление берём из direction).
    v1.8: было `int(v) if v.isdigit()` — у шорта размер приходит как "-6021973",
    а isdigit() для минуса даёт False, поэтому число повторно умножалось на 10⁹.
    Отсюда риск в миллиардах и «Order must have positive amount» на тейках."""
    v = pick(p, "size", "amount", "quantity", "baseAmount", "qty", "volume")
    if v is None:
        return 0
    v = str(v).strip()
    try:
        n = int(v)                      # целое (в т.ч. отрицательное) — уже fp9
    except ValueError:
        try:
            n = int(to_fp9(v))          # дробное — переводим в fp9
        except Exception:
            return 0
    return abs(n)

def _opp(d): return "short" if d == "long" else "long"

def _trunc(x, n=400):
    return str(x)[:n]


# ── исполнитель ──────────────────────────────────────────────────────────────
class Executor:
    def __init__(self, send_fn, csv_fn, risk_usd, max_pos_usd, pairs=None):
        self.send, self.csv = send_fn, csv_fn
        self.pairs = list(pairs or [])
        self.risk_usd, self.max_pos_usd = risk_usd, max_pos_usd
        req = os.environ.get("AUTO_TRADE", "off").strip().lower()
        self.mode = req if req in ("off", "dry", "demo") else "off"
        self.halted = False
        key = os.environ.get("UPSCALE_API_KEY", "").strip()
        self.client = UpscaleClient(key, os.environ.get("UPSCALE_AUTH_SCHEME", "")) if key else None
        self.account_id = os.environ.get("UPSCALE_ACCOUNT_ID", "").strip()
        self.account_type = ""
        self.close_dir = "same"          # направление take-ордера при закрытии: как у позиции или встречное
        self.acc = {}                    # карточка счёта (лимиты просадки)
        self._open_risk = {}             # id позиции -> риск по стопу, $
        self._trip = None                # {"kind": "day"|"total", "date": ...} — сработала аварийная защита
        self._soft_date = ""
        self._snap_err = ""
        self._mk, self._mk_ts = {}, 0.0
        self._day = {"date": "", "n": 0}
        self._lock = threading.Lock()
        if self.mode == "demo" and self.client:
            threading.Thread(target=self._watchdog, daemon=True).start()

    # -- служебное --
    def _refresh_markets(self):
        if not self.client or not self.account_id:
            return
        if time.time() - self._mk_ts < MARKETS_TTL and self._mk:
            return
        self._mk = market_index(self.client.markets(self.account_id))
        self._mk_ts = time.time()

    def _ensure_account(self):
        if not self.client:
            return
        if self.account_id and self.account_type and self.acc:
            return
        accs = [a for a in _as_list(self.client.accounts()) if isinstance(a, dict)]
        if not self.account_id and len(accs) == 1:
            self.account_id = str(pick(accs[0], "accountId", "id") or "")
        for a in accs:
            if str(pick(a, "accountId", "id")) == self.account_id:
                self.account_type = str(a.get("type") or "").lower()
                self.acc = a

    def _demo_block(self):
        """Ручные команды (/uptest, /closeall): нужен только ключ и ДЕМО-счёт, режим AUTO_TRADE не важен."""
        if not self.client or not self.account_id:
            return "нет ключа или счёта"
        if self.account_type != "demo":
            return f"счёт не демо (type={self.account_type or '?'}) — ордера запрещены"
        return None

    def _real_block(self):
        """Причина, по которой АВТО-ордера слать нельзя (None — можно): режим demo + демо-счёт."""
        if self.mode != "demo":
            return "режим не demo"
        return self._demo_block()

    def _day_count(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._day["date"] != today:
            self._day = {"date": today, "n": 0}
        return self._day["n"]

    # -- контроль баланса и просадки --
    def _snapshot(self):
        """Эквити и лимиты из risk-status. None, если не удалось прочитать (тогда входы блокируются)."""
        try:
            rs = self.client.risk_status(self.account_id)
        except Exception as e:
            self._snap_err = _trunc(e, 200)
            return None
        if not isinstance(rs, dict) or pick(rs, "currentEquity", "equity") is None or pick(rs, "dayStartEquity") is None:
            self._snap_err = "неожиданный формат risk-status: " + _trunc(rs, 200)
            return None
        equity = from_fp9(pick(rs, "currentEquity", "equity"))
        day_start = from_fp9(rs["dayStartEquity"])
        init = self.acc.get("initialAccountBalance") or pick(rs, "periodStartEquity")
        base = from_fp9(init) if init else day_start
        dd = Decimal(str(self.acc.get("maxDailyDrawdown") or 5))
        td = Decimal(str(self.acc.get("maxTotalDrawdown") or 10))
        day_lim, tot_lim = day_start * dd / 100, base * td / 100
        return {"equity": equity, "day_start": day_start, "base": base, "rs": rs,
                "dd_pct": dd, "td_pct": td,
                "day_loss": max(Decimal(0), day_start - equity), "tot_loss": max(Decimal(0), base - equity),
                "day_gain": max(Decimal(0), equity - day_start),
                "day_lim": day_lim, "tot_lim": tot_lim,
                "day_soft": day_lim * DAY_SOFT_FRAC, "day_hard": day_lim * DAY_HARD_FRAC,
                "tot_soft": tot_lim * TOT_SOFT_FRAC, "tot_hard": tot_lim * TOT_HARD_FRAC}

    def _guard_skip(self, snap, new_risk: Decimal):
        """Причина отказа от входа по риску или None. Открытый риск считаем по стопам открытых позиций."""
        open_risk = sum(self._open_risk.values(), Decimal(0))
        if snap["day_loss"] + open_risk + new_risk > snap["day_soft"]:
            return (f"дневной риск: убыток ${snap['day_loss']:.0f} + открытый ${open_risk:.0f} + новый ${new_risk:.0f} "
                    f"> порога ${snap['day_soft']:.0f} (лимит ${snap['day_lim']:.0f})")
        if snap["tot_loss"] + open_risk + new_risk > snap["tot_soft"]:
            return (f"общая просадка: ${snap['tot_loss']:.0f} + открытый ${open_risk:.0f} + новый ${new_risk:.0f} "
                    f"> порога ${snap['tot_soft']:.0f} (лимит ${snap['tot_lim']:.0f})")
        if DAY_GAIN_CAP_PCT > 0 and snap["day_gain"] >= snap["base"] * DAY_GAIN_CAP_PCT / 100:
            return f"дневной плюс ${snap['day_gain']:.0f} достиг потолка {DAY_GAIN_CAP_PCT}% (правило 30%)"
        return None

    def _check_hard(self, snap):
        """Аварийная защита: закрыть всё и остановить исполнение. Возвращает True, если сработала."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        kind = None
        if snap["day_loss"] >= snap["day_hard"]:
            kind = "day"
        elif snap["tot_loss"] >= snap["tot_hard"]:
            kind = "total"
        if kind and not self._trip:
            try:
                self.client.close_all(self.account_id)
                closed = "все позиции закрыты"
            except Exception as e:
                closed = f"⚠️ close-all не принят: {_trunc(e, 150)} — закрой руками!"
            self.halted = True
            self._trip = {"kind": kind, "date": today}
            what = (f"дневной убыток ${snap['day_loss']:.0f} из лимита ${snap['day_lim']:.0f}" if kind == "day"
                    else f"общая просадка ${snap['tot_loss']:.0f} из лимита ${snap['tot_lim']:.0f}")
            self.send(f"🚨 <b>АВАРИЙНАЯ ЗАЩИТА</b>: {what}. {closed}. Исполнение остановлено"
                      + (" до следующего дня UTC." if kind == "day" else " (общая просадка — только вручную /resume)."))
            return True
        if snap["day_loss"] >= snap["day_soft"] and self._soft_date != today:
            self._soft_date = today
            self.send(f"🟠 Дневной убыток ${snap['day_loss']:.0f} достиг порога ${snap['day_soft']:.0f} "
                      f"(лимит ${snap['day_lim']:.0f}) — новые входы заблокированы до завтра (UTC).")
        return False

    def _watchdog_tick(self):
        if self.mode != "demo" or not self.client:
            return
        self._ensure_account()
        if not self.account_id or self._demo_block():
            return
        snap = self._snapshot()
        if not snap:
            return
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._trip and self._trip["kind"] == "day" and self._trip["date"] != today and snap["day_loss"] < snap["day_soft"]:
            self._trip, self.halted = None, False
            self.send("▶️ Новый день (UTC): исполнение возобновлено.")
        self._check_hard(snap)

    def _watchdog(self):
        while True:
            time.sleep(WATCHDOG_SEC)
            try:
                self._watchdog_tick()
            except Exception:
                print(f"[EXEC] watchdog: {traceback.format_exc()}")

    def risk_summary(self) -> str:
        L = [f"upscale_exec v{EXEC_VERSION}"]
        try:
            self._ensure_account()
            if not self.client or not self.account_id:
                return "⛔ нет ключа или счёта"
            snap = self._snapshot()
            if not snap:
                return "\n".join(L + [f"⚠️ не смог прочитать risk-status: {self._snap_err}"])
            rs = snap["rs"]
            L.append(f"Эквити ${snap['equity']:.2f} | начало дня ${snap['day_start']:.2f} | старт ${snap['base']:.2f}")
            L.append(f"Дневной убыток ${snap['day_loss']:.2f} из лимита ${snap['day_lim']:.0f} ({snap['dd_pct']}%) — "
                     f"стоп входов ${snap['day_soft']:.0f}, аварийное закрытие ${snap['day_hard']:.0f}")
            L.append(f"Общая просадка ${snap['tot_loss']:.2f} из лимита ${snap['tot_lim']:.0f} ({snap['td_pct']}%) — "
                     f"стоп входов ${snap['tot_soft']:.0f}, аварийное закрытие ${snap['tot_hard']:.0f}")
            open_risk = sum(self._open_risk.values(), Decimal(0))
            L.append(f"Открытый риск по стопам (по данным бота): ${open_risk:.0f}")
            profit = snap["equity"] - from_fp9(rs.get("periodStartEquity") or 0)
            best = from_fp9(rs.get("maxPeriodDailyEquityDelta") or 0)
            ratio = (best / profit * 100) if profit > 0 else None
            L.append(f"Прибыль периода ${profit:.2f} | лучший день ${best:.2f}"
                     + (f" = {ratio:.0f}% (правило 30%: должно быть < 30%)" if ratio is not None else ""))
            L.append(f"Профитных дней: {self.acc.get('profitableDays', '?')} из {self.acc.get('minTradingDays', '?')} | цель {self.acc.get('profitTarget', '?')}%")
            if self._trip:
                L.append(f"🚨 Аварийная защита сработала: {self._trip}")
        except Exception as e:
            L.append(f"⚠️ {_trunc(e, 200)}")
        return "\n".join(L)

    # -- вход сигнала --
    def on_signal(self, b: dict, score: int):
        if self.mode == "off":
            return
        threading.Thread(target=self._handle, args=(dict(b), score), daemon=True).start()

    def _handle(self, b: dict, score: int):
        sym, side = b["symbol"], b["side"]
        skip, plan, market, result = None, None, None, ""
        try:
            if self.halted:
                skip = "исполнение остановлено (/halt)"
            if not skip:
                skip = chase_skip(b.get("ext_atr"))
            if not skip and self.client:
                self._ensure_account()
                self._refresh_markets()
                market = find_market(self._mk, sym) if self._mk else None
                if self._mk and not market:
                    skip = "монеты нет на Upscale"
            if not skip:
                # v1.9: уровни считаны по свечам Gate, а торгуем на Upscale. Если цены
                # площадок разошлись, сдвигаем ВСЕ уровни на то же отношение — тогда
                # стоп и цели остаются на своих процентах от реальной цены входа,
                # а не уезжают (из-за этого на SAND RR упало с 1:2 до 1:1.1).
                if market is not None:
                    up_px = market_price(market)
                    sig_px0 = Decimal(str(b["price"]))
                    if up_px and sig_px0 > 0:
                        k = up_px / sig_px0
                        if Decimal("0.9") < k < Decimal("1.1"):      # защита от битых данных
                            if abs(k - 1) > Decimal("0.0005"):
                                self.send(f"📐 {sym}: цена Upscale {up_px:.6g} против {sig_px0:.6g} "
                                          f"по свечам ({(k-1)*100:+.2f}%) — сдвигаю уровни под Upscale")
                            for f_ in ("price", "stop", "tp1_price", "tp2_price", "tp3_price"):
                                if b.get(f_):
                                    b[f_] = float(Decimal(str(b[f_])) * k)
                plan = build_plan(b["price"], b["stop_pct"], side, b["stop"],
                                  b["tp1_price"], b["tp2_price"], self.risk_usd, self.max_pos_usd)
            if not skip and self.mode == "demo":
                skip = self._real_block()
                if not skip and MAX_TRADES_DAY and self._day_count() >= MAX_TRADES_DAY:
                    skip = f"лимит {MAX_TRADES_DAY} входов за сутки UTC"
                if not skip:
                    result = self._execute(b, plan, market)
        except Exception as e:
            skip = f"ошибка: {e}"
            print(f"[EXEC] {sym}: {traceback.format_exc()}")
        self._report(b, score, plan, skip, result)

    # -- реальное исполнение (только демо) --
    def _positions(self):
        return [p for p in _as_list(self.client.positions(self.account_id)) if isinstance(p, dict)]

    def _wait_position(self, mid, direction, before, tries=10):
        for _ in range(tries):
            time.sleep(1)
            cands = [p for p in self._positions() if _pos_id(p) not in before]
            for p in cands:
                if _pos_market(p) == mid and _pos_dir(p) == direction:
                    return p
            if len(cands) == 1 and _pos_market(cands[0]) in ("", mid):   # единственная новая позиция
                return cands[0]
        return None

    def _execute(self, b, plan, market) -> str:
        sym, side, mid, acc = b["symbol"], b["side"], str(market["id"]), self.account_id
        pos_now = self._positions()
        if MAX_OPEN and len(pos_now) >= MAX_OPEN:
            return f"пропуск: уже {len(pos_now)} открытых позиций (лимит {MAX_OPEN})"
        if any(_pos_market(p) == mid for p in pos_now):
            return "пропуск: по монете уже есть позиция"
        before = {_pos_id(p) for p in pos_now}
        live = set(before)
        self._open_risk = {k: v for k, v in self._open_risk.items() if k in live}
        snap = self._snapshot()
        if snap is None:
            return f"пропуск: не смог прочитать risk-status ({self._snap_err})"
        why = self._guard_skip(snap, plan["real_risk_usd"])
        if why:
            return "пропуск: " + why
        body = open_body(acc, mid, side, plan["margin_usd"], plan["leverage"], stop=b["stop"])
        try:
            self.client.order(body)
        except UpscaleError as e:
            return f"ордер отклонён: {e}"
        self._day["n"] += 1
        pos = self._wait_position(mid, side, before)
        if not pos:
            return "ордер отправлен, но позицию не нашёл (проверь терминал)"
        size = _pos_size(pos)
        size_base = Decimal(size) / FP
        notional = abs(from_fp9(pos["notional"])) if pos.get("notional") else Decimal(0)
        if notional <= 0:                                        # нет поля или мусор
            notional = size_base * Decimal(str(b["price"]))
        sig_px = Decimal(str(b["price"]))
        fill = notional / size_base if size_base else sig_px      # цена входа = notional / размер
        if not (sig_px / 2 < fill < sig_px * 2):                  # v1.8: страховка от битых данных
            self.send(f"⚠️ {sym}: цена входа из API ({fill:.6g}) не похожа на цену сигнала "
                      f"({sig_px:.6g}) — считаю по сигналу. Проверь позицию в терминале.")
            fill = sig_px
        slip = (fill - sig_px) / sig_px * 100 * (1 if side == "long" else -1)      # + = вход хуже сигнала
        # v1.7: риск считаем от ФАКТИЧЕСКОГО входа до ФАКТИЧЕСКОГО стопа.
        # Раньше брали плановый stop_pct — при проскальзывании 0.21% и стопе 0.5%
        # реальная дистанция до стопа 0.71%, и риск занижался почти в полтора раза
        # (а заниженная цифра шла ещё и в защиту от просадки).
        stop_px = Decimal(str(b["stop"]))
        risk_px = abs(fill - stop_px)
        real_stop_pct = risk_px / fill * 100
        real_risk = notional * real_stop_pct / 100
        # Цели НЕ двигаем: они структурные, а не кратные риску. В УКЛОНЕ TP2 стоит
        # чуть перед границей коридора, в ПРОБОЕ цели берутся из свингов и высоты
        # диапазона. Пересчёт «чтобы сохранить RR» вынес бы их за структуру —
        # в живой сделке SAND TP2 уехал бы выше самой границы.
        # Проскальзывание честно ухудшает RR, и это надо ПОКАЗАТЬ, а не замаскировать.
        rr1_real = abs(Decimal(str(b["tp1_price"])) - fill) / (risk_px or Decimal(1))
        info = {"id": _pos_id(pos), "mid": mid, "dir": side, "size": size,
                "tp1": b["tp1_price"], "tp2": b["tp2_price"],
                "tp3": b.get("tp3_price"), "sym": sym, "entry": fill}
        self._open_risk[info["id"]] = real_risk
        threading.Timer(TP_DELAY_SEC, self._place_tps, args=(info,)).start()
        self.send(f"✅ <b>DEMO · {sym} {side.upper()} открыт</b>: размер {size_base:.6g} ≈ ${notional:.0f}, "
                  f"вход {fill:.6g} (проскальзывание {slip:+.2f}% к сигналу), "
                  f"риск по стопу ≈ ${real_risk:.1f} (план ${plan['real_risk_usd']:.1f}, "
                  f"стоп {real_stop_pct:.2f}% от входа). "
                  f"Стоп {b['stop']:.6g} со входом. TP {b['tp1_price']:.6g} / {b['tp2_price']:.6g} "
                  f"(реальное RR к TP1 = 1:{float(rr1_real):.1f}) через {TP_DELAY_SEC}с.")
        if rr1_real < Decimal("1"):
            self.send(f"⚠️ {sym}: проскальзывание {slip:+.2f}% срезало RR до 1:{float(rr1_real):.1f} — "
                      f"цель ближе стопа. Цели структурные, двигать их нельзя; "
                      f"если такое повторяется, увеличивай буфер входа или стоп.")
        if plan["real_risk_usd"] and abs(real_risk - plan["real_risk_usd"]) / plan["real_risk_usd"] > Decimal("0.3"):
            self.send(f"⚠️ {sym}: фактический риск ${real_risk:.1f} сильно отличается от плана "
                      f"${plan['real_risk_usd']:.1f} — проверь, как API трактует amount/плечо.")
        return f"открыт pos={info['id']} size={size_base:.6g} risk≈{real_risk:.1f} slip={slip:+.3f}%"

    def _send_take(self, info, amount, price):
        """take-ордер; направление — как у позиции, при отказе пробуем встречное и запоминаем."""
        order_dirs = [info["dir"], _opp(info["dir"])] if self.close_dir == "same" else [_opp(info["dir"]), info["dir"]]
        last = None
        for d in order_dirs:
            try:
                self.client.order(take_body(self.account_id, info["mid"], d, info["id"], amount, price))
                self.close_dir = "same" if d == info["dir"] else "opposite"
                return
            except UpscaleError as e:
                last = e
        raise last

    def _place_tps(self, info):
        try:
            if self.halted or not self.client:
                return
            cur = next((p for p in self._positions() if _pos_id(p) == info["id"]), None)
            if not cur:
                self.send(f"ℹ️ {info['sym']}: позиция уже закрыта (стоп?) — TP не ставлю.")
                return
            size = _pos_size(cur) or info["size"]
            tps = [t for t in (info.get("tp1"), info.get("tp2"), info.get("tp3")) if t]
            n = len(tps)
            if n >= 2:
                part = size // n
                legs = [(part, tps[i], f"TP{i+1}") for i in range(n - 1)]
                legs.append((size - part * (n - 1), tps[-1], f"TP{n}"))
            else:
                legs = [(size, tps[0], "TP1")] if tps else []
            done = []
            for amt, price, label in legs:
                if amt <= 0:
                    continue
                try:
                    self._send_take(info, amt, price)
                    done.append(f"{label} {price:.6g}")
                except UpscaleError as e:
                    self.send(f"⚠️ {info['sym']}: {label} не принят: {_trunc(e, 250)}")
            if done and BREAKEVEN_ENABLED and len(legs) > 1:
                info["size"] = size
                info["n_legs"] = len(legs)
                threading.Thread(target=self._watch_breakeven, args=(info,), daemon=True).start()
        except Exception as e:
            print(f"[EXEC] place_tps: {traceback.format_exc()}")
            self.send(f"⚠️ {info['sym']}: ошибка постановки TP: {e}")

    # -- безубыток после TP1 --
    def _send_stop(self, info, amount, price):
        """stop-ордер; направление как у take (уже выяснено рабочее в close_dir)."""
        dirs = [info["dir"], _opp(info["dir"])] if self.close_dir == "same" else [_opp(info["dir"]), info["dir"]]
        last = None
        for d in dirs:
            try:
                self.client.order(stop_body(self.account_id, info["mid"], d, info["id"], amount, price))
                return
            except UpscaleError as e:
                last = e
        raise last

    def _watch_breakeven(self, info):
        """Подтягивает стоп по мере взятия целей:
          после TP1 — в безубыток (риска больше нет);
          после TP2 — на цену TP1 (прибыль заперта).
        Именно подтяжка даёт основной прирост: в бэктесте те же три цели без неё
        давали +0.174R, с ней +0.264R, винрейт 66% против 77%.
        Факт взятия цели определяем по уменьшению размера позиции."""
        deadline = time.time() + BREAKEVEN_MAX_MIN * 60
        start = info["size"]
        legs = max(2, int(info.get("n_legs", 2)))
        moved = 0
        while time.time() < deadline:
            time.sleep(BREAKEVEN_POLL)
            try:
                if not self.client:
                    return
                cur = next((p for p in self._positions() if _pos_id(p) == info["id"]), None)
                if not cur:
                    return                               # позиция закрыта целиком
                size_now = _pos_size(cur)
                if size_now <= 0:
                    return
                taken = round((start - size_now) / start * legs)   # сколько целей взято
                if taken <= moved:
                    continue
                if taken == 1:
                    lvl, what = breakeven_price(info["entry"], info["dir"]), "безубыток"
                elif taken >= 2 and info.get("tp1"):
                    lvl, what = Decimal(str(info["tp1"])), "уровень TP1"
                else:
                    continue
                self._send_stop(info, size_now, lvl)
                moved = taken
                self.send(f"🔒 {info['sym']}: взята цель {taken} — стоп переставлен "
                          f"в {what} ({float(lvl):.6g}) на остаток.")
                if taken >= legs - 1:
                    return
            except UpscaleError as e:
                self.send(f"⚠️ {info['sym']}: стоп не переставлен: {_trunc(e, 250)} — "
                          f"перенеси руками.")
                return
            except Exception:
                print(f"[EXEC] trail: {traceback.format_exc()}")
                return

    # -- отчёт --
    def _report(self, b, score, plan, skip, result=""):
        sym, side = b["symbol"], b["side"]
        row = {"ts": int(time.time()), "symbol": sym, "side": side, "score": score,
               "price": b["price"], "stop": b["stop"], "tp1": b["tp1_price"], "tp2": b["tp2_price"],
               "ext_atr": b.get("ext_atr"), "skip": skip or "",
               "pos_usd": round(float(plan["pos_usd"]), 2) if plan else "",
               "margin_usd": round(float(plan["margin_usd"]), 2) if plan else "",
               "mode": self.mode, "result": result}
        try:
            self.csv(row)
        except Exception as e:
            print(f"[EXEC] csv: {e}")
        arrow = "🟢 LONG" if side == "long" else "🔴 SHORT"
        if skip:
            self.send(f"🧪 <b>{self.mode.upper()} · {sym} {arrow}: пропускаю</b> — {skip}")
        elif self.mode == "dry":
            self.send(f"🧪 <b>DRY · открыл бы {sym} {arrow}</b>\n"
                      f"позиция ${plan['pos_usd']:.0f} (риск ${plan['real_risk_usd']:.1f}), "
                      f"плечо {plan['leverage']}×, маржа ≈ ${plan['margin_usd']:.0f}\n"
                      f"вход ≈ {b['price']:.6g} | стоп {b['stop']:.6g} со входом | "
                      f"TP1 {b['tp1_price']:.6g} и TP2 {b['tp2_price']:.6g} через {plan['tp_delay_sec']}с (правило 60с)")
        elif result and not result.startswith("открыт"):
            self.send(f"⚠️ <b>DEMO · {sym} {arrow}</b>: {result}")

    # -- команды --
    def halt(self):
        self.halted = True
        return "🛑 Исполнение остановлено (новые входы и постановка TP). Сигналы в телеграм идут как обычно. /resume — включить."

    def resume(self):
        self.halted = False
        return "▶️ Исполнение включено."

    def closeall(self) -> str:
        try:
            self._ensure_account()
            block = self._demo_block()
            if block:
                return f"⛔ {block}"
            self.client.close_all(self.account_id)
            return "🧹 Команда close-all отправлена (демо-счёт)."
        except Exception as e:
            return f"⚠️ close-all: {_trunc(e, 300)}"

    def selftest(self) -> str:
        """Как Quickstart: открыть BTC long на $100 резерва ×5 (без TP/SL), найти позицию, закрыть. Только демо."""
        L = [f"upscale_exec v{EXEC_VERSION}"]
        try:
            self._ensure_account()
            self._refresh_markets()
            block = self._demo_block()
            if block:
                return f"⛔ /uptest не выполнен: {block}"
            m = find_market(self._mk, "BTC")
            if not m:
                return "⛔ рынок BTC не найден"
            mid = str(m["id"])
            existing = self._positions()
            before = {_pos_id(p) for p in existing}
            body = open_body(self.account_id, mid, "long", 100, 5)
            L.append("1) открываю BTC long $100 ×5: " + _trunc(body, 250))
            resp = self.client.order(body)
            L.append("   ответ: " + _trunc(resp, 300))
            pos = self._wait_position(mid, "long", before)
            if not pos:
                raw = self._positions()
                L.append("⚠️ позицию по полям не нашёл. Сырые активные позиции (" + str(len(raw)) + "): " + _trunc(raw, 700))
                if not existing:      # до теста позиций не было — значит, любая открытая от теста, можно чистить
                    try:
                        self.client.close_all(self.account_id)
                        L.append("🧹 до теста позиций не было — отправил close-all")
                    except UpscaleError as e:
                        L.append(f"⚠️ close-all не принят: {_trunc(e, 200)} — закрой руками или /closeall")
                else:
                    L.append("Были и другие позиции — закрой тестовую руками")
                return "\n".join(L)
            L.append("2) позиция: " + _trunc(pos, 500))
            size = _pos_size(pos)
            L.append(f"   size(fp9)={size} → {Decimal(size) / FP:.6g} BTC")
            info = {"id": _pos_id(pos), "mid": mid, "dir": "long", "size": size}
            try:
                self._send_take(info, size, 0)
                L.append(f"3) закрытие take triggerPrice=0 принято (направление: {self.close_dir})")
            except UpscaleError as e:
                L.append(f"3) ⚠️ закрытие не принято: {_trunc(e, 300)}\n   делаю close-all")
                self.client.close_all(self.account_id)
            time.sleep(2)
            left = [p for p in self._positions() if _pos_id(p) == info["id"]]
            L.append("4) позиция закрыта ✅" if not left else "4) ⚠️ позиция ещё открыта — /closeall")
        except Exception as e:
            L.append(f"⚠️ /uptest: {_trunc(e, 300)}")
        return "\n".join(L)

    def risk_dump(self) -> str:
        """Сырые данные для настройки защиты по просадке: риск-статус и все поля счёта."""
        L = [f"upscale_exec v{EXEC_VERSION}"]
        try:
            self._ensure_account()
            if not self.client or not self.account_id:
                return "⛔ нет ключа или счёта"
            acc = next((a for a in _as_list(self.client.accounts())
                        if isinstance(a, dict) and str(pick(a, "accountId", "id")) == self.account_id), None)
            L.append("Счёт (все поля): " + _trunc(acc, 1500))
        except Exception as e:
            L.append(f"⚠️ счёт: {_trunc(e, 200)}")
        try:
            L.append("risk-status: " + _trunc(self.client.risk_status(self.account_id), 1800))
        except Exception as e:
            L.append(f"⚠️ risk-status: {_trunc(e, 200)}")
        return "\n".join(L)

    def status(self) -> str:
        lines = [f"🤖 upscale_exec v{EXEC_VERSION} | Авто-режим: <b>{self.mode}</b>" + (" (реальные ордера ТОЛЬКО на демо-счёт)" if self.mode == "demo" else ""),
                 f"Пауза: {'да' if self.halted else 'нет'} | ключ: {'есть' if self.client else 'НЕТ'}"]
        if not self.client:
            return "\n".join(lines)
        try:
            self._ensure_account()
            accs = _as_list(self.client.accounts())
            lines.append(f"Счетов доступно по ключу: {len(accs)} | тип выбранного: {self.account_type or '?'}")
            for a in accs[:3]:
                if isinstance(a, dict):
                    lines.append("  • " + ", ".join(f"{k}={v}" for k, v in list(a.items())[:8]))
            if not self.account_id:
                lines.append("⚠️ UPSCALE_ACCOUNT_ID не задан и счёт неоднозначен")
            else:
                self._mk_ts = 0
                raw = self.client.markets(self.account_id)
                self._mk, self._mk_ts = market_index(raw), time.time()
                lines.append(f"Счёт: {self.account_id} | рынков: {len(_as_list(raw))}, тикеров: {len(self._mk)}")
                first = next((m for m in _as_list(raw) if isinstance(m, dict)), {})
                if isinstance(first.get("state"), dict):
                    lines.append("Поля state: " + ", ".join(list(first["state"].keys())[:20]))
                if self.pairs and self._mk:
                    miss = [p for p in self.pairs if not find_market(self._mk, p)]
                    lines.append(f"Пар бота на Upscale: {len(self.pairs) - len(miss)} из {len(self.pairs)}"
                                 + (f" (нет: {', '.join(miss[:40])})" if miss else ""))
                snap = self._snapshot()
                if snap:
                    lines.append(f"Эквити ${snap['equity']:.2f} | убыток дня ${snap['day_loss']:.2f}/${snap['day_lim']:.0f} | просадка ${snap['tot_loss']:.2f}/${snap['tot_lim']:.0f}")
                try:
                    lines.append("Открыто позиций: " + str(len(self._positions())) + f" | входов сегодня (UTC): {self._day_count()}/{MAX_TRADES_DAY}")
                except Exception as e:
                    lines.append(f"⚠️ позиции: {_trunc(e, 200)}")
        except Exception as e:
            lines.append(f"⚠️ API: {e}")
        return "\n".join(lines)
