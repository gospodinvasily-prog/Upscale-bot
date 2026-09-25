"""
Бэктест Upscale Bot: ЗАРЯД → ПРОБОЙ на истории Gate.io.

Что проверяет:
  • таймфрейм заряда (15m / 30m / 1h) и окно сжатия;
  • подтверждение пробоя: касание цены / закрытие 1m / 5m / 15m свечи;
  • требование к объёму свечи пробоя;
  • стоп (множитель ATR, потолок) и цели (в стопах или по структуре);
  • окна времени МСК (весь день / окна v8.2);
  • обе стороны отдельно, с учётом комиссии.

Чего НЕ проверяет: OI, фандинг, taker L/S, дельту — Gate не отдаёт их историю.
Эти показатели проверяются только вперёд, на живой работе бота.

Запуск: python backtest.py
Переменные окружения: TELEGRAM_TOKEN, CHAT_ID (необязательно — тогда только в лог),
  BT_DAYS (по умолчанию 45), BT_PAIRS (сколько пар, по умолчанию 40),
  BT_CONFIRM_TF (1m|5m — таймфрейм подтверждения; 1m точнее, но качает в 5 раз больше данных).
Зависимости: только requests.
"""

import os
import time

import statistics
import itertools
import threading
import gc
from datetime import datetime, timezone, timedelta

import requests

# ─── НАСТРОЙКИ ────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID        = os.environ.get("CHAT_ID", "")
DAYS           = int(os.environ.get("BT_DAYS", "45"))
N_PAIRS        = int(os.environ.get("BT_PAIRS", "103"))   # все пары; пары считаются по очереди, память не копится
CONFIRM_TF     = os.environ.get("BT_CONFIRM_TF", "5m")     # 1m — точнее, но тяжелее
FEE_PCT        = 0.10        # комиссия вход+выход, % от объёма (Gate taker ~0.05% × 2)
SLIP_PCT       = float(os.environ.get("BT_SLIP", "0.05"))   # проскальзывание на КАЖДОЙ стороне, %
COST_PCT       = FEE_PCT + 2 * SLIP_PCT                     # полные издержки на сделку
MSK            = timezone(timedelta(hours=3))

ALL_PAIRS = [
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
    "WIF","WLD","XLM","XMR","XTZ","ZEC","ZRO","0G",
]

TF_SEC = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600}

# ─── ПЕРЕБИРАЕМЫЕ НАСТРОЙКИ ───────────────────────────────────────────────────
# Каждая комбинация прогоняется по всем парам и всей истории.

GRID = {
    "charge_tf":    ["30m", "1h"],             # 15м в трёх прогонах был худшим — убран
    "squeeze_pctl": [25, 35],
    "confirm":      ["touch", "close"],        # касание уровня или закрытие свечи подтверждения
    "min_bar_rvol": [1.2, 2.0],
    "stop_atr":     [1.0, 1.5],
    "targets":      ["struct", "1.5R/3R"],
    "session":      ["all", "v82"],
    # фильтр по состоянию BTC: off — без фильтра; trend12 — только по движению за 12ч;
    # fast1 — только по движению за 1ч (ловит разворот рано); combo — по 1ч, но не против 12ч
    "btc_filter":   ["off", "trend12", "fast1", "combo"],
}

ACC_WINDOW      = 12      # свечей в окне заряда
ACC_FLAT_ATR    = 2.0     # |ход за окно| ≤ N × нормальный ATR
ACC_MAX_RANGE   = 5.0     # % — шире не считаем зарядом
ACC_RVOL_MIN    = 1.3     # объём второй половины окна к норме
BASE_FROM, BASE_TO = 84, 12   # база объёма/ATR: закрытые свечи [-84:-12]
SWING_LOOKBACK  = 144     # свечей для процентиля BB и свингов
BREAK_BUFFER    = 0.001   # 0.1% за уровень
STOP_MIN, STOP_MAX = 0.8, 3.0      # % границы стопа
HOLD_BARS_MAX   = 48      # сколько свечей подтверждения держим сделку (48×5м = 4ч)
TP_MAX_R        = 4.0     # цель не дальше 4 стопов (ограничение на дальние свинги)
HALF_TS         = [0]     # середина периода — заполняется в main()
SYM_NOW         = [""]    # какая пара считается сейчас
WATCH_TTL_MULT  = 16      # заряд живёт N свечей своего ТФ

SESSIONS = {
    "all": None,
    "v82": [(10 * 60, 11 * 60 + 30), (14 * 60 + 30, 21 * 60)],
}

# ─── СЕТЬ ─────────────────────────────────────────────────────────────────────

_lock = threading.Lock()
_last = [0.0]

def api_get(path, params, tries=3, quiet=False):
    url = f"https://api.gateio.ws/api/v4/futures/usdt/{path}"
    for a in range(tries):
        with _lock:                       # не чаще 10 запросов в секунду
            wait = 0.1 - (time.time() - _last[0])
            if wait > 0:
                time.sleep(wait)
            _last[0] = time.time()
        try:
            r = requests.get(url, params=params, timeout=20)
            if r.status_code == 429:
                time.sleep(2 * (a + 1)); continue
            if r.status_code == 400:      # глубже история не отдаётся — это не сбой
                if not quiet:
                    print(f"  [400] {params.get('contract')} {params.get('interval')}: {r.text[:120]}")
                return None
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if a == tries - 1:
                if not quiet:
                    print(f"  [ERR] {path} {params.get('contract')}: {e}")
                return None
            time.sleep(1 + a)
    return None

def get_candles(sym, tf, frm, to):
    """Свечи за период. Идём страницами НАЗАД от текущего момента (to + limit) —
    Gate не отдаёт глубокую историю по from/to для мелких таймфреймов."""
    step = TF_SEC[tf]
    out, seen = [], set()
    cur_to = int(to)
    for _ in range(40):                       # максимум 40 страниц = до 80 000 свечей
        raw = api_get("candlesticks", {"contract": f"{sym}_USDT", "interval": tf,
                                       "to": cur_to, "limit": 1999}, quiet=True)
        if not raw:
            break
        page = []
        for c in raw:
            t = int(c["t"])
            if t in seen:
                continue
            seen.add(t)
            page.append((t, float(c["o"]), float(c["h"]), float(c["l"]),
                         float(c["c"]), float(c["v"])))
        if not page:
            break
        out.extend(page)
        oldest = min(p[0] for p in page)
        if oldest <= frm or len(raw) < 100:    # дошли до нужной даты или история кончилась
            break
        cur_to = oldest - step
    out = [c for c in out if c[0] >= frm]
    out.sort()
    return out

# ─── ВСПОМОГАТЕЛЬНОЕ ──────────────────────────────────────────────────────────

T, O, H, L, C, V = 0, 1, 2, 3, 4, 5

def trimmed_mean(vals):
    if not vals:
        return 0.0
    s = sorted(vals)
    cut = max(1, len(s) // 10)
    s = s[:-cut] if len(s) > 2 * cut else s
    return sum(s) / len(s) if s else 0.0

def true_ranges(cs):
    out = [cs[0][H] - cs[0][L]]
    for i in range(1, len(cs)):
        p = cs[i - 1][C]
        out.append(max(cs[i][H] - cs[i][L], abs(cs[i][H] - p), abs(p - cs[i][L])))
    return out

def bb_width(cs):
    """Ширина полос Боллинджера (20) в % от средней — для каждой свечи с 20-й."""
    out = [None] * len(cs)
    for i in range(19, len(cs)):
        w = [c[C] for c in cs[i - 19:i + 1]]
        mean = sum(w) / 20
        sd = statistics.pstdev(w)
        out[i] = (4 * sd / mean * 100) if mean else None
    return out

def swings(cs, left=2, right=2):
    hi, lo = [], []
    for i in range(left, len(cs) - right):
        if all(cs[i][H] >= cs[j][H] for j in range(i - left, i + right + 1) if j != i):
            hi.append((i, cs[i][H]))
        if all(cs[i][L] <= cs[j][L] for j in range(i - left, i + right + 1) if j != i):
            lo.append((i, cs[i][L]))
    return hi, lo

BTC = {"h1": [], "fast": [], "t0h": 0, "t0f": 0, "stepf": 300}

def load_btc(frm, now):
    """Часовой ряд BTC (для 12ч и 1ч) и ряд подтверждения (для получаса)."""
    BTC["h1"] = get_candles("BTC", "1h", frm, now)
    BTC["fast"] = get_candles("BTC", CONFIRM_TF, frm, now)
    BTC["t0h"] = BTC["h1"][0][T] if BTC["h1"] else 0
    BTC["t0f"] = BTC["fast"][0][T] if BTC["fast"] else 0
    BTC["stepf"] = TF_SEC[CONFIRM_TF]
    return bool(BTC["h1"])

def btc_state(ts):
    """Изменение BTC за 12ч, 1ч и 30 минут на момент ts (в процентах)."""
    h = BTC["h1"]
    if not h:
        return 0.0, 0.0, 0.0
    i = min(max(int((ts - BTC["t0h"]) // 3600), 0), len(h) - 1)
    c_now = h[i][C]
    c12 = h[max(i - 12, 0)][C]
    c1 = h[max(i - 1, 0)][C]
    chg12 = (c_now - c12) / c12 * 100 if c12 else 0.0
    chg1 = (c_now - c1) / c1 * 100 if c1 else 0.0
    chg30 = 0.0
    f = BTC["fast"]
    if f:
        j = int((ts - BTC["t0f"]) // BTC["stepf"])
        back = max(1, 1800 // BTC["stepf"])
        if 0 <= j < len(f) and j - back >= 0 and f[j - back][C]:
            chg30 = (f[j][C] - f[j - back][C]) / f[j - back][C] * 100
    return chg12, chg1, chg30

def btc_allows(ts, is_long, mode):
    """Пускать ли сделку при текущем состоянии BTC."""
    if mode == "off":
        return True
    chg12, chg1, chg30 = btc_state(ts)
    sgn = 1 if is_long else -1
    if mode == "trend12":
        return chg12 * sgn > 0.5
    if mode == "fast1":
        # быстрое окно: ловит разворот, пока 12ч ещё показывает старое направление
        return (chg1 * sgn > 0.15) or (chg30 * sgn > 0.25)
    if mode == "combo":
        fast_ok = (chg1 * sgn > 0.1) or (chg30 * sgn > 0.2)
        hard_against = chg12 * sgn < -1.5 and chg1 * sgn < 0      # против сильного тренда — нет
        return fast_ok and not hard_against
    return True

def in_session(ts, sess):
    if sess is None:
        return True
    mins = datetime.fromtimestamp(ts, MSK).hour * 60 + datetime.fromtimestamp(ts, MSK).minute
    return any(a <= mins < b for a, b in sess)

# ─── ПОИСК ЗАРЯДОВ ────────────────────────────────────────────────────────────

def find_charges(cs, squeeze_pctl):
    """Возвращает список зарядов: (индекс последней свечи окна, hi, lo, atr).
    Логика повторяет бота, но без OI/taker — их истории нет."""
    n = len(cs)
    if n < BASE_FROM + ACC_WINDOW + 5:
        return []
    trs = true_ranges(cs)
    bbw = bb_width(cs)
    res = []
    for i in range(BASE_FROM + ACC_WINDOW, n):
        win = cs[i - ACC_WINDOW:i]
        base = cs[i - BASE_FROM:i - BASE_TO]
        if len(base) < 20:
            continue
        baseline = trimmed_mean([c[V] for c in base])
        if baseline <= 0:
            continue
        tr_base = trs[i - BASE_FROM:i - BASE_TO]
        atr_norm = sum(tr_base) / len(tr_base) if tr_base else 0
        if atr_norm <= 0:
            continue
        hi = max(c[H] for c in win); lo = min(c[L] for c in win)
        if lo <= 0:
            continue
        rng_pct = (hi - lo) / lo * 100
        if rng_pct > ACC_MAX_RANGE:
            continue
        if abs(win[-1][C] - win[0][O]) > ACC_FLAT_ATR * atr_norm:
            continue                                     # цена не стоит
        # сжатие: ширина BB в нижних N% за SWING_LOOKBACK свечей ИЛИ узкие свечи
        hist = [x for x in bbw[max(0, i - SWING_LOOKBACK):i] if x is not None]
        cur_bb = bbw[i - 1]
        squeezed = False
        if hist and cur_bb is not None:
            pctl = sum(1 for x in hist if x < cur_bb) / len(hist) * 100
            squeezed = pctl <= squeeze_pctl
        tr_win = sum(trs[i - ACC_WINDOW:i]) / ACC_WINDOW
        if not squeezed and not (tr_win <= 0.75 * atr_norm):
            continue
        half = ACC_WINDOW // 2
        vol_half = sum(c[V] for c in win[-half:]) / half
        if vol_half / baseline < ACC_RVOL_MIN:
            continue                                     # объём не поджимается
        res.append({"i": i, "hi": hi, "lo": lo, "atr": atr_norm,
                    "ts": cs[i - 1][T] + (cs[1][T] - cs[0][T]),
                    "baseline": baseline, "rng": hi - lo})
    return res

# ─── СИМУЛЯЦИЯ СДЕЛОК ─────────────────────────────────────────────────────────

def simulate(charges, conf, cfg, tf_sec, sw_hi, sw_lo, cs_tf):
    """conf — свечи подтверждения (1m/5m). Возвращает список сделок."""
    trades = []
    if not conf:
        return trades
    conf_sec = TF_SEC[CONFIRM_TF]
    idx_by_ts = {c[T]: k for k, c in enumerate(conf)}
    ttl = WATCH_TTL_MULT * tf_sec
    base_bar = None
    for ch in charges:
        start_ts = ch["ts"]
        k = None
        for off in range(0, 6):                    # ищем первую свечу подтверждения после заряда
            k = idx_by_ts.get(start_ts + off * conf_sec)
            if k is not None:
                break
        if k is None:
            continue
        base_bar = ch["baseline"] * conf_sec / tf_sec     # норма объёма на свечу подтверждения
        hi, lo, atr = ch["hi"], ch["lo"], ch["atr"]
        end_ts = start_ts + ttl
        j = k
        while j < len(conf) and conf[j][T] <= end_ts:
            c = conf[j]
            buf = max(BREAK_BUFFER, 0.1 * atr / c[C]) if c[C] > 0 else BREAK_BUFFER
            up = dn = False
            if cfg["confirm"] == "touch":
                up = c[H] > hi * (1 + buf); dn = c[L] < lo * (1 - buf)
                entry = hi * (1 + buf) if up else lo * (1 - buf)
            else:
                up = c[C] > hi * (1 + buf); dn = c[C] < lo * (1 - buf)
                entry = c[C]
            if not up and not dn:
                j += 1; continue
            if cfg["min_bar_rvol"] and base_bar > 0 and c[V] / base_bar < cfg["min_bar_rvol"]:
                j += 1; continue
            if not in_session(c[T], SESSIONS[cfg["session"]]):
                j += 1; continue
            if not btc_allows(c[T], up, cfg.get("btc_filter", "off")):
                j += 1; continue
            side = "long" if up else "short"
            level = hi if up else lo
            # стоп за уровнем на N×ATR, в границах
            raw_stop = level - cfg["stop_atr"] * atr if up else level + cfg["stop_atr"] * atr
            dist = abs(entry - raw_stop) / entry * 100
            dist = min(max(dist, STOP_MIN), STOP_MAX)
            stop = entry * (1 - dist / 100) if up else entry * (1 + dist / 100)
            # цели
            if cfg["targets"] == "struct":
                cands = [p for _, p in (sw_hi if up else sw_lo)]
                cands = sorted([p for p in cands if p > entry * (1 + dist / 100)]) if up else \
                        sorted([p for p in cands if 0 < p < entry * (1 - dist / 100)], reverse=True)
                h = ch["rng"]
                mm1 = level + h if up else level - h
                mm2 = level + 2 * h if up else level - 2 * h
                pool = (cands + [mm1, mm2]) if up else (cands + [mm1, mm2])
                pool = sorted(pool) if up else sorted(pool, reverse=True)
                # цель не дальше TP_MAX_R стопов — иначе бэктест «высиживает» дальние свинги,
                # чего в реальной торговле не делают
                lim1 = entry * (1 + TP_MAX_R * dist / 100) if up else entry * (1 - TP_MAX_R * dist / 100)
                pool = [p for p in pool if (p <= lim1 if up else p >= lim1)]
                tp1 = pool[0] if pool else (entry * (1 + dist / 100) if up else entry * (1 - dist / 100))
                nxt = [p for p in pool if (p > tp1 * 1.002 if up else p < tp1 * 0.998)]
                tp2 = nxt[0] if nxt else (entry * (1 + 2 * dist / 100) if up else entry * (1 - 2 * dist / 100))
            else:
                r1, r2 = (1.0, 2.0) if cfg["targets"] == "1R/2R" else (1.5, 3.0)
                tp1 = entry * (1 + r1 * dist / 100) if up else entry * (1 - r1 * dist / 100)
                tp2 = entry * (1 + r2 * dist / 100) if up else entry * (1 - r2 * dist / 100)
            # Ведём сделку по свечам подтверждения.
            # При входе «по касанию» сделка открылась ВНУТРИ свечи j, поэтому её остаток
            # тоже проверяем (консервативно: если в этой же свече есть и стоп, и цель —
            # считаем стоп). Без этого бэктест не видел разворотов сразу после касания
            # и завышал результат именно у лучшей комбинации.
            first_q = j if cfg["confirm"] == "touch" else j + 1
            res, hit1 = None, False
            for q in range(first_q, min(first_q + HOLD_BARS_MAX, len(conf))):
                b = conf[q]
                stop_hit = b[L] <= stop if up else b[H] >= stop
                t1_hit   = b[H] >= tp1 if up else b[L] <= tp1
                t2_hit   = b[H] >= tp2 if up else b[L] <= tp2
                if stop_hit and not hit1:
                    res = -dist; break                      # стоп первым (консервативно)
                if t1_hit and not hit1:
                    hit1 = True
                    if t2_hit:                              # обе цели в одной свече
                        res = (abs(tp1 - entry) / entry * 50 + abs(tp2 - entry) / entry * 50); break
                    continue
                if hit1:
                    if t2_hit:
                        res = (abs(tp1 - entry) / entry * 50 + abs(tp2 - entry) / entry * 50); break
                    if stop_hit:                            # половина по TP1, вторая — в безубыток
                        res = abs(tp1 - entry) / entry * 50; break
            if res is None:
                last = conf[min(j + HOLD_BARS_MAX, len(conf) - 1)][C]
                chg = (last - entry) / entry * 100 * (1 if up else -1)
                res = (abs(tp1 - entry) / entry * 50 + chg * 0.5) if hit1 else chg
            trades.append({"side": side, "pnl": res - COST_PCT, "gross": res,
                           "hour": datetime.fromtimestamp(c[T], MSK).hour,
                           "dist": dist, "hit1": hit1, "half": 0 if c[T] < HALF_TS[0] else 1,
                           "sym": SYM_NOW[0]})
            break       # один заряд — одна сделка
    return trades

# ─── ОТЧЁТ ────────────────────────────────────────────────────────────────────

class Agg:
    """Счётчики по одной комбинации. Сделки не храним — иначе на 648 комбинациях
    и сотне пар память уходит в сотни мегабайт, и Render убивает процесс."""
    __slots__ = ("n", "wins", "total", "gp", "gl", "eq", "peak", "dd", "dists", "side", "hour", "half", "pair", "gross")

    def __init__(self):
        self.n = self.wins = 0
        self.total = self.gp = self.gl = self.eq = self.peak = self.dd = 0.0
        self.dists = []          # только для медианы стопа, режем до 2000 значений
        self.side = {}           # сторона -> [n, сумма]
        self.hour = {}           # час МСК -> [n, сумма]
        self.half = {0: [0, 0.0], 1: [0, 0.0]}   # 1-я и 2-я половина периода — проверка на подгонку
        self.pair = {}           # монета -> [n, сумма, сумма 1-й половины, сумма 2-й]
        self.gross = 0.0         # сумма до издержек — для проверки разных проскальзываний

    def add(self, t):
        p = t["pnl"]
        self.gross += t.get("gross", p)
        self.n += 1
        if p > 0:
            self.wins += 1; self.gp += p
        else:
            self.gl -= p
        self.total += p
        self.eq += p
        self.peak = max(self.peak, self.eq)
        self.dd = min(self.dd, self.eq - self.peak)
        if len(self.dists) < 2000:
            self.dists.append(t["dist"])
        s = self.side.setdefault(t["side"], [0, 0.0]); s[0] += 1; s[1] += p
        h = self.hour.setdefault(t["hour"], [0, 0.0]); h[0] += 1; h[1] += p
        q = self.half[t["half"]]; q[0] += 1; q[1] += p
        pr = self.pair.setdefault(t["sym"], [0, 0.0, 0.0, 0.0])
        pr[0] += 1; pr[1] += p; pr[2 + t["half"]] += p

    def result(self):
        if not self.n:
            return None
        return {"n": self.n, "wr": self.wins / self.n * 100, "avg": self.total / self.n,
                "total": self.total, "dd": self.dd,
                "pf": (self.gp / self.gl) if self.gl else float("inf"),
                "stop": statistics.median(self.dists) if self.dists else 0,
                "side": self.side, "hour": self.hour, "half": self.half, "pair": self.pair,
                "gross_avg": self.gross / self.n}

def send_telegram(text):
    print(text)
    if not (TELEGRAM_TOKEN and CHAT_ID):
        return
    for i in range(0, len(text), 3800):
        try:
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                          json={"chat_id": CHAT_ID, "text": text[i:i + 3800], "parse_mode": "HTML"},
                          timeout=20)
        except Exception as e:
            print(f"[TG ERR] {e}")
        time.sleep(0.5)

# ─── ГЛАВНОЕ ──────────────────────────────────────────────────────────────────

def main():
    pairs = ALL_PAIRS[:N_PAIRS]
    now = int(time.time())
    frm = now - DAYS * 86400
    HALF_TS[0] = frm + DAYS * 86400 // 2      # для проверки «первая половина / вторая»
    keys = list(GRID)
    combos = list(itertools.product(*[GRID[k] for k in keys]))
    tfs = sorted(set(GRID["charge_tf"]))
    print(f"Бэктест: {len(pairs)} пар, {DAYS} дней, подтверждение {CONFIRM_TF}, "
          f"комбинаций {len(combos)}, комиссия {FEE_PCT}%")
    send_telegram(f"🔬 <b>Бэктест запущен</b>\nПар: {len(pairs)} | дней: {DAYS} | "
                  f"подтверждение: {CONFIRM_TF} | комбинаций: {len(combos)}\n"
                  f"Пары считаются по очереди, память не копится. Это займёт 20–60 минут…")

    if not load_btc(frm, now):
        print("[BTC] не удалось загрузить — фильтры по BTC работать не будут")
    else:
        print(f"[BTC] загружено {len(BTC['h1'])} часовых свечей для фильтров")

    # Пары обрабатываем по одной и сразу освобождаем память — иначе 100 пар × месяц
    # пятиминуток не помещаются в память Render.
    acc = [Agg() for _ in range(len(combos))]
    used, skipped, t0 = 0, [], time.time()
    for idx, sym in enumerate(pairs, 1):
        d, ok, depth = {}, True, {}
        for tf in tfs + [CONFIRM_TF]:
            cs = get_candles(sym, tf, frm, now)
            if len(cs) < 200:
                ok = False; break
            d[tf] = cs
            depth[tf] = round((cs[-1][0] - cs[0][0]) / 86400, 1)
        if not ok:
            skipped.append(sym)
            print(f"  [{idx}/{len(pairs)}] {sym}: мало данных, пропуск")
            continue
        used += 1
        SYM_NOW[0] = sym
        ch_cache, sw_cache = {}, {}
        for tf in tfs:
            sw_cache[tf] = swings(d[tf])
            for sq in GRID["squeeze_pctl"]:
                ch_cache[(tf, sq)] = find_charges(d[tf], sq)
        n_tr = 0
        for ci, combo in enumerate(combos):
            cfg = dict(zip(keys, combo))
            ch = ch_cache[(cfg["charge_tf"], cfg["squeeze_pctl"])]
            if not ch:
                continue
            sw_hi, sw_lo = sw_cache[cfg["charge_tf"]]
            tr = simulate(ch, d[CONFIRM_TF], cfg, TF_SEC[cfg["charge_tf"]], sw_hi, sw_lo, d[cfg["charge_tf"]])
            for t in tr:
                acc[ci].add(t)
            n_tr += len(tr)
        d.clear(); ch_cache.clear(); sw_cache.clear(); gc.collect()
        el = int(time.time() - t0)
        left = int(el / idx * (len(pairs) - idx))
        print(f"  [{idx}/{len(pairs)}] {sym}: дней {depth} | сделок по всем комбинациям {n_tr} "
              f"| прошло {el}с, осталось ~{left}с")

    results = []
    for ci, combo in enumerate(combos):
        st = acc[ci].result()
        if st and st["n"] >= 30:          # комбинации с горсткой сделок не показываем
            results.append((dict(zip(keys, combo)), st))
    if not results:
        send_telegram("🔬 Бэктест: не набралось сделок для выводов. "
                      "Попробуй увеличить BT_DAYS или снизить пороги.")
        return
    results.sort(key=lambda r: -r[1]["avg"])

    lines = ["🔬 <b>Бэктест готов</b>",
             f"Пар: {used} (пропущено {len(skipped)}) | дней: {DAYS} | подтверждение: {CONFIRM_TF}",
             f"Издержки: комиссия {FEE_PCT}% + проскальзывание {SLIP_PCT}%×2 = {COST_PCT}% на сделку",
             f"Комбинаций с ≥30 сделками: {len(results)} из {len(combos)}\n",
             "<b>Лучшие 12 (прибыль на сделку, после комиссии):</b>"]
    for cfg, st in results[:12]:
        lines.append(f"{cfg['charge_tf']:>3} | сжатие {cfg['squeeze_pctl']:>2} | {cfg['confirm']:>5} | "
                     f"объём {cfg['min_bar_rvol']:.1f}× | стоп {cfg['stop_atr']}ATR | {cfg['targets']:>7} | "
                     f"{cfg['session']:>3} → n={st['n']:<5} winrate {st['wr']:.0f}% "
                     f"на сделку {st['avg']:+.3f}% | стоп {st['stop']:.2f}% | PF {st['pf']:.2f}")
    lines.append("\n<b>Проверка на подгонку — лучшие 8 по половинам периода:</b>")
    lines.append(f"(1-я половина: первые {DAYS//2} дн., 2-я: последние {DAYS - DAYS//2} дн.)")
    stable = []
    for cfg, st in results[:8]:
        h0, h1 = st["half"][0], st["half"][1]
        a0 = h0[1] / h0[0] if h0[0] else 0
        a1 = h1[1] / h1[0] if h1[0] else 0
        ok = "✅ обе" if a0 > 0 and a1 > 0 else ("⚠️ только 1-я" if a0 > 0 else "⚠️ только 2-я")
        if a0 > 0 and a1 > 0:
            stable.append((cfg, st, min(a0, a1)))
        lines.append(f"{cfg['charge_tf']:>3}|{cfg['confirm']:>5}|{cfg['targets']:>7}|об.{cfg['min_bar_rvol']:.1f}× → "
                     f"1-я {a0:+.3f}% (n={h0[0]}) | 2-я {a1:+.3f}% (n={h1[0]}) {ok}")
    if stable:
        stable.sort(key=lambda x: -x[2])
        cfg, st, worst = stable[0]
        lines.append(f"\n🏆 <b>Самая устойчивая</b>: {cfg['charge_tf']} | сжатие {cfg['squeeze_pctl']} | "
                     f"{cfg['confirm']} | объём {cfg['min_bar_rvol']:.1f}× | стоп {cfg['stop_atr']}ATR | "
                     f"{cfg['targets']} | {cfg['session']}\n   худшая половина {worst:+.3f}% на сделку, "
                     f"всего {st['total']:+.0f}%, PF {st['pf']:.2f}")
    else:
        lines.append("\n⚠️ Ни одна из лучших комбинаций не прибыльна в обеих половинах — "
                     "это признак подгонки, доверять результату нельзя.")

    lines.append("\n<b>Худшие 3:</b>")
    for cfg, st in results[-3:]:
        lines.append(f"{cfg['charge_tf']} | {cfg['confirm']} | {cfg['targets']} | {cfg['session']} → "
                     f"n={st['n']} на сделку {st['avg']:+.3f}%")

    def summarize(param):
        agg = {}
        for cfg, st in results:
            agg.setdefault(cfg[param], []).append(st)
        out = [f"\n<b>Влияние «{param}»</b> (среднее по остальным настройкам):"]
        for val, sts in sorted(agg.items(), key=lambda x: -sum(s["avg"] for s in x[1]) / len(x[1])):
            avg = sum(s["avg"] for s in sts) / len(sts)
            n = sum(s["n"] for s in sts) // len(sts)
            wr = sum(s["wr"] for s in sts) / len(sts)
            out.append(f"   {str(val):>8}: на сделку {avg:+.3f}% | winrate {wr:.0f}% | сделок ~{n}")
        return out
    for p in keys:
        lines += summarize(p)

    # ── сравнение фильтров BTC при прочих равных (лучшие настройки) ──
    base = {"charge_tf": "1h", "squeeze_pctl": 25, "confirm": "touch",
            "min_bar_rvol": 2.0, "stop_atr": 1.0, "targets": "struct", "session": "v82"}
    lines.append("\n<b>Фильтр BTC при прочих равных</b> (1h|25|touch|2.0×|1.0ATR|struct|v82):")
    for cfg, st in results:
        if all(cfg[k] == v for k, v in base.items()):
            h0, h1 = st["half"][0], st["half"][1]
            a0 = h0[1] / h0[0] if h0[0] else 0
            a1 = h1[1] / h1[0] if h1[0] else 0
            sh = st["side"].get("short", [0, 0]); lo = st["side"].get("long", [0, 0])
            lines.append(f"   {cfg['btc_filter']:>7}: n={st['n']:<5} на сделку {st['avg']:+.3f}% | "
                         f"WR {st['wr']:.0f}% | PF {st['pf']:.2f} | просадка {st['dd']:.0f}%")
            lines.append(f"            половины: {a0:+.3f}% / {a1:+.3f}% | "
                         f"лонг {lo[1]/lo[0]:+.3f}% ({lo[0]}) | шорт {sh[1]/sh[0]:+.3f}% ({sh[0]})"
                         if lo[0] and sh[0] else "")

    best_cfg, best_st = results[0]
    lines.append("\n<b>Лучшая комбинация — детали:</b>")
    for s, (n, tot) in best_st["side"].items():
        lines.append(f"   {s}: n={n}, на сделку {tot/n:+.3f}%, всего {tot:+.0f}%")
    lines.append("   по часам МСК: " + ", ".join(
        f"{h}ч {tot/n:+.2f}%" for h, (n, tot) in sorted(best_st["hour"].items())))
    lines.append(f"\nПросадка лучшей: {best_st['dd']:.0f}% | Profit factor {best_st['pf']:.2f}")

    # ── насколько результат держится при разном проскальзывании ──
    ref = stable[0][1] if stable else best_st
    lines.append("\n<b>Чувствительность к проскальзыванию</b> (устойчивая комбинация, "
                 f"{ref['n']} сделок, до издержек {ref['gross_avg']:+.3f}%):")
    for slip in (0.0, 0.03, 0.05, 0.10, 0.15):
        net = ref["gross_avg"] - (FEE_PCT + 2 * slip)
        mark = "✅" if net > 0.1 else ("⚠️" if net > 0 else "❌")
        lines.append(f"   {mark} проскальзывание {slip:.2f}% → на сделку {net:+.3f}% | "
                     f"за период {net * ref['n']:+.0f}%")

    # ── разбор по парам: кто тянет вверх, кто портит ──
    ref_cfg, ref_st = (stable[0][0], stable[0][1]) if stable else (best_cfg, best_st)
    pr = [(s, v[0], v[1] / v[0], v[1], v[2], v[3]) for s, v in ref_st["pair"].items() if v[0] >= 5]
    pr.sort(key=lambda x: -x[2])
    lines.append(f"\n<b>По парам</b> (комбинация {ref_cfg['charge_tf']}|{ref_cfg['confirm']}|"
                 f"{ref_cfg['targets']}, только пары с ≥5 сделками):")
    lines.append("   ЛУЧШИЕ: " + ", ".join(f"{s} {a:+.2f}%({n})" for s, n, a, _, _, _ in pr[:10]))
    lines.append("   ХУДШИЕ: " + ", ".join(f"{s} {a:+.2f}%({n})" for s, n, a, _, _, _ in pr[-10:]))
    plus_pairs = [x for x in pr if x[2] > 0]
    tot_all = sum(x[3] for x in pr)
    tot_plus = sum(x[3] for x in plus_pairs)
    lines.append(f"   прибыльных пар {len(plus_pairs)} из {len(pr)} | всего {tot_all:+.0f}% | "
                 f"только по прибыльным {tot_plus:+.0f}%")
    # какие пары прибыльны в ОБЕИХ половинах — это не подгонка
    both = [x for x in pr if x[4] > 0 and x[5] > 0]
    both.sort(key=lambda x: -x[3])
    lines.append(f"   прибыльны в обеих половинах периода ({len(both)}): " +
                 (", ".join(x[0] for x in both[:25]) if both else "нет"))
    lines.append("   ⚠️ Отбирать пары «по прибыли за прошлое» опасно: половина из них "
                 "случайна. Ориентируйся на список «в обеих половинах».")
    lines.append("\n⚠️ OI, фандинг, taker L/S и дельта в бэктесте НЕ участвуют — Gate не отдаёт их историю.")
    send_telegram("\n".join(lines))

if __name__ == "__main__":
    main()
