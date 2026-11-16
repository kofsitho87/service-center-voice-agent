"""Call-end event publisher backed by a local JSONL event log.

Exposes Kafka-compatible public functions (``send_kafka_message``/
``send_kafka_message_async``/``get_kafka_client``/``cleanup``) with a
synchronous-send result contract (``True`` only after the record is durably
written), and appends one JSON line per message to
``artifacts/events/<topic>.jsonl`` instead of publishing to a broker.

Environment:
    DEMO_EVENTS_DIR: output directory (default ``artifacts/events``)
    KAFKA_TOPIC: default topic name
"""

import asyncio
import atexit
import json
import os
import threading
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from fake_backend.config import ARTIFACTS_DIR

DEFAULT_TOPIC = "service-center.call-analysis"


class KafkaProducerClient:
    """Local JSONL "producer" with a Kafka-producer-style interface."""

    def __init__(self):
        self._lock = threading.Lock()

    @property
    def directory(self) -> Path:
        # Resolved per send: the client is a process-wide singleton, so caching
        # the directory at construction would pin it to the first caller's env.
        return Path(os.getenv("DEMO_EVENTS_DIR", str(ARTIFACTS_DIR / "events")))

    @property
    def default_topic(self) -> str:
        return os.getenv("KAFKA_TOPIC") or DEFAULT_TOPIC

    def connect(self) -> bool:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            return True
        except OSError as e:
            logger.bind(event="KAFKA_CONNECT", stage="exception").opt(
                exception=e
            ).error("Failed to prepare local event directory")
            return False

    def send(
        self,
        message: Any,
        topic: Optional[str] = None,
        key: Optional[str] = None,
    ) -> bool:
        """Append one message; ``True`` only after it is flushed to disk."""
        if not self.connect():
            return False
        target_topic = topic or self.default_topic
        try:
            # Serialization contract: the payload must be JSON-serializable.
            record = json.dumps({"key": key, "value": message}, ensure_ascii=False)
            path = self.directory / f"{target_topic}.jsonl"
            with self._lock, path.open("a", encoding="utf-8") as fh:
                fh.write(record + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            logger.bind(
                event="KAFKA_SEND", stage="local_append", topic=target_topic
            ).info("Call-end event written to local event log")
            return True
        except (TypeError, ValueError, OSError) as e:
            logger.bind(event="KAFKA_SEND", stage="exception", topic=target_topic).opt(
                exception=e
            ).error("Failed to write call-end event")
            return False

    def flush(self, timeout: float = 10.0):
        """Writes are synchronous; kept for interface compatibility."""

    def close(self):
        """Nothing to release; kept for interface compatibility."""


_kafka_client: Optional[KafkaProducerClient] = None


def get_kafka_client() -> KafkaProducerClient:
    global _kafka_client
    if _kafka_client is None:
        _kafka_client = KafkaProducerClient()
    return _kafka_client


def send_kafka_message(
    message: Any,
    topic: Optional[str] = None,
    key: Optional[str] = None,
) -> bool:
    try:
        return get_kafka_client().send(message=message, topic=topic, key=key)
    except Exception as e:
        logger.bind(event="KAFKA_SEND", stage="exception").opt(exception=e).error(
            "Failed to send call-end event"
        )
        return False


async def send_kafka_message_async(
    message: Any,
    topic: Optional[str] = None,
    key: Optional[str] = None,
) -> bool:
    """Run the blocking send off the event loop."""
    return await asyncio.to_thread(send_kafka_message, message, topic, key)


@atexit.register
def cleanup():
    global _kafka_client
    if _kafka_client:
        _kafka_client.close()
