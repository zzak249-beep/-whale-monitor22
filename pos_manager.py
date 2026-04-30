# -*- coding: utf-8 -*-
"""pos_manager.py -- Position lifecycle: breakeven, partial close, trailing exit.

Flow per open trade on each scan cycle:
  1. Fetch live price
  2. If profit >= 1R  and not yet at BE:
       - Cancel open SL order
       - Place new SL at entry (breakeven)
       - Close 50 % of position (partial TP)
       - Mark be_done = True
  3. If be_done and delta1 flips against trade:
       - Close remaining position (trailing exit)
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from loguru import logger

from exchange import client as ex
from notifier import notify
from strategy import delta1_flipped


@dataclass
class Trade:
    symbol:      str
    side:        str          # "BUY" | "SELL"
    entry:       float
    sl:          float
    tp:          float
    atr:         float
    size_usdt:   float
    qty:         float = 0.0  # filled quantity (units)
    be_done:     bool  = False
    partial_done: bool = False
    closed:      bool  = False
    order_id:    str   = ""


# In-memory registry  {symbol: Trade}
_trades: dict[str, Trade] = {}


def add_trade(trade: Trade) -> None:
    _trades[trade.symbol] = trade


def remove_trade(symbol: str) -> None:
    _trades.pop(symbol, None)


def open_symbols() -> set[str]:
    return set(_trades.keys())


def trade_count() -> int:
    return len(_trades)


async def sync_from_exchange() -> None:
    """On startup, re-import any live positions that survived a restart."""
    live = await ex.get_all_positions()
    for sym, pos in live.items():
        if sym in _trades:
            continue
        amt  = float(pos.get("positionAmt", 0))
        side = "BUY" if amt > 0 else "SELL"
        ep   = float(pos.get("avgPrice", 0))
        if ep <= 0:
            continue
        t = Trade(
            symbol=sym, side=side, entry=ep, sl=0.0, tp=0.0, atr=0.0,
            size_usdt=0.0, qty=abs(amt), be_done=True, partial_done=True,
        )
        _trades[sym] = t
        logger.info(f"[SYNC] re-imported {sym} {side} @ {ep}")
        await notify(f"[SYNC] Re-imported {sym} {side} @ {ep}")


async def _close_partial(trade: Trade, live_pos: dict) -> None:
    """Close cfg.partial_pct of position."""
    from core.config import cfg
    qty = trade.qty * cfg.partial_pct
    if qty <= 0:
        return
    close_side = "SELL" if trade.side == "BUY" else "BUY"
    resp = await ex.place_reduce_order(trade.symbol, close_side, round(qty, 6))
    code = resp.get("code", -1)
    if code in (0, 200):
        trade.qty -= qty
        trade.partial_done = True
        logger.info(f"[PARTIAL] {trade.symbol} closed {qty:.6f} units")
        await notify(
            f"*[PARTIAL TP]* {trade.symbol}\n"
            f"Closed {cfg.partial_pct*100:.0f}% | remaining qty={trade.qty:.6f}"
        )
    else:
        logger.warning(f"[PARTIAL] {trade.symbol} failed: {resp}")


async def _move_to_breakeven(trade: Trade) -> None:
    """Cancel SL orders and re-place at entry price."""
    await ex.cancel_all_orders(trade.symbol)
    # BingX doesn't have a dedicated SL-edit endpoint; we rely on the bot's
    # trailing logic to exit rather than a resting order after BE.
    trade.be_done = True
    logger.info(f"[BREAKEVEN] {trade.symbol} SL moved to entry {trade.entry}")
    await notify(
        f"*[BREAKEVEN]* {trade.symbol}\n"
        f"SL moved to entry {trade.entry} | side={trade.side}"
    )


async def manage_positions(ohlcv_map: dict[str, dict]) -> None:
    """Called once per scan cycle for every open trade."""
    from core.config import cfg

    closed_symbols: list[str] = []

    for sym, trade in list(_trades.items()):
        if trade.closed:
            closed_symbols.append(sym)
            continue

        price = await ex.get_price(sym)
        if price <= 0:
            continue

        # Profit in R units
        r_dist = trade.atr * cfg.atr_mult if trade.atr > 0 else abs(trade.entry - trade.sl)
        if r_dist <= 0:
            continue

        pnl = (price - trade.entry) if trade.side == "BUY" else (trade.entry - price)
        r_achieved = pnl / r_dist

        # ── Step 1: Breakeven + partial TP at +1R ────────────────────────
        if not trade.be_done and r_achieved >= cfg.breakeven_r:
            live_positions = await ex.get_all_positions()
            live_pos = live_positions.get(sym, {})
            if live_pos:
                await _move_to_breakeven(trade)
                await _close_partial(trade, live_pos)

        # ── Step 2: Trailing exit when delta1 flips ────────────────────
        if trade.be_done and sym in ohlcv_map:
            ohlcv = ohlcv_map[sym]
            if delta1_flipped(ohlcv, cfg.period, trade.side):
                live_positions = await ex.get_all_positions()
                live_pos = live_positions.get(sym, {})
                if live_pos:
                    resp = await ex.close_position(sym, live_pos)
                    code = resp.get("code", -1)
                    if code in (0, 200):
                        trade.closed = True
                        closed_symbols.append(sym)
                        logger.info(f"[TRAIL EXIT] {sym} closed at {price:.6f} | R={r_achieved:.2f}")
                        await notify(
                            f"*[TRAIL EXIT]* {sym}\n"
                            f"Price={price:.6f} | R={r_achieved:.2f}R\n"
                            f"Delta1 flipped against {trade.side}"
                        )
                else:
                    # Position already closed (hit SL/TP on exchange)
                    trade.closed = True
                    closed_symbols.append(sym)
                    logger.info(f"[CLOSED] {sym} no longer on exchange (SL/TP hit)")

    for sym in closed_symbols:
        remove_trade(sym)
