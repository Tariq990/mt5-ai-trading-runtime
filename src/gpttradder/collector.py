from __future__ import annotations

import asyncio
from datetime import timezone

from .broker.base import Broker
from .config import Settings
from .hashing import market_packet_hash
from .models import MarketPacket


TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]


class MarketCollector:
    def __init__(self, broker: Broker, settings: Settings):
        self.broker = broker
        self.settings = settings

    async def collect(self, trigger: str = "POLL", reason: str | None = None) -> MarketPacket:
        account_task = self.broker.get_account_state()
        position_task = self.broker.get_positions()
        orders_task = self.broker.get_pending_orders()
        trades_task = self.broker.get_recent_trades()
        market_tasks = {
            symbol: asyncio.create_task(
                self.broker.get_market_data(symbol, TIMEFRAMES, self.settings.candle_limit)
            )
            for symbol in self.settings.symbols
        }
        account, positions, pending_orders, recent_trades = await asyncio.gather(
            account_task, position_task, orders_task, trades_task
        )
        symbol_data = {symbol: await task for symbol, task in market_tasks.items()}
        broker_ts = max(item.quote.ts for item in symbol_data.values())
        packet = MarketPacket(
            broker_timestamp=broker_ts.astimezone(timezone.utc),
            symbols=symbol_data,
            account=account,
            positions=positions,
            pending_orders=pending_orders,
            recent_trades=recent_trades,
            trigger=trigger,
            trigger_reason=reason,
        )
        packet.packet_hash = market_packet_hash(packet)
        return packet
