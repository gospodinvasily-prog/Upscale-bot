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

BASE_URL      = os.environ.get("UPSCALE_API_URL", "https://api.upscale.trade")
FP            = Decimal(10) ** 9
LEVERAGE      = Decimal(os.environ.get("EXEC_LEVERAGE", "5"))
MARGIN_BUFFER = Decimal(os.environ.get("EXEC_MARGIN_BUFFER", "0.10"))   # запас на комиссию/спред
MAX_CHASE_ATR = float(os.environ.get("EXEC_MAX_CHASE_ATR", "0.3"))
TP_DELAY_SEC  = int(os.environ.get("EXEC_TP_DELAY_SEC", "65"))
MAX_OPEN      = int(os.environ.get("EXEC_MAX_OPEN", "3"))
MAX_TRADES_DAY = int(os.environ.get("EXEC_MAX_TRADES_DAY", "8"))
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

def take_body(account_id, market_id, direction, position_id, amount_fp9, trigger_price) -> dict:
    """take-ордер по позиции: amount = размер в базовом активе (fp9), trigger 0 = по рынку."""
    return {"accountId": account_id, "marketId": market_id, "type": "take", "direction": direction,
            "positionId": position_id, "amount": str(amount_fp9), "triggerPrice": to_fp9(trigger_price)}

def _pos_id(p):     return str(pick(p, "id", "positionId") or "")
def _pos_market(p):
    m = pick(p, "marketId")
    if m is None and isinstance(p.get("market"), dict):
        m = p["market"].get("id")
    return str(m or "")
def _pos_dir(p):    return str(pick(p, "direction", "side") or "").lower()
def _pos_size(p):
    """размер позиции в fp9 (целое). Числа в API — строки ×10⁹."""
    v = pick(p, "size", "amount", "quantity")
    if v is None:
        return 0
    v = str(v)
    return int(v) if v.isdigit() else int(to_fp9(v))

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
        self._mk, self._mk_ts = {}, 0.0
        self._day = {"date": "", "n": 0}
        self._lock = threading.Lock()

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
        if self.account_id and self.account_type:
            return
        accs = [a for a in _as_list(self.client.accounts()) if isinstance(a, dict)]
        if not self.account_id and len(accs) == 1:
            self.account_id = str(pick(accs[0], "accountId", "id") or "")
        for a in accs:
            if str(pick(a, "accountId", "id")) == self.account_id:
                self.account_type = str(a.get("type") or "").lower()

    def _real_block(self):
        """Причина, по которой реальные ордера слать нельзя (None — можно)."""
        if self.mode != "demo":
            return "режим не demo"
        if not self.client or not self.account_id:
            return "нет ключа или счёта"
        if self.account_type != "demo":
            return f"счёт не демо (type={self.account_type or '?'}) — ордера запрещены"
        return None

    def _day_count(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._day["date"] != today:
            self._day = {"date": today, "n": 0}
        return self._day["n"]

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
                plan = build_plan(b["price"], b["stop_pct"], side, b["stop"],
                                  b["tp1_price"], b["tp2_price"], self.risk_usd, self.max_pos_usd)
            if not skip and self.mode == "demo":
                skip = self._real_block()
                if not skip and self._day_count() >= MAX_TRADES_DAY:
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
            for p in self._positions():
                if _pos_market(p) == mid and _pos_dir(p) == direction and _pos_id(p) not in before:
                    return p
        return None

    def _execute(self, b, plan, market) -> str:
        sym, side, mid, acc = b["symbol"], b["side"], str(market["id"]), self.account_id
        pos_now = self._positions()
        if len(pos_now) >= MAX_OPEN:
            return f"пропуск: уже {len(pos_now)} открытых позиций (лимит {MAX_OPEN})"
        if any(_pos_market(p) == mid for p in pos_now):
            return "пропуск: по монете уже есть позиция"
        before = {_pos_id(p) for p in pos_now}
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
        notional = size_base * Decimal(str(b["price"]))
        real_risk = notional * Decimal(str(b["stop_pct"])) / 100
        info = {"id": _pos_id(pos), "mid": mid, "dir": side, "size": size,
                "tp1": b["tp1_price"], "tp2": b["tp2_price"], "sym": sym}
        threading.Timer(TP_DELAY_SEC, self._place_tps, args=(info,)).start()
        self.send(f"✅ <b>DEMO · {sym} {side.upper()} открыт</b>: размер {size_base:.6g} ≈ ${notional:.0f}, "
                  f"риск по стопу ≈ ${real_risk:.1f} (план ${plan['real_risk_usd']:.1f}). "
                  f"Стоп {b['stop']:.6g} выставлен со входом. TP поставлю через {TP_DELAY_SEC}с.")
        if plan["real_risk_usd"] and abs(real_risk - plan["real_risk_usd"]) / plan["real_risk_usd"] > Decimal("0.3"):
            self.send(f"⚠️ {sym}: фактический риск ${real_risk:.1f} сильно отличается от плана "
                      f"${plan['real_risk_usd']:.1f} — проверь, как API трактует amount/плечо.")
        return f"открыт pos={info['id']} size={size_base:.6g} risk≈{real_risk:.1f}"

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
            half = size // 2
            legs = [(half, info["tp1"], "TP1"), (size - half, info["tp2"], "TP2")] if half > 0 else [(size, info["tp1"], "TP1")]
            done = []
            for amt, price, label in legs:
                try:
                    self._send_take(info, amt, price)
                    done.append(f"{label} {price:.6g}")
                except UpscaleError as e:
                    self.send(f"⚠️ {info['sym']}: {label} не принят: {_trunc(e, 300)}")
            if done:
                self.send(f"🎯 {info['sym']}: выставлены " + ", ".join(done))
        except Exception as e:
            print(f"[EXEC] place_tps: {traceback.format_exc()}")
            self.send(f"⚠️ {info['sym']}: ошибка постановки TP: {e}")

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
            block = self._real_block()
            if block:
                return f"⛔ {block}"
            self.client.close_all(self.account_id)
            return "🧹 Команда close-all отправлена (демо-счёт)."
        except Exception as e:
            return f"⚠️ close-all: {_trunc(e, 300)}"

    def selftest(self) -> str:
        """Как Quickstart: открыть BTC long на $100 резерва ×5 (без TP/SL), найти позицию, закрыть. Только демо."""
        L = []
        try:
            self._ensure_account()
            self._refresh_markets()
            block = self._real_block()
            if block:
                return f"⛔ /uptest не выполнен: {block}"
            m = find_market(self._mk, "BTC")
            if not m:
                return "⛔ рынок BTC не найден"
            mid = str(m["id"])
            before = {_pos_id(p) for p in self._positions()}
            body = open_body(self.account_id, mid, "long", 100, 5)
            L.append("1) открываю BTC long $100 ×5: " + _trunc(body, 250))
            resp = self.client.order(body)
            L.append("   ответ: " + _trunc(resp, 300))
            pos = self._wait_position(mid, "long", before)
            if not pos:
                return "\n".join(L + ["⚠️ позиция не появилась за 10с — проверь терминал (/closeall если открылась)"])
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

    def status(self) -> str:
        lines = [f"🤖 Авто-режим: <b>{self.mode}</b>" + (" (реальные ордера ТОЛЬКО на демо-счёт)" if self.mode == "demo" else ""),
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
                try:
                    lines.append("Открыто позиций: " + str(len(self._positions())) + f" | входов сегодня (UTC): {self._day_count()}/{MAX_TRADES_DAY}")
                except Exception as e:
                    lines.append(f"⚠️ позиции: {_trunc(e, 200)}")
        except Exception as e:
            lines.append(f"⚠️ API: {e}")
        return "\n".join(lines)
