# -*- coding: utf-8 -*-
"""
big_candle_scanner.py — Сканер больших свечей по BTC (12:00-13:00 МСК)
=====================================================================
Логика:
  - Каждый день с 12:00 до 13:00 МСК (09:00-10:00 UTC) мониторит 5-минутные свечи BTC
  - Считает средний объём за последние 20 свечей (≈ 100 минут)
  - Если очередная свеча имеет объём > 2× среднего → это "большая свеча"
  - Отправляет TG-сообщение: "🚨 BTC BIG CANDLE: ЛОНГОВАЯ" (зелёная) или "ШОРТОВАЯ" (красная)
  - Один сигнал в день (чтобы не спамить) — или по env BIG_CANDLE_MAX_SIGNALS (по умолчанию 3)
  - Время запуска: env BIG_CANDLE_HOUR_MSK (по умолчанию 12) — час начала скана по МСК
  - Длительность: env BIG_CANDLE_DURATION_HOURS (по умолчанию 1)

Управление через Telegram:
  /bigcandle — статус сканера + сколько сигналов сегодня

Запуск: автоматически из bot.py (запускается в отдельном потоке)
"""

import os
import time
import threading
import datetime as dt
from collections import deque

try:
    import bot as B
except Exception:
    B = None

# --- Параметры ---
BIG_CANDLE_HOUR_MSK       = int(os.environ.get("BIG_CANDLE_HOUR_MSK", "12"))     # 12:00 МСК
BIG_CANDLE_DURATION_HOURS = int(os.environ.get("BIG_CANDLE_DURATION_HOURS", "1"))  # 1 час
BIG_CANDLE_MAX_SIGNALS    = int(os.environ.get("BIG_CANDLE_MAX_SIGNALS", "3"))    # макс 3 сигнала в день
BIG_CANDLE_VOL_MULT       = float(os.environ.get("BIG_CANDLE_VOL_MULT", "2.0"))   # объём > 2× среднего
BIG_CANDLE_LOOKBACK       = int(os.environ.get("BIG_CANDLE_LOOKBACK", "20"))      # 20 свечей для среднего
BIG_CANDLE_INTERVAL       = "5m"                                                  # 5-минутные свечи

# Москва = UTC+3
MSK_OFFSET_HOURS = 3

# --- Состояние ---
_scanner_thread = None
_state = {
    "signals_today": 0,
    "signals_today_date": "",
    "last_signal_ts": 0,
    "last_candle_ts": 0,
    "last_candle_vol": 0,
    "avg_vol": 0,
    "scanning": False,
}


def _today_msk_str():
    return dt.datetime.now(dt.timezone.utc).astimezone(
        dt.timezone(dt.timedelta(hours=MSK_OFFSET_HOURS))
    ).strftime("%Y-%m-%d")


def _now_msk():
    return dt.datetime.now(dt.timezone.utc).astimezone(
        dt.timezone(dt.timedelta(hours=MSK_OFFSET_HOURS))
    )


def _is_scan_window():
    """Сейчас окно скана? (12:00-13:00 МСК по умолчанию)"""
    now_msk = _now_msk()
    start_hour = BIG_CANDLE_HOUR_MSK
    end_hour = start_hour + BIG_CANDLE_DURATION_HOURS
    return start_hour <= now_msk.hour < end_hour


def _reset_counters():
    today = _today_msk_str()
    if _state.get("signals_today_date") != today:
        _state["signals_today"] = 0
        _state["signals_today_date"] = today
        _state["last_signal_ts"] = 0


def _fetch_candles():
    """Получает последние BIG_CANDLE_LOOKBACK 5-минутных свечей BTC_USDT с Gate.io"""
    if not B:
        return None
    try:
        # Используем candles endpoint: gate.io futures API
        # https://api.gateio.ws/api/v4/futures/usdt/candles?contract=BTC_USDT&interval=5m&limit=30
        # bot.py api_get может быть обёрткой над requests
        import requests
        url = "https://api.gateio.ws/api/v4/futures/usdt/candles"
        params = {"contract": "BTC_USDT", "interval": BIG_CANDLE_INTERVAL, "limit": str(BIG_CANDLE_LOOKBACK + 5)}
        r = requests.get(url, params=params, timeout=10)
        if not r.ok:
            print(f"[big_candle] candles fetch error: {r.status_code}")
            return None
        candles = r.json()
        if not isinstance(candles, list) or len(candles) < BIG_CANDLE_LOOKBACK:
            return None
        # Формат Gate.io: [[t, volume, close, high, low, open, name], ...]
        # t = timestamp секунд
        # volume в контрактах → нужно умножить на quanto_multiplier × price
        # Но для relative сравнения (большая/средняя) — можно использовать volume "как есть"
        # (он пропорционален USD объёму)
        result = []
        for c in candles:
            if len(c) < 7:
                continue
            try:
                t = int(c[0])
                vol = float(c[1])
                close = float(c[2])
                high = float(c[3])
                low = float(c[4])
                op = float(c[5])
                result.append({
                    "ts": t,
                    "open": op,
                    "close": close,
                    "high": high,
                    "low": low,
                    "vol": vol,
                })
            except (ValueError, TypeError):
                continue
        return result
    except Exception as e:
        print(f"[big_candle] fetch_candles error: {e}")
        return None


def _check_big_candle():
    """Проверяет последнюю закрытую свечу — большая ли она?"""
    candles = _fetch_candles()
    if not candles or len(candles) < BIG_CANDLE_LOOKBACK:
        return None

    # Последняя свеча в списке — самая свежая, но она ещё может быть "текущая" (не закрытая).
    # Берём предпоследнюю — она точно закрыта.
    last_closed = candles[-2]
    # Все свечи кроме last_closed — для расчёта среднего
    history = candles[:-1]
    if len(history) < BIG_CANDLE_LOOKBACK:
        history = history[-(BIG_CANDLE_LOOKBACK):]
    else:
        history = history[-(BIG_CANDLE_LOOKBACK):]

    vols = [c["vol"] for c in history]
    avg_vol = sum(vols) / len(vols) if vols else 0

    if avg_vol <= 0:
        return None

    if last_closed["vol"] < avg_vol * BIG_CANDLE_VOL_MULT:
        return None  # не большая свеча

    # Это большая свеча!
    is_long = last_closed["close"] >= last_closed["open"]
    return {
        "ts": last_closed["ts"],
        "vol": last_closed["vol"],
        "avg_vol": avg_vol,
        "ratio": last_closed["vol"] / avg_vol,
        "open": last_closed["open"],
        "close": last_closed["close"],
        "high": last_closed["high"],
        "low": last_closed["low"],
        "is_long": is_long,
    }


def _scanner_loop():
    """Главный цикл сканера — работает постоянно, но активен только в окне 12-13 МСК"""
    print(f"[big_candle] Сканер запущен: окно {BIG_CANDLE_HOUR_MSK}:00-{BIG_CANDLE_HOUR_MSK + BIG_CANDLE_DURATION_HOURS}:00 МСК")
    while True:
        try:
            _reset_counters()

            if not _is_scan_window():
                _state["scanning"] = False
                time.sleep(60)
                continue

            _state["scanning"] = True

            # В окне — проверяем каждые 30 секунд (свечи 5m, так что чаще нет смысла)
            if _state.get("signals_today", 0) >= BIG_CANDLE_MAX_SIGNALS:
                # Лимит сигналов на день исчерпан
                time.sleep(60)
                continue

            # Не отправляем сигнал по одной и той же свече дважды
            # Минимальный интервал между сигналами — 5 минут (длина свечи)
            now_ts = time.time()
            if now_ts - _state.get("last_signal_ts", 0) < 300:
                time.sleep(30)
                continue

            big = _check_big_candle()
            if big:
                # Проверяем что эта свеча ещё не обработана
                if big["ts"] != _state.get("last_candle_ts"):
                    _state["last_candle_ts"] = big["ts"]
                    _state["last_candle_vol"] = big["vol"]
                    _state["avg_vol"] = big["avg_vol"]
                    _state["last_signal_ts"] = now_ts
                    _state["signals_today"] += 1

                    direction = "🟢 ЛОНГОВАЯ" if big["is_long"] else "🔴 ШОРТОВАЯ"
                    candle_time = dt.datetime.fromtimestamp(big["ts"], tz=dt.timezone.utc).astimezone(
                        dt.timezone(dt.timedelta(hours=MSK_OFFSET_HOURS))
                    ).strftime("%H:%M МСК")

                    if B:
                        B.send_telegram(
                            f"🚨 <b>BIG CANDLE BTC</b> {direction}\n"
                            f"Свеча {candle_time} (5m)\n"
                            f"Объём: {big['vol']:.0f} (×{big['ratio']:.2f} от среднего)\n"
                            f"Средний объём: {big['avg_vol']:.0f}\n"
                            f"Open: ${big['open']:.2f} → Close: ${big['close']:.2f}\n"
                            f"High: ${big['high']:.2f} | Low: ${big['low']:.2f}\n"
                            f"Сигнал {_state['signals_today']}/{BIG_CANDLE_MAX_SIGNALS} за сегодня"
                        )
                    print(f"[big_candle] BIG CANDLE {direction} @ {candle_time}, vol ×{big['ratio']:.2f}")

            time.sleep(30)
        except Exception as e:
            print(f"[big_candle] scanner loop error: {e}")
            time.sleep(60)


def status_report():
    """Статус сканера для /bigcandle команды"""
    lines = ["", "━━━ Big Candle Scanner ━━━"]
    lines.append(f"Окно: {BIG_CANDLE_HOUR_MSK}:00-{BIG_CANDLE_HOUR_MSK + BIG_CANDLE_DURATION_HOURS}:00 МСК")
    lines.append(f"Активен сейчас: {'✅ ДА' if _state.get('scanning') else '🔴 НЕТ'}")
    lines.append(f"Сигналов сегодня: {_state.get('signals_today', 0)}/{BIG_CANDLE_MAX_SIGNALS}")
    lines.append(f"Порог объёма: ×{BIG_CANDLE_VOL_MULT} от среднего за {BIG_CANDLE_LOOKBACK} свечей")
    if _state.get("last_candle_vol", 0) > 0:
        lines.append(f"Последняя большая свеча: vol={_state['last_candle_vol']:.0f} (avg={_state['avg_vol']:.0f})")
    return "\n".join(lines)


def start():
    """Запуск сканера в отдельном потоке"""
    global _scanner_thread
    if _scanner_thread is not None:
        return
    _scanner_thread = threading.Thread(target=_scanner_loop, daemon=True, name="big-candle-scanner")
    _scanner_thread.start()
    print(f"[big_candle] Поток запущен")
    if B:
        B.send_telegram(
            f"📊 <b>Big Candle Scanner запущен</b>\n"
            f"Окно: {BIG_CANDLE_HOUR_MSK}:00-{BIG_CANDLE_HOUR_MSK + BIG_CANDLE_DURATION_HOURS}:00 МСК\n"
            f"Порог: ×{BIG_CANDLE_VOL_MULT} от среднего за {BIG_CANDLE_LOOKBACK} свечей\n"
            f"Макс сигналов в день: {BIG_CANDLE_MAX_SIGNALS}\n"
            f"Команда: /bigcandle — статус"
        )


if __name__ == "__main__":
    print("big_candle_scanner.py — standalone test")
    start()
    while True:
        time.sleep(60)
