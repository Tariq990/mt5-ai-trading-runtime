from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator
from uuid import UUID
from zoneinfo import ZoneInfo

from .models import AccountState, Decision, ExecutionResult, MarketPacket


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
CREATE TABLE IF NOT EXISTS runtime_locks (
    name TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_reports (
    report_date TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    report_text TEXT NOT NULL,
    sent INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS bridge_sends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT NOT NULL,
    client_message_id TEXT NOT NULL,
    message_sha256 TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    dispatch_state TEXT,
    error_code TEXT,
    outcome TEXT,
    response_found INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS review_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    response_json TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sent_at TEXT
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn) -> None:
        """Idempotent ALTER TABLE additions for databases created before a schema change."""
        existing = {row[1] for row in conn.execute("PRAGMA table_info(bridge_sends)").fetchall()}
        additions = {
            "dispatch_state": "TEXT",
            "error_code": "TEXT",
            "outcome": "TEXT",
            "response_found": "INTEGER",
        }
        for column, ddl in additions.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE bridge_sends ADD COLUMN {column} {ddl}")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
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

    def record_bridge_send(
        self,
        cycle_id,
        client_message_id: str,
        message_sha256: str,
        attempt: int,
        dispatch_state: str | None = None,
        error_code: str | None = None,
        outcome: str | None = None,
        response_found: bool | None = None,
    ) -> None:
        """Persist one ChatGPT bridge send attempt (idempotency audit trail)."""
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO bridge_sends
                (cycle_id, client_message_id, message_sha256, attempt,
                 dispatch_state, error_code, outcome, response_found)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(cycle_id),
                    client_message_id,
                    message_sha256,
                    int(attempt),
                    dispatch_state,
                    error_code,
                    outcome,
                    int(response_found) if response_found is not None else None,
                ),
            )

    def get_bridge_sends(self, cycle_id: str, limit: int = 100) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT cycle_id, client_message_id, message_sha256, attempt,
                dispatch_state, error_code, outcome, response_found, created_at
                FROM bridge_sends WHERE cycle_id=? ORDER BY id LIMIT ?""",
                (str(cycle_id), limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_review_event(self, event_id: str, event_type: str, payload: dict) -> bool:
        """Atomically reserve a review event id.

        Returns False when the id already exists (the review event was already
        claimed), which is the durable idempotency boundary: the same stable
        event id can never be delivered twice, even across process restarts.
        """
        if not event_id or not event_id.startswith("gpttradder-review:"):
            raise ValueError("review event ids must use the gpttradder-review: prefix")
        try:
            with self.connect() as conn:
                conn.execute(
                    """INSERT INTO review_events(event_id, event_type, status, payload_json)
                    VALUES (?, ?, 'CLAIMED', ?)""",
                    (event_id, event_type, json.dumps(payload)),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def finish_review_event(self, event_id: str, *, status: str, response: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE review_events SET status=?, response_json=?, attempts=attempts+1,
                   sent_at=CURRENT_TIMESTAMP WHERE event_id=?""",
                (status, json.dumps({"text": response}) if response is not None else None, event_id),
            )

    def review_event_exists(self, event_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM review_events WHERE event_id=?", (event_id,)
            ).fetchone()
        return row is not None

    def review_event_attempts(self, event_id: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT attempts FROM review_events WHERE event_id=?", (event_id,)
            ).fetchone()
        return 0 if row is None else int(row["attempts"])

    def get_review_events(self, limit: int = 100, event_type: str | None = None) -> list[dict]:
        limit = max(1, min(500, int(limit)))
        sql = "SELECT event_id, event_type, status, attempts, created_at, sent_at FROM review_events"
        params: list = []
        if event_type:
            sql += " WHERE event_type=?"
            params.append(event_type)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

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

    def claim_execution(self, decision: Decision) -> bool:
        """Reserve decision_id before any broker side effect.

        A crash after this insert leaves a PENDING row, which deliberately fails closed
        on every retry rather than risking a second broker order.
        """
        pending = ExecutionResult(
            decision_id=decision.decision_id,
            cycle_id=decision.cycle_id,
            status="PENDING",
            reason="EXECUTION_CLAIMED",
        )
        try:
            with self.connect() as conn:
                conn.execute(
                    """INSERT INTO executions
                    (decision_id, cycle_id, status, broker_ticket, payload_json)
                    VALUES (?, ?, 'PENDING', NULL, ?)""",
                    (str(decision.decision_id), str(decision.cycle_id), pending.model_dump_json()),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def finalize_execution(self, result: ExecutionResult) -> None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT status FROM executions WHERE decision_id=?", (str(result.decision_id),)
            ).fetchone()
            if row is None:
                raise RuntimeError("Execution must be claimed before it can be finalized")
            conn.execute(
                """UPDATE executions SET status=?, broker_ticket=?, payload_json=?
                WHERE decision_id=?""",
                (result.status, result.broker_ticket, result.model_dump_json(), str(result.decision_id)),
            )
            conn.execute(
                "UPDATE decisions SET executed=1 WHERE decision_id=?",
                (str(result.decision_id),),
            )

    def get_execution(self, decision_id: UUID) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM executions WHERE decision_id=?", (str(decision_id),)
            ).fetchone()
            return None if row is None else dict(row)

    # Compatibility helpers retained for callers/tests.
    def was_executed(self, decision_id: UUID) -> bool:
        return self.get_execution(decision_id) is not None

    def save_execution(self, result: ExecutionResult) -> bool:
        if not self.claim_execution(
            Decision.model_validate_json(
                self._decision_payload(result.decision_id)
            )
        ):
            return False
        self.finalize_execution(result)
        return True

    def _decision_payload(self, decision_id: UUID) -> str:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM decisions WHERE decision_id=?", (str(decision_id),)
            ).fetchone()
            if row is None:
                raise RuntimeError("Decision does not exist")
            return str(row["payload_json"])

    def update_decision_metadata(self, decision: Decision, metadata: dict) -> None:
        """Merge informational metadata (e.g. broker risk breakdown) into a saved decision."""
        if not metadata:
            return
        with self.connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM decisions WHERE decision_id=?", (str(decision.decision_id),)
            ).fetchone()
            if row is None:
                return
            payload = json.loads(row["payload_json"])
            merged = {**(payload.get("metadata") or {}), **metadata}
            payload["metadata"] = merged
            conn.execute(
                "UPDATE decisions SET payload_json=? WHERE decision_id=?",
                (json.dumps(payload), str(decision.decision_id)),
            )

    def get_recent_decisions(self, limit: int = 20) -> list[dict]:
        limit = max(1, min(100, int(limit)))
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT d.payload_json AS decision_json, e.payload_json AS execution_json
                FROM decisions d
                LEFT JOIN executions e ON e.decision_id=d.decision_id
                ORDER BY d.created_at DESC, d.rowid DESC
                LIMIT ?""",
                (limit,),
            ).fetchall()
        history = []
        for row in reversed(rows):
            item = {"decision": json.loads(row["decision_json"])}
            if row["execution_json"]:
                item["execution"] = json.loads(row["execution_json"])
            history.append(item)
        return history

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

    def apply_risk_state(self, account: AccountState, timezone_name: str) -> AccountState:
        tz = ZoneInfo(timezone_name)
        today = datetime.now(tz).date().isoformat()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = {
                row["key"]: json.loads(row["value"])
                for row in conn.execute(
                    "SELECT key, value FROM state WHERE key IN ('risk_day','daily_start_equity','peak_equity')"
                ).fetchall()
            }
            peak = max(float(rows.get("peak_equity", account.equity)), account.equity)
            if rows.get("risk_day") != today:
                daily_start = account.equity
            else:
                daily_start = float(rows.get("daily_start_equity", account.equity))

            values = {
                "risk_day": today,
                "daily_start_equity": daily_start,
                "peak_equity": peak,
            }
            for key, value in values.items():
                conn.execute(
                    """INSERT INTO state(key,value,updated_at) VALUES (?,?,CURRENT_TIMESTAMP)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP""",
                    (key, json.dumps(value)),
                )

        daily_pnl = account.equity - daily_start
        dd = 0.0 if peak <= 0 else max(0.0, (peak - account.equity) / peak * 100)
        return account.model_copy(
            update={
                "daily_start_equity": daily_start,
                "daily_pnl": daily_pnl,
                "peak_equity": peak,
                "trailing_drawdown_pct": dd,
            }
        )

    def try_acquire_lock(self, name: str, owner: str, ttl_seconds: int) -> bool:
        now = time.time()
        expires = now + ttl_seconds
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM runtime_locks WHERE expires_at <= ?", (now,))
            existing = conn.execute("SELECT owner FROM runtime_locks WHERE name=?", (name,)).fetchone()
            if existing is not None:
                conn.execute("ROLLBACK")
                return False
            conn.execute(
                "INSERT INTO runtime_locks(name,owner,expires_at) VALUES (?,?,?)",
                (name, owner, expires),
            )
            conn.execute("COMMIT")
            return True
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def release_lock(self, name: str, owner: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM runtime_locks WHERE name=? AND owner=?", (name, owner))

    def acquire_notification_slot(self, key: str, cooldown_seconds: int) -> bool:
        """Persistently dedupe noisy alerts across process restarts."""
        state_key = f"notification:{key}"
        now = time.time()
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT value FROM state WHERE key=?", (state_key,)).fetchone()
            last = 0.0 if row is None else float(json.loads(row["value"]))
            if cooldown_seconds > 0 and now - last < cooldown_seconds:
                conn.execute("ROLLBACK")
                return False
            conn.execute(
                """INSERT INTO state(key,value,updated_at) VALUES (?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP""",
                (state_key, json.dumps(now)),
            )
            conn.execute("COMMIT")
            return True
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def heartbeat(self, component: str, payload: dict | None = None) -> None:
        self.set_state(
            f"heartbeat:{component}",
            {"ts": time.time(), "payload": payload or {}},
        )

    def heartbeat_age(self, component: str) -> float | None:
        value = self.get_state(f"heartbeat:{component}")
        if not isinstance(value, dict) or "ts" not in value:
            return None
        return max(0.0, time.time() - float(value["ts"]))

    def save_daily_report(self, report_date: str, payload: dict, report_text: str, *, sent: bool) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO daily_reports(report_date,payload_json,report_text,sent)
                VALUES (?,?,?,?)
                ON CONFLICT(report_date) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    report_text=excluded.report_text,
                    sent=MAX(daily_reports.sent, excluded.sent)""",
                (report_date, json.dumps(payload), report_text, int(sent)),
            )

    def get_daily_report(self, report_date: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM daily_reports WHERE report_date=?", (report_date,)
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["payload"] = json.loads(value.pop("payload_json"))
        return value

    @staticmethod
    def _iso_to_local_day(value: str, timezone_name: str) -> str | None:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                from datetime import timezone
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(ZoneInfo(timezone_name)).date().isoformat()
        except Exception:
            return None

    def get_day_summary(self, day: str, timezone_name: str) -> dict:
        """Build a report from durable cycle/decision/execution records.

        Cycles carry the authoritative timestamp and account snapshot, so decisions
        are joined to their cycle instead of relying on SQLite's implicit UTC text.
        """
        with self.connect() as conn:
            cycles = conn.execute(
                "SELECT cycle_id,created_at,status,packet_json FROM cycles ORDER BY created_at, rowid"
            ).fetchall()
            decisions = conn.execute(
                "SELECT decision_id,cycle_id,decision,symbol,payload_json FROM decisions ORDER BY rowid"
            ).fetchall()
            executions = conn.execute(
                "SELECT decision_id,cycle_id,status,payload_json FROM executions ORDER BY id"
            ).fetchall()

        selected_cycles = []
        cycle_ids: set[str] = set()
        for row in cycles:
            if self._iso_to_local_day(str(row["created_at"]), timezone_name) == day:
                payload = json.loads(row["packet_json"])
                selected_cycles.append((row, payload))
                cycle_ids.add(str(row["cycle_id"]))

        selected_decisions = [row for row in decisions if str(row["cycle_id"]) in cycle_ids]
        selected_executions = [row for row in executions if str(row["cycle_id"]) in cycle_ids]
        return self._summarize(selected_cycles, selected_decisions, selected_executions)

    def get_since_summary(self, since_iso: str, timezone_name: str) -> dict:
        """Window-based summary (cycles created at/after `since_iso`, UTC ISO)."""
        with self.connect() as conn:
            cycles = conn.execute(
                """SELECT cycle_id,created_at,status,packet_json FROM cycles
                   WHERE created_at >= ? ORDER BY created_at, rowid""",
                (since_iso,),
            ).fetchall()
            decisions = conn.execute(
                "SELECT decision_id,cycle_id,decision,symbol,payload_json FROM decisions ORDER BY rowid"
            ).fetchall()
            executions = conn.execute(
                "SELECT decision_id,cycle_id,status,payload_json FROM executions ORDER BY id"
            ).fetchall()

        cycle_ids = {str(row["cycle_id"]) for row in cycles}
        selected_cycles = [(row, json.loads(row["packet_json"])) for row in cycles]
        selected_decisions = [row for row in decisions if str(row["cycle_id"]) in cycle_ids]
        selected_executions = [row for row in executions if str(row["cycle_id"]) in cycle_ids]
        return self._summarize(selected_cycles, selected_decisions, selected_executions)

    def get_review_payloads(self, event_type: str, since_iso: str | None = None, limit: int = 500) -> list[dict]:
        limit = max(1, min(2000, int(limit)))
        sql = "SELECT event_id, payload_json FROM review_events WHERE event_type=?"
        params: list = [event_type]
        if since_iso:
            sql += " AND created_at >= ?"
            params.append(since_iso)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    @staticmethod
    def _summarize(
        selected_cycles: list[tuple],
        selected_decisions: list,
        selected_executions: list,
    ) -> dict:
        decision_counts: dict[str, int] = {}
        symbol_counts: dict[str, int] = {}
        for row in selected_decisions:
            action = str(row["decision"])
            decision_counts[action] = decision_counts.get(action, 0) + 1
            symbol = row["symbol"]
            if symbol:
                symbol_counts[str(symbol)] = symbol_counts.get(str(symbol), 0) + 1

        execution_counts: dict[str, int] = {}
        rejection_reasons: dict[str, int] = {}
        for row in selected_executions:
            status = str(row["status"])
            execution_counts[status] = execution_counts.get(status, 0) + 1
            if status == "REJECTED":
                payload = json.loads(row["payload_json"])
                reason = str(payload.get("reason") or "REJECTED")[:160]
                rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1

        accounts = [payload.get("account") or {} for _, payload in selected_cycles]
        first_account = accounts[0] if accounts else {}
        last_account = accounts[-1] if accounts else {}
        last_packet = selected_cycles[-1][1] if selected_cycles else {}

        top_rejections = sorted(
            rejection_reasons.items(), key=lambda item: (-item[1], item[0])
        )
        return {
            "cycles": len(selected_cycles),
            "decisions": len(selected_decisions),
            "decision_counts": decision_counts,
            "execution_counts": execution_counts,
            "symbol_counts": symbol_counts,
            "start_equity": first_account.get("equity"),
            "end_equity": last_account.get("equity"),
            "latest_daily_pnl": last_account.get("daily_pnl"),
            "latest_drawdown_pct": last_account.get("trailing_drawdown_pct"),
            "peak_equity": last_account.get("peak_equity"),
            "open_positions": len(last_packet.get("positions") or []),
            "pending_orders": len(last_packet.get("pending_orders") or []),
            "top_rejections": top_rejections,
        }

    def latest_packet(self) -> dict | None:
        """Latest stored MarketPacket parsed from JSON, or None."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT packet_json FROM cycles ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        return json.loads(row["packet_json"]) if row else None

    def dashboard_snapshot(self, *, limit: int = 20, timezone_name: str = "UTC") -> dict:
        limit = max(1, min(100, int(limit)))
        with self.connect() as conn:
            latest_cycle = conn.execute(
                "SELECT cycle_id,created_at,status,packet_json FROM cycles ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            recent_rows = conn.execute(
                """SELECT d.decision_id,d.cycle_id,d.decision,d.symbol,d.created_at,
                          d.payload_json,e.status AS execution_status,e.payload_json AS execution_json
                   FROM decisions d LEFT JOIN executions e ON e.decision_id=d.decision_id
                   ORDER BY d.rowid DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            totals = conn.execute(
                "SELECT COUNT(*) AS cycles FROM cycles"
            ).fetchone()

        latest_packet = json.loads(latest_cycle["packet_json"]) if latest_cycle else None
        recent = []
        for row in recent_rows:
            recent.append(
                {
                    "decision_id": row["decision_id"],
                    "cycle_id": row["cycle_id"],
                    "decision": row["decision"],
                    "symbol": row["symbol"],
                    "created_at": row["created_at"],
                    "execution_status": row["execution_status"],
                    "reason": json.loads(row["payload_json"]).get("reason"),
                }
            )
        local_day = datetime.now(ZoneInfo(timezone_name)).date().isoformat()
        return {
            "latest_cycle": None if latest_cycle is None else {
                "cycle_id": latest_cycle["cycle_id"],
                "created_at": latest_cycle["created_at"],
                "status": latest_cycle["status"],
            },
            "account": None if latest_packet is None else latest_packet.get("account"),
            "positions": [] if latest_packet is None else latest_packet.get("positions", []),
            "pending_orders": [] if latest_packet is None else latest_packet.get("pending_orders", []),
            "symbols": {} if latest_packet is None else latest_packet.get("symbols", {}),
            "recent_decisions": recent,
            "day": self.get_day_summary(local_day, timezone_name),
            "runtime_heartbeat_age": self.heartbeat_age("runtime"),
            "watchdog_heartbeat_age": self.heartbeat_age("watchdog"),
            "bridge_status": self.get_state("bridge_status", "unknown"),
            "watchdog_status": self.get_state("watchdog_status", "unknown"),
            "total_cycles": int(totals["cycles"]) if totals else 0,
        }
