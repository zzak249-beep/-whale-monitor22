# -*- coding: utf-8 -*-
"""core/config.py -- Central configuration from environment variables."""
import os
from dataclasses import dataclass, field


@dataclass
class Config:
    # ── BingX ──────────────────────────────────────────────────────────────
    bingx_api_key:    str = ""
    bingx_secret_key: str = ""

    # ── Telegram ───────────────────────────────────────────────────────────
    telegram_token:   str = ""
    telegram_chat_id: str = ""

    # ── Trade sizing ───────────────────────────────────────────────────────
    trade_usdt:   float = 5.0
    leverage:     int   = 5

    # ── Strategy params ────────────────────────────────────────────────────
    period:       int   = 25    # Three-Step period (same as Pine default)
    atr_period:   int   = 14
    atr_mult:     float = 2.0   # SL distance = ATR * mult  (1R)
    rr:           float = 2.0   # TP = entry +/- 1R * rr
    timeframe:    str   = "1h"  # candle interval for signals

    # ── Position management ─────────────────────────────────────────────────
    max_positions:  int = 3     # max simultaneous open trades
    breakeven_r:    float = 1.0 # move SL to BE when profit >= breakeven_r * R
    partial_r:      float = 1.0 # close 50 % when profit >= partial_r * R
    partial_pct:    float = 0.5 # fraction to close at partial TP

    # ── Scanning ───────────────────────────────────────────────────────────
    symbols_raw:    str = "BTC-USDT,ETH-USDT,SOL-USDT,BNB-USDT,XRP-USDT,DOGE-USDT,ADA-USDT,AVAX-USDT"
    scan_interval:  int = 300   # seconds between scan cycles
    max_concurrent: int = 10    # parallel fetches

    # ── HTTP ───────────────────────────────────────────────────────────────
    http_timeout:   int = 15

    # ── Health-check (Railway) ─────────────────────────────────────────────
    health_port:    int = 8080

    @property
    def symbols(self) -> list[str]:
        return [s.strip() for s in self.symbols_raw.split(",") if s.strip()]

    def __post_init__(self) -> None:
        self.bingx_api_key    = os.getenv("BINGX_API_KEY",    self.bingx_api_key)
        self.bingx_secret_key = os.getenv("BINGX_SECRET_KEY", self.bingx_secret_key)
        self.telegram_token   = os.getenv("TELEGRAM_TOKEN",   self.telegram_token)
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", self.telegram_chat_id)
        self.trade_usdt       = float(os.getenv("TRADE_USDT",  str(self.trade_usdt)))
        self.leverage         = int(os.getenv("LEVERAGE",      str(self.leverage)))
        self.period           = int(os.getenv("PERIOD",        str(self.period)))
        self.atr_period       = int(os.getenv("ATR_PERIOD",    str(self.atr_period)))
        self.atr_mult         = float(os.getenv("ATR_MULT",    str(self.atr_mult)))
        self.rr               = float(os.getenv("RR",          str(self.rr)))
        self.timeframe        = os.getenv("TIMEFRAME",         self.timeframe)
        self.max_positions    = int(os.getenv("MAX_POSITIONS", str(self.max_positions)))
        self.scan_interval    = int(os.getenv("SCAN_INTERVAL", str(self.scan_interval)))
        self.symbols_raw      = os.getenv("SYMBOLS",           self.symbols_raw)
        self.health_port      = int(os.getenv("PORT",          str(self.health_port)))


cfg = Config()
