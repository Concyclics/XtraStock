from __future__ import annotations

import datetime as dt
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

import psycopg
from psycopg.rows import dict_row


SendCallback = Callable[[str, str, dict[str, Any]], bool]
LogCallback = Callable[[str, dict[str, Any]], None]


def _load_env() -> dict[str, str]:
    from xtrastock.config import load_legacy_env
    return load_legacy_env()


def _connect():
    from xtrastock.config import Settings
    return psycopg.connect(**{**Settings().postgres_kwargs('app'), 'connect_timeout': 5, 'row_factory': dict_row})


class OutboxWorker:
    """Leased, idempotent sender for one connector channel."""

    def __init__(self, channel: str, send: SendCallback, log: LogCallback | None = None,
                 poll_seconds: float = 1.0):
        if channel not in {"qq", "wechat"}:
            raise ValueError("unsupported outbox channel")
        self.channel = channel
        self.send_callback = send
        self.log_callback = log
        self.poll_seconds = max(0.2, float(poll_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._loop, name=f"finance-outbox-{self.channel}", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _log(self, event: str, payload: dict[str, Any]) -> None:
        if self.log_callback:
            self.log_callback(event, payload)

    def _claim(self) -> dict[str, Any] | None:
        with _connect() as conn:
            conn.execute(
                """
                UPDATE agent.message_outbox
                SET status='failed',available_at=now(),claimed_at=NULL,
                    last_error_class='StaleLease',last_error='sending lease expired',updated_at=now()
                WHERE channel=%s AND status='sending' AND claimed_at < now()-interval '5 minutes'
                """,
                (self.channel,),
            )
            row = conn.execute(
                """
                WITH candidate AS (
                  SELECT message_id
                  FROM agent.message_outbox
                  WHERE channel=%s AND status IN ('pending','failed')
                    AND available_at <= now() AND attempt_count < max_attempts
                  ORDER BY priority,available_at,created_at,message_id
                  FOR UPDATE SKIP LOCKED LIMIT 1
                )
                UPDATE agent.message_outbox o
                SET status='sending',claimed_at=now(),attempt_count=o.attempt_count+1,updated_at=now()
                FROM candidate c WHERE o.message_id=c.message_id
                RETURNING o.*
                """,
                (self.channel,),
            ).fetchone()
        return dict(row) if row else None

    def _sent(self, message_id: Any) -> None:
        with _connect() as conn:
            conn.execute(
                """
                UPDATE agent.message_outbox
                SET status='sent',sent_at=now(),claimed_at=NULL,last_error_class=NULL,
                    last_error=NULL,updated_at=now() WHERE message_id=%s
                """,
                (message_id,),
            )

    def _failed(self, row: dict[str, Any], exc: Exception) -> None:
        exhausted = int(row["attempt_count"]) >= int(row["max_attempts"])
        delay = min(900, 2 ** min(int(row["attempt_count"]), 9))
        with _connect() as conn:
            conn.execute(
                """
                UPDATE agent.message_outbox
                SET status=%s,available_at=now()+(%s*interval '1 second'),claimed_at=NULL,
                    last_error_class=%s,last_error=%s,updated_at=now()
                WHERE message_id=%s
                """,
                (
                    "dead_letter" if exhausted else "failed", delay,
                    type(exc).__name__, str(exc)[:2000], row["message_id"],
                ),
            )

    def run_once(self) -> bool:
        row = self._claim()
        if not row:
            return False
        payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
        try:
            if not self.send_callback(row["target_type"], row["target_id"], payload):
                raise RuntimeError("connector returned false")
            self._sent(row["message_id"])
            self._log("outbox_sent", {
                "message_id": str(row["message_id"]), "message_kind": row["message_kind"],
                "target_type": row["target_type"], "attempt": row["attempt_count"],
            })
        except Exception as exc:  # noqa: BLE001
            self._failed(row, exc)
            self._log("outbox_failed", {
                "message_id": str(row["message_id"]), "message_kind": row["message_kind"],
                "target_type": row["target_type"], "attempt": row["attempt_count"],
                "error_class": type(exc).__name__, "error": str(exc),
            })
        return True

    def _loop(self) -> None:
        last_db_error: str | None = None
        while not self._stop.is_set():
            try:
                worked = self.run_once()
                last_db_error = None
                if not worked:
                    self._stop.wait(self.poll_seconds)
            except Exception as exc:  # noqa: BLE001
                current = f"{type(exc).__name__}: {exc}"
                if current != last_db_error:
                    self._log("outbox_database_error", {"error": current})
                    last_db_error = current
                self._stop.wait(min(30.0, self.poll_seconds * 5))
