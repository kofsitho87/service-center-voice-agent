import asyncio

import fakeredis.aioredis
import pytest

import agents.base_agent as base_agent_module
from agents.base_agent import _prepare_queued_warm_transfer_briefing
from data import TransferRecord
from services.warm_transfer_queue import CallDirection, WarmTransferQueue


@pytest.mark.asyncio
async def test_queued_briefing_is_published_for_every_fifo_entry(monkeypatch):
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    queue = WarmTransferQueue(redis_client)
    try:
        await queue.enqueue(
            42,
            "sip-a",
            room_name="room-a",
            call_direction=CallDirection.INBOUND,
            metadata={"briefing_text": ""},
        )
        await queue.enqueue(
            42,
            "sip-b",
            room_name="room-b",
            call_direction=CallDirection.INBOUND,
            metadata={"briefing_text": ""},
        )

        async def _generate_briefing(**kwargs) -> str:
            return f"브리핑:{kwargs['conversation_history']}"

        monkeypatch.setattr(
            base_agent_module,
            "generate_warm_transfer_briefing_text",
            _generate_briefing,
        )

        records = {
            "sip-a": TransferRecord(),
            "sip-b": TransferRecord(),
        }

        async def _prepare(sip_identity: str) -> str | None:
            async def _on_ready(briefing_text: str) -> None:
                await queue.update_entry(
                    42,
                    sip_identity,
                    event="briefing_ready",
                    fields={"briefing_text": briefing_text},
                )

            return await _prepare_queued_warm_transfer_briefing(
                company_id=42,
                sip_identity=sip_identity,
                conversation_history=sip_identity,
                customer_name=None,
                is_new_customer=True,
                language_code="ko",
                transfer_record=records[sip_identity],
                on_ready=_on_ready,
            )

        await asyncio.gather(_prepare("sip-a"), _prepare("sip-b"))

        first = await queue.get_entry(42, "sip-a")
        second = await queue.get_entry(42, "sip-b")
        assert await queue.get_rank(42, "sip-a") == 0
        assert await queue.get_rank(42, "sip-b") == 1
        assert first["status"] == "queued"
        assert second["status"] == "queued"
        assert first["briefing_text"] == "브리핑:sip-a"
        assert second["briefing_text"] == "브리핑:sip-b"
        assert records["sip-a"].briefing_text == "브리핑:sip-a"
        assert records["sip-b"].briefing_text == "브리핑:sip-b"

        events = await redis_client.xrange(queue.event_stream_key(42))
        briefing_events = [
            fields for _, fields in events if fields["event"] == "briefing_ready"
        ]
        assert {event["sip_identity"] for event in briefing_events} == {
            "sip-a",
            "sip-b",
        }
        assert all("briefing_text" not in event for event in briefing_events)
    finally:
        await redis_client.aclose()
