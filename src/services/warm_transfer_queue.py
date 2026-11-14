"""Redis-backed warm-transfer queue, live state, and event stream."""

from __future__ import annotations

import time
from collections.abc import Mapping
from enum import StrEnum
from typing import Any

import redis.asyncio as aioredis
from redis.exceptions import WatchError

QUEUE_KEY_PREFIX = "wt:queue"
ENTRY_KEY_PREFIX = "wt:entry"
EVENT_STREAM_KEY_PREFIX = "wt:events"
ENTRY_TTL_SECONDS = 660  # 10-minute queue deadline + 60s buffer
COMPLETED_ENTRY_TTL_SECONDS = 600
EVENT_STREAM_MAX_LENGTH = 10_000


class WarmTransferQueueStatus(StrEnum):
    """Backend-facing state for the live warm-transfer dashboard."""

    QUEUED = "queued"
    WAITING_FOR_TRUNK = "waiting_for_trunk"
    DIALING = "dialing"
    BRIEFING = "briefing"
    CONNECTED = "connected"
    RETRYING = "retrying"
    DECLINED = "declined"
    CALLER_DISCONNECTED = "caller_disconnected"
    QUEUE_TIMEOUT = "queue_timeout"
    COMPLETED = "completed"
    ERROR = "error"


class CallDirection(StrEnum):
    """Direction of the customer call that requested the warm transfer."""

    INBOUND = "inbound"
    OUTBOUND = "outbound"


def _company_slot(company_id: int) -> str:
    return f"{{{company_id}}}"


def _queue_key(company_id: int) -> str:
    return f"{QUEUE_KEY_PREFIX}:{_company_slot(company_id)}"


def _entry_key(company_id: int, sip_identity: str) -> str:
    return f"{ENTRY_KEY_PREFIX}:{_company_slot(company_id)}:{sip_identity}"


def _event_stream_key(company_id: int) -> str:
    return f"{EVENT_STREAM_KEY_PREFIX}:{_company_slot(company_id)}"


def _serialize_fields(fields: Mapping[str, Any]) -> dict[str, str]:
    """Convert optional dataclass values into Redis-safe strings."""
    serialized: dict[str, str] = {}
    for key, value in fields.items():
        if value is None:
            serialized[key] = ""
        elif isinstance(value, bool):
            serialized[key] = str(value).lower()
        else:
            serialized[key] = str(value)
    return serialized


class WarmTransferQueue:
    """Company-scoped FIFO queue and live-state store.

    The ZSET is authoritative for occupancy and order. The HASH is a live read
    model for the operations dashboard, while the Stream only notifies
    subscribers that the read model changed. Personally identifiable data is
    intentionally kept out of Stream payloads.
    """

    def __init__(self, client: aioredis.Redis) -> None:
        self._redis = client

    @staticmethod
    def event_stream_key(company_id: int) -> str:
        """Return the public Stream key consumed by backend services."""
        return _event_stream_key(company_id)

    async def enqueue(
        self,
        company_id: int,
        sip_identity: str,
        *,
        room_name: str,
        call_direction: CallDirection,
        metadata: Mapping[str, Any] | None = None,
    ) -> float:
        # ZSET score is the authoritative enqueue timestamp. Duplicate enqueues
        # must preserve the original ts so waited_seconds stays honest and the
        # zombie TTL window does not get extended by repeated HSET/EXPIRE calls.
        queue_key = _queue_key(company_id)
        entry_key = _entry_key(company_id, sip_identity)
        stream_key = _event_stream_key(company_id)

        for _ in range(10):
            timestamp = time.time()
            entry_fields = _serialize_fields(metadata or {})
            entry_fields.update(
                {
                    "room_name": room_name,
                    "call_direction": call_direction.value,
                    "enqueue_ts": str(timestamp),
                    "updated_at": str(timestamp),
                    "status": WarmTransferQueueStatus.QUEUED.value,
                    "attempt_count": "0",
                    "last_status": "",
                    "briefing_text": entry_fields.get("briefing_text", ""),
                }
            )
            entry_fields = _serialize_fields(entry_fields)

            async with self._redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(queue_key, entry_key)
                    existing_score = await pipe.zscore(queue_key, sip_identity)
                    if existing_score is not None:
                        await pipe.unwatch()
                        return float(existing_score)

                    pipe.multi()
                    pipe.zadd(queue_key, {sip_identity: timestamp}, nx=True)
                    pipe.hset(entry_key, mapping=entry_fields)
                    pipe.expire(entry_key, ENTRY_TTL_SECONDS)
                    pipe.xadd(
                        stream_key,
                        {
                            "event": "enqueued",
                            "sip_identity": sip_identity,
                            "status": WarmTransferQueueStatus.QUEUED.value,
                            "occurred_at": str(timestamp),
                        },
                        maxlen=EVENT_STREAM_MAX_LENGTH,
                        approximate=True,
                    )
                    await pipe.execute()
                    return timestamp
                except WatchError:
                    continue

        raise RuntimeError(
            "Warm-transfer queue changed during repeated enqueue attempts"
        )

    async def update_entry(
        self,
        company_id: int,
        sip_identity: str,
        *,
        status: WarmTransferQueueStatus | str | None = None,
        event: str | None = None,
        fields: Mapping[str, Any] | None = None,
        increment_attempt: bool = False,
    ) -> bool:
        """Update a live entry and optionally publish a non-PII Stream event.

        Returns ``False`` when the entry was already removed. The existence
        guard prevents late AgentTask callbacks from recreating a dequeued HASH.
        """
        entry_key = _entry_key(company_id, sip_identity)
        queue_key = _queue_key(company_id)
        status_value = status.value if isinstance(status, StrEnum) else status

        for _ in range(3):
            timestamp = time.time()
            entry_fields = _serialize_fields(fields or {})
            entry_fields["updated_at"] = str(timestamp)
            if status_value is not None:
                entry_fields["status"] = status_value

            async with self._redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(entry_key, queue_key)
                    entry_exists = await pipe.exists(entry_key)
                    queue_score = await pipe.zscore(queue_key, sip_identity)
                    if not entry_exists or queue_score is None:
                        await pipe.unwatch()
                        return False

                    pipe.multi()
                    if increment_attempt:
                        pipe.hincrby(entry_key, "attempt_count", 1)
                    pipe.hset(entry_key, mapping=entry_fields)
                    pipe.expire(entry_key, ENTRY_TTL_SECONDS)
                    if event is not None:
                        event_fields = {
                            "event": event,
                            "sip_identity": sip_identity,
                            "occurred_at": str(timestamp),
                        }
                        if status_value is not None:
                            event_fields["status"] = status_value
                        pipe.xadd(
                            _event_stream_key(company_id),
                            event_fields,
                            maxlen=EVENT_STREAM_MAX_LENGTH,
                            approximate=True,
                        )
                    await pipe.execute()
                    return True
                except WatchError:
                    continue

        raise RuntimeError(
            "Warm-transfer entry changed during repeated update attempts"
        )

    async def get_entry(self, company_id: int, sip_identity: str) -> dict[str, str]:
        """Return the dashboard read model for one warm-transfer call."""
        return await self._redis.hgetall(_entry_key(company_id, sip_identity))

    async def get_rank(self, company_id: int, sip_identity: str) -> int | None:
        rank = await self._redis.zrank(_queue_key(company_id), sip_identity)
        return rank  # redis-py returns None when missing

    async def dequeue(
        self,
        company_id: int,
        sip_identity: str,
        *,
        reason: str = "dequeued",
        final_status: WarmTransferQueueStatus | str | None = None,
        retain_metadata_seconds: int = 0,
    ) -> None:
        """Release occupancy and publish a durable dequeue notification.

        Retaining the HASH briefly lets a subscriber fetch the final state after
        receiving the event. Active snapshots must still be built from the ZSET,
        so retained completed HASHes never appear as active calls.
        """
        entry_key = _entry_key(company_id, sip_identity)
        timestamp = time.time()
        status_value = (
            final_status.value if isinstance(final_status, StrEnum) else final_status
        )
        metadata_exists = bool(await self._redis.exists(entry_key))

        pipe = self._redis.pipeline()
        pipe.zrem(_queue_key(company_id), sip_identity)
        if metadata_exists and retain_metadata_seconds > 0:
            final_fields = {
                "updated_at": str(timestamp),
                "completed_at": str(timestamp),
                "last_status": reason,
            }
            if status_value is not None:
                final_fields["status"] = status_value
            pipe.hset(entry_key, mapping=final_fields)
            pipe.expire(entry_key, retain_metadata_seconds)
        else:
            pipe.delete(entry_key)

        event_fields = {
            "event": "dequeued",
            "sip_identity": sip_identity,
            "reason": reason,
            "occurred_at": str(timestamp),
        }
        if status_value is not None:
            event_fields["status"] = status_value
        pipe.xadd(
            _event_stream_key(company_id),
            event_fields,
            maxlen=EVENT_STREAM_MAX_LENGTH,
            approximate=True,
        )
        await pipe.execute()

    async def get_queue_length(self, company_id: int) -> int:
        return int(await self._redis.zcard(_queue_key(company_id)))

    async def get_front_entry(self, company_id: int) -> tuple[str, float] | None:
        """Return (sip_identity, score) of the current front entry, or None."""
        items = await self._redis.zrange(_queue_key(company_id), 0, 0, withscores=True)
        if not items:
            return None
        sid, score = items[0]
        return sid, float(score)

    async def refresh_entry_ttl(self, company_id: int, sip_identity: str) -> bool:
        return bool(
            await self._redis.expire(
                _entry_key(company_id, sip_identity),
                ENTRY_TTL_SECONDS,
            )
        )

    async def cleanup_stale_front(
        self, company_id: int, *, max_age_seconds: float
    ) -> str | None:
        """Remove the front entry if its metadata HASH expired and it is old."""
        front = await self.get_front_entry(company_id)
        if front is None:
            return None
        sid, score = front
        age = time.time() - score
        if age < max_age_seconds:
            return None
        meta_exists = bool(await self._redis.exists(_entry_key(company_id, sid)))
        if meta_exists:
            return None
        await self.dequeue(
            company_id,
            sid,
            reason="stale_removed",
            final_status=WarmTransferQueueStatus.ERROR,
        )
        return sid
