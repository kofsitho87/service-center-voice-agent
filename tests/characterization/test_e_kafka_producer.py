"""KafkaProducerClient.send against the local JSONL publisher.

The publisher is an append-only ``<DEMO_EVENTS_DIR>/<topic>.jsonl`` writer
with a synchronous-send result contract: ``True`` only after the record is
written, ``False`` (never an exception) on any failure.
"""

import json

import pytest

from external_apis import kafka_producer
from external_apis.kafka_producer import KafkaProducerClient


@pytest.fixture
def events_dir(tmp_path, monkeypatch):
    directory = tmp_path / "events"
    monkeypatch.setenv("DEMO_EVENTS_DIR", str(directory))
    monkeypatch.setenv("KAFKA_TOPIC", "default-topic")
    return directory


@pytest.fixture
def client(events_dir):
    return KafkaProducerClient()


def _records(path):
    return [json.loads(line) for line in path.read_text("utf-8").splitlines()]


def test_send_serializes_message_and_reports_success(client, events_dir):
    assert client.send({"a": "한글"}, key="room-1") is True

    path = events_dir / "default-topic.jsonl"
    assert _records(path) == [{"key": "room-1", "value": {"a": "한글"}}]
    # Non-ASCII stays readable in the local log (source encoded UTF-8 bytes).
    assert "한글" in path.read_text("utf-8")


def test_send_uses_explicit_topic_and_no_key(client, events_dir):
    assert client.send({"a": 1}, topic="other") is True

    assert _records(events_dir / "other.jsonl") == [{"key": None, "value": {"a": 1}}]
    assert not (events_dir / "default-topic.jsonl").exists()


def test_send_appends_one_line_per_message(client, events_dir):
    assert client.send({"n": 1}, key="k1") is True
    assert client.send({"n": 2}, key="k2") is True

    assert _records(events_dir / "default-topic.jsonl") == [
        {"key": "k1", "value": {"n": 1}},
        {"key": "k2", "value": {"n": 2}},
    ]


def test_default_topic_is_used_when_env_is_unset(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_EVENTS_DIR", str(tmp_path))
    monkeypatch.delenv("KAFKA_TOPIC", raising=False)

    assert KafkaProducerClient().send({"a": 1}) is True
    assert (tmp_path / f"{kafka_producer.DEFAULT_TOPIC}.jsonl").exists()


def test_non_json_payload_returns_false(client, events_dir):
    assert client.send({"a": object()}) is False

    path = events_dir / "default-topic.jsonl"
    assert not path.exists() or path.read_text("utf-8") == ""


def test_unwritable_directory_returns_false(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setenv("DEMO_EVENTS_DIR", str(blocker / "events"))

    assert KafkaProducerClient().send({"a": 1}) is False


def test_send_kafka_message_swallows_unexpected_errors(monkeypatch):
    class Exploding:
        def send(self, **_kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr(kafka_producer, "get_kafka_client", lambda: Exploding())
    assert kafka_producer.send_kafka_message({"a": 1}) is False


@pytest.mark.asyncio
async def test_send_kafka_message_async_runs_the_same_send(monkeypatch):
    calls = []

    class Recording:
        def send(self, **kwargs):
            calls.append(kwargs)
            return True

    monkeypatch.setattr(kafka_producer, "get_kafka_client", lambda: Recording())

    assert await kafka_producer.send_kafka_message_async({"a": 1}, "t", "k") is True
    assert calls == [{"message": {"a": 1}, "topic": "t", "key": "k"}]
