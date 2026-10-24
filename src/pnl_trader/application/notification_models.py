"""Durable notification records shared by the application and adapters."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OutboxNotification:
    event_id: str
    channel: str
    dedupe_key: str
    event_type: str
    payload_json: str
    priority: int
    attempts: int
    next_attempt_at_ms: int
