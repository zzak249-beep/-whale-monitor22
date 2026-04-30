# -*- coding: utf-8 -*-
"""scanner.py -- Concurrent OHLCV fetcher for the symbol universe."""
from __future__ import annotations
import asyncio
from loguru import logger
from exchange.client import fetch_ohlcv


async def fetch_universe(symbols: list[str], timeframe: str, max_concurrent: int = 10) -> dict[str, dict]:
    """Fetch OHLCV for every symbol, returns {symbol: ohlcv_dict}."""
    results: dict[str, dict] = {}
    sem = asyncio.Semaphore(max_concurrent)

    async def _one(sym: str) -> None:
        async with sem:
            data = await fetch_ohlcv(sym, timeframe, limit=300)
            if data is not None:
                results[sym] = data

    tasks = [asyncio.create_task(_one(s)) for s in symbols]
    await asyncio.gather(*tasks, return_exceptions=True)
    logger.debug(f"Scanned {len(results)}/{len(symbols)} symbols OK")
    return results
