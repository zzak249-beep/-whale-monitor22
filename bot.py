# -*- coding: utf-8 -*-
"""bot.py -- Three Step Future-Trend Bot  v1.0
Reads Three Step volume delta signals (BigBeluga / Pine Script port),
manages breakeven + partial TP + delta-trail exit.
Deploys on Railway via Procfile.
"""
from __future__ import annotations
import asyncio
import os
import sys
from datetime import datetime

from loguru import logger
from aiohttp import web

from core.config import cfg
from exchange import client as ex
from scanner import fetch_universe
from strategy import get_signal
from pos_manager import (
    Trade, add_trade, open_symbols, trade_count,
    manage_positions, sync_from_exchange,
)
from notifier import notify

# ── Logging setup ─────────────────────────────────────────────────────────────
logger.remove()
logger.add(sys.stdout, level="INFO",
           format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | {message}")


# ── Health-check server (required by Railway) ─────────────────────────────────
async def _health(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "trades": trade_count()})


async def start_health_server() -> None:
    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", cfg.health_port)
    await site.start()
    logger.info(f"Health server listening on :{cfg.health_port}")


# ── Entry execution ───────────────────────────────────────────────────────────
async def enter_trade(sig, ohlcv: dict) -> None:
    """Set leverage, place order, record trade."""
    from core.config import cfg

    # Guard: already in this symbol
    if sig.symbol in open_symbols():
        return

    # Guard: max positions
    if trade_count() >= cfg.max_positions:
        logger.debug(f"[SKIP] max positions reached ({cfg.max_positions})")
        return

    # Balance check
    bal = await ex.get_balance()
    if bal < cfg.trade_usdt * 1.1:
        logger.warning(f"[SKIP] balance too low: {bal:.2f} USDT")
        await notify(f"[WARN] Balance too low: {bal:.2f} USDT. Skipping {sig.symbol}.")
        return

    # Set leverage
    await ex.set_leverage(sig.symbol, cfg.leverage)
    await asyncio.sleep(0.2)

    # Place order
    resp = await ex.place_market_order(
        symbol=sig.symbol, side=sig.side,
        size_usdt=cfg.trade_usdt,
        sl=sig.sl, tp=sig.tp,
    )
    code = resp.get("code", -1)
    if code not in (0, 200, None):
        logger.warning(f"[ORDER FAIL] {sig.symbol} code={code} {resp.get('msg','')}")
        return

    # Estimate qty from filled data
    order_data = resp.get("data", {}).get("order", {})
    qty = float(order_data.get("executedQty", 0) or order_data.get("origQty", 0))
    if qty <= 0:
        # Fallback estimate
        qty = (cfg.trade_usdt * cfg.leverage) / sig.price

    trade = Trade(
        symbol=sig.symbol, side=sig.side,
        entry=sig.price, sl=sig.sl, tp=sig.tp,
        atr=sig.atr, size_usdt=cfg.trade_usdt, qty=qty,
        order_id=str(order_data.get("orderId", "")),
    )
    add_trade(trade)

    logger.success(
        f"[ENTRY] {sig.symbol} {sig.side} @ {sig.price:.6f} "
        f"SL={sig.sl:.6f} TP={sig.tp:.6f} qty={qty:.6f}"
    )
    await notify(
        f"*[ENTRY]* {sig.symbol} `{sig.side}`\n"
        f"Price: `{sig.price:.6f}`\n"
        f"SL: `{sig.sl:.6f}`\n"
        f"TP: `{sig.tp:.6f}`\n"
        f"Size: {cfg.trade_usdt} USDT x{cfg.leverage}\n"
        f"Delta1: {sig.delta1:+.0f}  Delta2: {sig.delta2:+.0f}"
    )


# ── Main scan loop ────────────────────────────────────────────────────────────
async def scan_cycle() -> None:
    symbols = cfg.symbols
    logger.info(f"Scanning {len(symbols)} symbols on {cfg.timeframe} | "
                f"period={cfg.period} atr_mult={cfg.atr_mult} rr={cfg.rr}")

    ohlcv_map = await fetch_universe(symbols, cfg.timeframe, cfg.max_concurrent)

    # ── Manage open positions first ──
    await manage_positions(ohlcv_map)

    # ── Look for new signals ──
    for sym, ohlcv in ohlcv_map.items():
        if sym in open_symbols():
            continue
        sig = get_signal(
            ohlcv, sym,
            period=cfg.period,
            atr_period=cfg.atr_period,
            atr_mult=cfg.atr_mult,
            rr=cfg.rr,
        )
        if sig:
            logger.info(f"[SIGNAL] {sym} {sig.side} | D1={sig.delta1:+.0f} D2={sig.delta2:+.0f}")
            await enter_trade(sig, ohlcv)


async def main_loop() -> None:
    await start_health_server()

    logger.info("=" * 60)
    logger.info("  THREE STEP FUTURE-TREND BOT  v1.0")
    logger.info(f"  Symbols : {cfg.symbols}")
    logger.info(f"  TF      : {cfg.timeframe}")
    logger.info(f"  Trade   : {cfg.trade_usdt} USDT x{cfg.leverage}")
    logger.info(f"  Period  : {cfg.period}  ATR mult={cfg.atr_mult}  RR={cfg.rr}")
    logger.info("=" * 60)

    await notify(
        "*Three Step Future-Trend Bot Started* [v1.0]\n"
        f"Symbols: {', '.join(cfg.symbols)}\n"
        f"TF: {cfg.timeframe} | Trade: {cfg.trade_usdt} USDT x{cfg.leverage}\n"
        f"Period: {cfg.period} | ATR: {cfg.atr_mult}x | RR: {cfg.rr}"
    )

    # Re-import surviving positions from exchange
    await sync_from_exchange()

    while True:
        try:
            t0 = asyncio.get_event_loop().time()
            await scan_cycle()
            elapsed = asyncio.get_event_loop().time() - t0
            sleep_for = max(0, cfg.scan_interval - elapsed)
            logger.info(f"Cycle done in {elapsed:.1f}s | sleeping {sleep_for:.0f}s")
            await asyncio.sleep(sleep_for)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[LOOP ERROR] {e}")
            await notify(f"[BOT ERROR] {e}")
            await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(main_loop())
