from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from uuid import UUID

from .models import Decision, ExecutionResult, MarketPacket


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS cycles (
    cycle_id TEXT PRIMARY KEY,
    packet_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    trigger TEXT NOT NULL,
    status TEXT NOT NULL,
    packet_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    symbol TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    payload_json TEXT NOT NULL,
    executed INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(cycle_id) REFERENCES cycles(cycle_id)
);
CREATE TABLE IF NOT EXISTS executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id TEXT NOT NULL UNIQUE,
    cycle_id TEXT NOT NULL,
    status TEXT NOT NULL,
    broker_ticket TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def save_cycle(self, packet: MarketPacket, packet_hash: str, status: str = "CREATED") -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO cycles
                (cycle_id, packet_hash, created_at, trigger, status, packet_json)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    str(packet.cycle_id),
                    packet_hash,
                    packet.packet_created_at.isoformat(),
                    packet.trigger,
                    status,
                    packet.model_dump_json(),
                ),
            )

    def update_cycle_status(self, cycle_id: UUID, status: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE cycles SET status=? WHERE cycle_id=?", (status, str(cycle_id)))

    def save_decision(self, decision: Decision) -> bool:
        try:
            with self.connect() as conn:
                conn.execute(
                    """INSERT INTO decisions
                    (decision_id, cycle_id, decision, symbol, payload_json)
                    VALUES (?, ?, ?, ?, ?)""",
                    (
                        str(decision.decision_id),
                        str(decision.cycle_id),
                        decision.decision.value,
                        decision.symbol,
                        decision.model_dump_json(),
                    ),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def was_executed(self, decision_id: UUID) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM executions WHERE decision_id=? LIMIT 1", (str(decision_id),)
            ).fetchone()
            return row is not None

    def save_execution(self, result: ExecutionResult) -> bool:
        try:
            with self.connect() as conn:
                conn.execute(
                    """INSERT INTO executions
                    (decision_id, cycle_id, status, broker_ticket, payload_json)
                    VALUES (?, ?, ?, ?, ?)""",
                    (
                        str(result.decision_id),
                        str(result.cycle_id),
                        result.status,
                        result.broker_ticket,
                        result.model_dump_json(),
                    ),
                )
                conn.execute(
                    "UPDATE decisions SET executed=1 WHERE decision_id=?",
                    (str(result.decision_id),),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def set_state(self, key: str, value) -> None:
        encoded = json.dumps(value)
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO state(key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP""",
                (key, encoded),
            )

    def get_state(self, key: str, default=None):
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            return default if row is None else json.loads(row["value"])
