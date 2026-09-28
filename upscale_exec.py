"""
Слой исполнения для Upscale API (v0: чтение + DRY-RUN).

ВАЖНО: отправка ордеров здесь НЕ реализована — в полученной документации нет
формата тела POST /orders (названия полей рынка/стороны). Модуль умеет:
  • читать счета, рынки, позиции, risk-status (эндпоинты из Quickstart);
  • считать полный план сделки (размер, маржа, стоп, TP с задержкой 60с);
  • в режиме dry писать в телеграм и в CSV «что бы открыл»;
  • kill-switch (/halt, /resume).
Сигналы в телеграм от этого модуля не зависят: любая ошибка здесь гасится.

Переменные окружения:
  AUTO_TRADE          off | dry  (demo = пока то же, что dry, пока нет справочника ордеров)
  UPSCALE_API_KEY     ключ (только в Render, в чат не присылать)
  UPSCALE_AUTH_SCHEME "" (ключ как есть) или "Bearer"
  UPSCALE_ACCOUNT_ID  id демо-счёта (если пусто — берём единственный из списка)
  EXEC_LEVERAGE       плечо для расчёта маржи (по умолчанию 5)
  EXEC_MAX_CHASE_ATR  не входить, если цена ушла от уровня дальше N ATR (0.3)
  EXEC_TP_DELAY_SEC   когда можно ставить TP после входа (65, правило 60с)
"""
import os
import re
import time
import threading
import traceback
from decimal import Decimal, ROUND_DOWN, InvalidOperation

import requests

BASE_URL      = os.environ.get("UPSCALE_API_URL", "https://api.upscale.trade")
FP            = Decimal(10) ** 9
LEVERAGE      = Decimal(os.environ.get("EXEC_LEVERAGE", "5"))
MARGIN_BUFFER = Decimal(os.environ.get("EXEC_MARGIN_BUFFER", "0.10"))   # запас на комиссию/спред
MAX_CHASE_ATR = float(os.environ.get("EXEC_MAX_CHASE_ATR", "0.3"))
TP_DELAY_SEC  = int(os.environ.get("EXEC_TP_DELAY_SEC", "65"))
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

    def accounts(self):            return self._get("/accounts/with-risk-status")
    def markets(self, acc_id):     return self._get("/v2/markets", {"accountId": acc_id})
    def positions(self, acc_id):   return self._get(f"/positions/{acc_id}/active")
    def risk_status(self, acc_id): return self._get(f"/accounts/{acc_id}/risk-status")


# ── разбор ответов (формат неизвестен → защитно) ─────────────────────────────
def _as_list(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("data", "items", "results", "accounts", "markets", "positions"):
            if isinstance(data.get(k), list):
                return data[k]
    return []

def _norm_sym(s: str) -> str:
    s = re.sub(r"[^A-Z0-9]", "", str(s).upper())
    for suf in ("PERP", "USDT", "USDC", "USD"):
        if s.endswith(suf) and len(s) > len(suf):
            s = s[: -len(suf)]
    return s

def market_index(markets_raw) -> dict:
    """{'SEI': market_dict, ...} по любым полям, похожим на тикер."""
    idx = {}
    for m in _as_list(markets_raw):
        if not isinstance(m, dict):
            continue
        for f in ("symbol", "name", "ticker", "baseSymbol", "baseAsset", "base", "asset"):
            if isinstance(m.get(f), str):
                idx.setdefault(_norm_sym(m[f]), m)
    return idx

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


# ── исполнитель ──────────────────────────────────────────────────────────────
class Executor:
    def __init__(self, send_fn, csv_fn, risk_usd, max_pos_usd):
        self.send, self.csv = send_fn, csv_fn
        self.risk_usd, self.max_pos_usd = risk_usd, max_pos_usd
        req = os.environ.get("AUTO_TRADE", "off").strip().lower()
        self.mode = req if req in ("off", "dry", "demo") else "off"
        self.demo_downgraded = self.mode == "demo"      # ордера ещё не написаны
        if self.demo_downgraded:
            self.mode = "dry"
        self.halted = False
        key = os.environ.get("UPSCALE_API_KEY", "").strip()
        self.client = UpscaleClient(key, os.environ.get("UPSCALE_AUTH_SCHEME", "")) if key else None
        self.account_id = os.environ.get("UPSCALE_ACCOUNT_ID", "").strip()
        self._mk, self._mk_ts = {}, 0.0
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
        if self.account_id or not self.client:
            return
        accs = [a for a in _as_list(self.client.accounts()) if isinstance(a, dict)]
        if len(accs) == 1:
            self.account_id = str(pick(accs[0], "accountId", "id") or "")

    # -- вход сигнала --
    def on_signal(self, b: dict, score: int):
        if self.mode == "off":
            return
        threading.Thread(target=self._handle, args=(dict(b), score), daemon=True).start()

    def _handle(self, b: dict, score: int):
        sym, side = b["symbol"], b["side"]
        skip = None
        plan = None
        try:
            if self.halted:
                skip = "исполнение остановлено (/halt)"
            if not skip:
                skip = chase_skip(b.get("ext_atr"))
            if not skip and self.client:
                self._ensure_account()
                self._refresh_markets()
                if self._mk and _norm_sym(sym) not in self._mk:
                    skip = "монеты нет на Upscale"
            if not skip:
                plan = build_plan(b["price"], b["stop_pct"], side, b["stop"],
                                  b["tp1_price"], b["tp2_price"], self.risk_usd, self.max_pos_usd)
        except Exception as e:
            skip = f"ошибка расчёта: {e}"
            print(f"[EXEC] {sym}: {traceback.format_exc()}")
        self._report(b, score, plan, skip)

    def _report(self, b, score, plan, skip):
        sym, side = b["symbol"], b["side"]
        row = {"ts": int(time.time()), "symbol": sym, "side": side, "score": score,
               "price": b["price"], "stop": b["stop"], "tp1": b["tp1_price"], "tp2": b["tp2_price"],
               "ext_atr": b.get("ext_atr"), "skip": skip or "",
               "pos_usd": round(float(plan["pos_usd"]), 2) if plan else "",
               "margin_usd": round(float(plan["margin_usd"]), 2) if plan else "",
               "mode": self.mode}
        try:
            self.csv(row)
        except Exception as e:
            print(f"[EXEC] csv: {e}")
        arrow = "🟢 LONG" if side == "long" else "🔴 SHORT"
        if skip:
            txt = f"🧪 <b>DRY · {sym} {arrow}: пропускаю</b> — {skip}"
        else:
            txt = (f"🧪 <b>DRY · открыл бы {sym} {arrow}</b>\n"
                   f"позиция ${plan['pos_usd']:.0f} (риск ${plan['real_risk_usd']:.1f}), "
                   f"плечо {plan['leverage']}×, маржа ≈ ${plan['margin_usd']:.0f}\n"
                   f"вход ≈ {b['price']:.6g} | стоп {b['stop']:.6g} со входом | "
                   f"TP1 {b['tp1_price']:.6g} и TP2 {b['tp2_price']:.6g} через {plan['tp_delay_sec']}с "
                   f"(правило 60с)")
        self.send(txt)

    # -- команды --
    def halt(self):
        self.halted = True
        return "🛑 Исполнение остановлено. Сигналы в телеграм идут как обычно. /resume — включить."

    def resume(self):
        self.halted = False
        return "▶️ Исполнение включено."

    def status(self) -> str:
        lines = [f"🤖 Авто-режим: <b>{self.mode}</b>" + (" (demo пока = dry: ордера не подключены)" if self.demo_downgraded else ""),
                 f"Пауза: {'да' if self.halted else 'нет'} | ключ: {'есть' if self.client else 'НЕТ'}"]
        if not self.client:
            return "\n".join(lines)
        try:
            self._ensure_account()
            accs = _as_list(self.client.accounts())
            lines.append(f"Счетов доступно по ключу: {len(accs)}")
            for a in accs[:3]:
                if isinstance(a, dict):
                    lines.append("  • " + ", ".join(f"{k}={v}" for k, v in list(a.items())[:8]))
            if not self.account_id:
                lines.append("⚠️ UPSCALE_ACCOUNT_ID не задан и счёт неоднозначен")
            else:
                self._mk_ts = 0
                self._refresh_markets()
                lines.append(f"Счёт: {self.account_id} | рынков распознано: {len(self._mk)}")
                if self._mk:
                    m = next(iter(self._mk.values()))
                    lines.append("Поля рынка: " + ", ".join(list(m.keys())[:15]))
        except Exception as e:
            lines.append(f"⚠️ API: {e}")
        return "\n".join(lines)
