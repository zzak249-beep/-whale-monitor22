# -*- coding: utf-8 -*-
"""strategy.py -- Three Step Future-Trend signal engine.

Faithfully replicates the BigBeluga Pine Script logic:
  delta_vol = close > open ? +volume : -volume
  delta1    = sum(delta_vol, period)           [most recent period]
  delta2    = sum(delta_vol, period*2) - delta1
  delta3    = sum(delta_vol, period*3) - delta1 - delta2

Signal rules:
  LONG  when delta1 crosses ABOVE 0  AND delta2 >= 0 (confirmation)
  SHORT when delta1 crosses BELOW 0  AND delta2 <= 0 (confirmation)

SL = ATR * atr_mult from entry
TP = entry +/- atr * atr_mult * rr
"""
from __future__ import annotations

import numpy as np
from dataclasses import dataclass


@dataclass
class Signal:
    symbol:    str
    side:      str   # "BUY" | "SELL"
    price:     float
    sl:        float
    tp:        float
    atr:       float
    delta1:    float
    delta2:    float
    delta3:    float


def _rolling_sum(arr: np.ndarray, n: int) -> np.ndarray:
    """Efficient rolling sum using cumsum."""
    cs  = np.cumsum(arr)
    out = cs.copy()
    out[n:] = cs[n:] - cs[:-n]
    out[:n] = cs[:n]  # partial sum at the beginning (matches Pine's math.sum behaviour)
    return out


def _atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int) -> np.ndarray:
    prev_close = np.roll(closes, 1)
    prev_close[0] = closes[0]
    tr = np.maximum(highs - lows,
         np.maximum(np.abs(highs - prev_close),
                    np.abs(lows  - prev_close)))
    # Wilder smoothing
    atr_arr = np.zeros_like(tr)
    atr_arr[period - 1] = tr[:period].mean()
    for i in range(period, len(tr)):
        atr_arr[i] = (atr_arr[i - 1] * (period - 1) + tr[i]) / period
    return atr_arr


def compute_deltas(
    opens: np.ndarray, closes: np.ndarray, volumes: np.ndarray, period: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    delta_vol = np.where(closes > opens, volumes, -volumes)

    d_full1 = _rolling_sum(delta_vol, period)
    d_full2 = _rolling_sum(delta_vol, period * 2)
    d_full3 = _rolling_sum(delta_vol, period * 3)

    delta1 = d_full1
    delta2 = d_full2 - delta1
    delta3 = d_full3 - delta1 - delta2
    return delta1, delta2, delta3


def get_signal(
    ohlcv:      dict,
    symbol:     str,
    period:     int   = 25,
    atr_period: int   = 14,
    atr_mult:   float = 2.0,
    rr:         float = 2.0,
) -> Signal | None:
    """Return a Signal on a confirmed delta crossover, else None."""
    opens   = ohlcv["open"]
    highs   = ohlcv["high"]
    lows    = ohlcv["low"]
    closes  = ohlcv["close"]
    volumes = ohlcv["volume"]

    # Need at least 3*period + atr_period bars
    min_bars = period * 3 + atr_period + 5
    if len(closes) < min_bars:
        return None

    delta1, delta2, _ = compute_deltas(opens, closes, volumes, period)
    atr_arr = _atr(highs, lows, closes, atr_period)

    # Use last CLOSED candle (index -2) to avoid repainting
    d1_prev  = delta1[-3]   # 2 bars ago
    d1_curr  = delta1[-2]   # last closed bar
    d2_curr  = delta2[-2]
    d3_curr  = _   # unused in entry logic
    atr_val  = atr_arr[-2]
    price    = closes[-2]   # entry on close of signal bar

    if atr_val <= 0 or price <= 0:
        return None

    sl_dist = atr_val * atr_mult
    tp_dist = sl_dist * rr

    # LONG cross
    if d1_prev <= 0 < d1_curr and d2_curr >= 0:
        return Signal(
            symbol=symbol, side="BUY", price=price,
            sl=round(price - sl_dist, 8),
            tp=round(price + tp_dist, 8),
            atr=atr_val, delta1=d1_curr, delta2=d2_curr, delta3=0,
        )

    # SHORT cross
    if d1_prev >= 0 > d1_curr and d2_curr <= 0:
        return Signal(
            symbol=symbol, side="SELL", price=price,
            sl=round(price + sl_dist, 8),
            tp=round(price - tp_dist, 8),
            atr=atr_val, delta1=d1_curr, delta2=d2_curr, delta3=0,
        )

    return None


def delta1_flipped(ohlcv: dict, period: int, trade_side: str) -> bool:
    """Return True when delta1 has just flipped against the open trade (trailing exit)."""
    opens   = ohlcv["open"]
    closes  = ohlcv["close"]
    volumes = ohlcv["volume"]
    if len(closes) < period * 3 + 5:
        return False
    delta1, _, _ = compute_deltas(opens, closes, volumes, period)
    curr = delta1[-2]   # last closed bar
    if trade_side == "BUY"  and curr < 0:
        return True
    if trade_side == "SELL" and curr > 0:
        return True
    return False
