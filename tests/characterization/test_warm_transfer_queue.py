import time

import fakeredis.aioredis
import pytest

from services.warm_transfer_queue import (
    CallDirection,
    WarmTransferQueue,
    WarmTransferQueueStatus,
)


@pytest.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def queue(redis_client):
    return WarmTransferQueue(redis_client)


@pytest.mark.asyncio
async def test_enqueue_stores_live_metadata_and_publishes_non_pii_event(
    redis_client,
    queue,
):
    enqueue_ts = await queue.enqueue(
        42,
        "sip-caller-1",
        room_name="room-1",
        call_direction=CallDirection.INBOUND,
        metadata={
            "phone": "010-0000-0101",
            "user_name": "홍길동",
            "verification_code": "1990-01-01",
            "customer_no": "C-123",
            "customer_visit": "REVISIT",
            "briefing_text": "",
        },
    )

    entry = await queue.get_entry(42, "sip-caller-1")
    assert float(entry["enqueue_ts"]) == enqueue_ts
    assert entry["room_name"] == "room-1"
    assert entry["call_direction"] == CallDirection.INBOUND.value
    assert entry["status"] == WarmTransferQueueStatus.QUEUED.value
    assert entry["attempt_count"] == "0"
    assert entry["phone"] == "010-0000-0101"
    assert entry["user_name"] == "홍길동"
    assert entry["verification_code"] == "1990-01-01"
    assert entry["customer_no"] == "C-123"
    assert entry["customer_visit"] == "REVISIT"
    assert await queue.get_rank(42, "sip-caller-1") == 0

    events = await redis_client.xrange(queue.event_stream_key(42))
    assert len(events) == 1
    _, event = events[0]
    assert event["event"] == "enqueued"
    assert event["sip_identity"] == "sip-caller-1"
    assert event["status"] == WarmTransferQueueStatus.QUEUED.value
    assert "phone" not in event
    assert "user_name" not in event
    assert "verification_code" not in event
    assert "customer_no" not in event
    assert "call_direction" not in event


@pytest.mark.asyncio
async def test_duplicate_enqueue_preserves_original_metadata_and_event(
    redis_client, queue
):
    first_ts = await queue.enqueue(
        42,
        "sip-caller-1",
        room_name="room-original",
        call_direction=CallDirection.INBOUND,
        metadata={"user_name": "original"},
    )
    second_ts = await queue.enqueue(
        42,
        "sip-caller-1",
        room_name="room-duplicate",
        call_direction=CallDirection.OUTBOUND,
        metadata={"user_name": "duplicate"},
    )

    entry = await queue.get_entry(42, "sip-caller-1")
    events = await redis_client.xrange(queue.event_stream_key(42))

    assert second_ts == first_ts
    assert entry["room_name"] == "room-original"
    assert entry["call_direction"] == CallDirection.INBOUND.value
    assert entry["user_name"] == "original"
    assert len(events) == 1


@pytest.mark.asyncio
async def test_update_entry_tracks_status_attempt_and_briefing(redis_client, queue):
    await queue.enqueue(
        42,
        "sip-caller-1",
        room_name="room-1",
        call_direction=CallDirection.INBOUND,
    )

    updated = await queue.update_entry(
        42,
        "sip-caller-1",
        status=WarmTransferQueueStatus.DIALING,
        event="dial_started",
        fields={
            "dial_started_at": time.time(),
            "briefing_text": "휴대폰 점검 예약 변경 문의",
        },
        increment_attempt=True,
    )

    entry = await queue.get_entry(42, "sip-caller-1")
    events = await redis_client.xrange(queue.event_stream_key(42))

    assert updated is True
    assert entry["status"] == WarmTransferQueueStatus.DIALING.value
    assert entry["attempt_count"] == "1"
    assert entry["briefing_text"] == "휴대폰 점검 예약 변경 문의"
    assert events[-1][1]["event"] == "dial_started"
    assert "briefing_text" not in events[-1][1]


@pytest.mark.asyncio
async def test_dequeue_retains_final_read_model_and_rejects_late_update(
    redis_client,
    queue,
):
    await queue.enqueue(
        42,
        "sip-caller-1",
        room_name="room-1",
        call_direction=CallDirection.INBOUND,
        metadata={"user_name": "홍길동"},
    )

    await queue.dequeue(
        42,
        "sip-caller-1",
        reason="conversation_ended",
        final_status=WarmTransferQueueStatus.COMPLETED,
        retain_metadata_seconds=600,
    )

    entry = await queue.get_entry(42, "sip-caller-1")
    late_update = await queue.update_entry(
        42,
        "sip-caller-1",
        status=WarmTransferQueueStatus.BRIEFING,
        event="late_update",
    )
    events = await redis_client.xrange(queue.event_stream_key(42))

    assert await queue.get_rank(42, "sip-caller-1") is None
    assert entry["status"] == WarmTransferQueueStatus.COMPLETED.value
    assert entry["last_status"] == "conversation_ended"
    assert entry["user_name"] == "홍길동"
    assert 0 < await redis_client.ttl("wt:entry:{42}:sip-caller-1") <= 600
    assert late_update is False
    assert events[-1][1]["event"] == "dequeued"
    assert events[-1][1]["reason"] == "conversation_ended"
    assert events[-1][1]["status"] == WarmTransferQueueStatus.COMPLETED.value
    assert all(event[1]["event"] != "late_update" for event in events)


@pytest.mark.asyncio
async def test_queue_order_is_company_scoped(queue):
    await queue.enqueue(
        42,
        "sip-a",
        room_name="room-a",
        call_direction=CallDirection.INBOUND,
    )
    await queue.enqueue(
        42,
        "sip-b",
        room_name="room-b",
        call_direction=CallDirection.OUTBOUND,
    )
    await queue.enqueue(
        99,
        "sip-c",
        room_name="room-c",
        call_direction=CallDirection.INBOUND,
    )

    assert await queue.get_rank(42, "sip-a") == 0
    assert await queue.get_rank(42, "sip-b") == 1
    assert await queue.get_rank(99, "sip-c") == 0
    assert (await queue.get_entry(42, "sip-a"))["call_direction"] == "inbound"
    assert (await queue.get_entry(42, "sip-b"))["call_direction"] == "outbound"
