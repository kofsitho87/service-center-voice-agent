"""에이전트 발화 끊김 추적(utils.agent_interruption) 테스트.

인터럽트 임계값 env 해석, 세션 이벤트 상태 기계(pause → 해소 이벤트 → 레코드),
레코드 필드 계산, 세션 바인딩의 예외 차단을 검증한다. 시간은 가짜 시계로 고정한다.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from loguru import logger

from utils import agent_interruption
from utils.agent_interruption import InterruptionSettings, InterruptionTracker

DURATION_ENV = agent_interruption.INTERRUPT_MIN_DURATION_ENV
WORDS_ENV = agent_interruption.INTERRUPT_MIN_WORDS_ENV


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make_tracker(
    **kwargs: Any,
) -> tuple[InterruptionTracker, FakeClock, list[dict[str, Any]]]:
    clock = FakeClock()
    records: list[dict[str, Any]] = []
    kwargs.setdefault("started_at", 0.0)
    tracker = InterruptionTracker(clock=clock, emit=records.append, **kwargs)
    return tracker, clock, records


@pytest.fixture
def log_records():
    records: list[dict[str, Any]] = []
    handler_id = logger.add(
        lambda message: records.append(message.record), level="DEBUG"
    )
    yield records
    logger.remove(handler_id)


# ---------------------------------------------------------------- settings


def test_settings_use_defaults_without_env(log_records):
    settings = agent_interruption.resolve_interruption_settings(
        default_min_duration=0.5, default_min_words=2, environ={}
    )

    assert settings == InterruptionSettings(
        min_duration=0.5, min_words=2, source="default"
    )
    assert log_records == []


def test_settings_treat_empty_values_as_missing():
    settings = agent_interruption.resolve_interruption_settings(
        default_min_duration=0.2,
        default_min_words=1,
        environ={DURATION_ENV: "", WORDS_ENV: "   "},
    )

    assert settings == InterruptionSettings(
        min_duration=0.2, min_words=1, source="default"
    )


def test_settings_apply_valid_env_override(log_records):
    settings = agent_interruption.resolve_interruption_settings(
        default_min_duration=0.5,
        default_min_words=2,
        environ={DURATION_ENV: "0.3", WORDS_ENV: " 1 "},
    )

    assert settings == InterruptionSettings(min_duration=0.3, min_words=1, source="env")
    assert log_records == []


def test_settings_single_override_marks_source_env():
    settings = agent_interruption.resolve_interruption_settings(
        default_min_duration=0.5, default_min_words=2, environ={WORDS_ENV: "0"}
    )

    assert settings == InterruptionSettings(min_duration=0.5, min_words=0, source="env")


def test_settings_read_process_env_by_default(monkeypatch):
    monkeypatch.setenv(DURATION_ENV, "0.8")
    monkeypatch.delenv(WORDS_ENV, raising=False)

    settings = agent_interruption.resolve_interruption_settings(
        default_min_duration=0.2, default_min_words=1
    )

    assert settings == InterruptionSettings(min_duration=0.8, min_words=1, source="env")


@pytest.mark.parametrize(
    ("duration", "words"),
    [("abc", "abc"), ("-1", "-1"), ("nan", "1.5"), ("inf", "two")],
)
def test_settings_keep_defaults_for_invalid_values(log_records, duration, words):
    settings = agent_interruption.resolve_interruption_settings(
        default_min_duration=0.5,
        default_min_words=2,
        environ={DURATION_ENV: duration, WORDS_ENV: words},
    )

    assert settings == InterruptionSettings(
        min_duration=0.5, min_words=2, source="default"
    )
    invalid = [
        (record["extra"]["name"], record["extra"]["value"])
        for record in log_records
        if record["extra"].get("event") == "INTERRUPT_SETTINGS_INVALID"
    ]
    assert invalid == [(DURATION_ENV, duration), (WORDS_ENV, words)]
    assert all(record["level"].name == "WARNING" for record in log_records)


def test_settings_invalid_value_does_not_block_other_override():
    settings = agent_interruption.resolve_interruption_settings(
        default_min_duration=0.5,
        default_min_words=2,
        environ={DURATION_ENV: "fast", WORDS_ENV: "3"},
    )

    assert settings == InterruptionSettings(min_duration=0.5, min_words=3, source="env")


# ------------------------------------------------------------ tracker records


def test_false_interruption_resumed_record():
    tracker, clock, records = make_tracker()

    clock.now = 0.0
    tracker.on_agent_state("thinking", "speaking")
    clock.now = 3.0
    tracker.on_user_state("listening", "speaking")
    clock.now = 3.2
    tracker.on_transcript("네", is_final=False)
    clock.now = 3.3
    tracker.on_agent_state("speaking", "listening")
    clock.now = 3.6
    tracker.on_user_state("speaking", "listening")
    clock.now = 4.1
    tracker.on_false_interruption(resumed=True)

    assert len(records) == 1
    record = records[0]
    assert record["seq"] == 1
    assert record["outcome"] == "false_resumed"
    assert record["t_s"] == 3.3
    assert record["t_dump_s"] is None
    assert record["agent_said_s"] == 3.3
    assert record["user_speech_s"] == 0.6
    assert record["user_speaking"] is False
    assert record["interim_words"] == 1
    assert record["final_words"] == 0
    assert record["final_chars"] == 0
    assert record["speaker_id"] is None
    assert "text" not in record


def test_false_interruption_keeps_agent_said_when_resume_precedes_event():
    """SDK 는 재개할 때 listening→speaking 상태 변경을 false interruption 이벤트보다
    먼저 낸다. 재개로 갱신된 발화 시작 시각이 끊긴 발화 길이를 덮으면 안 된다."""
    tracker, clock, records = make_tracker()

    clock.now = 0.0
    tracker.on_agent_state("thinking", "speaking")
    clock.now = 3.0
    tracker.on_user_state("listening", "speaking")
    clock.now = 3.3
    tracker.on_agent_state("speaking", "listening")
    clock.now = 3.6
    tracker.on_user_state("speaking", "listening")
    clock.now = 4.1
    tracker.on_agent_state("listening", "speaking")
    tracker.on_false_interruption(resumed=True)

    assert len(records) == 1
    assert records[0]["t_s"] == 3.3
    assert records[0]["agent_said_s"] == 3.3


def test_false_interruption_not_resumed_is_dropped_outcome():
    tracker, clock, records = make_tracker()

    clock.now = 1.0
    tracker.on_agent_state("thinking", "speaking")
    clock.now = 2.0
    tracker.on_agent_state("speaking", "listening")
    clock.now = 2.5
    tracker.on_false_interruption(resumed=False)

    assert [record["outcome"] for record in records] == ["false_dropped"]


@pytest.mark.parametrize("log_text", [False, True])
def test_committed_interruption_record(log_text):
    tracker, clock, records = make_tracker(log_text=log_text, dump_origin=lambda: 0.5)

    clock.now = 0.0
    tracker.on_agent_state("thinking", "speaking")
    clock.now = 3.0
    tracker.on_user_state("listening", "speaking")
    clock.now = 3.2
    tracker.on_transcript("그게", is_final=False)
    clock.now = 3.3
    tracker.on_agent_state("speaking", "listening")
    clock.now = 3.6
    tracker.on_transcript("그게 아니라요", is_final=False)
    clock.now = 3.8
    tracker.on_user_state("speaking", "listening")
    clock.now = 3.9
    tracker.on_transcript("그게 아니라요 잠깐만요", is_final=True, speaker_id="spk_1")
    clock.now = 4.0
    tracker.on_item_added("message", "assistant", True)

    assert len(records) == 1
    record = records[0]
    assert record["outcome"] == "committed"
    assert record["t_s"] == 3.3
    assert record["t_dump_s"] == 2.8
    assert record["agent_said_s"] == 3.3
    assert record["user_speech_s"] == 0.8
    assert record["user_speaking"] is False
    assert record["interim_words"] == 1
    assert record["final_words"] == 3
    assert record["final_chars"] == 10
    assert record["speaker_id"] == "spk_1"
    if log_text:
        assert record["text"] == "그게 아니라요 잠깐만요"
    else:
        assert "text" not in record


def test_t_dump_s_is_none_until_dump_origin_is_known():
    tracker, clock, records = make_tracker(dump_origin=lambda: None)

    clock.now = 2.0
    tracker.on_false_interruption(resumed=True)

    assert records[0]["t_dump_s"] is None


def test_explicit_interrupt_without_user_speech():
    tracker, clock, records = make_tracker()

    clock.now = 1.0
    tracker.on_agent_state("thinking", "speaking")
    clock.now = 2.5
    tracker.on_agent_state("speaking", "listening")
    clock.now = 2.6
    tracker.on_item_added("message", "assistant", True)

    assert len(records) == 1
    record = records[0]
    assert record["outcome"] == "committed"
    assert record["t_s"] == 2.5
    assert record["agent_said_s"] == 1.5
    assert record["user_speech_s"] == 0.0
    assert record["user_speaking"] is False
    assert record["final_words"] == 0


def test_items_other_than_interrupted_assistant_message_are_ignored():
    tracker, clock, records = make_tracker()

    clock.now = 1.0
    tracker.on_agent_state("speaking", "listening")
    tracker.on_item_added("message", "user", True)
    tracker.on_item_added("message", "assistant", False)
    tracker.on_item_added("function_call", "assistant", True)
    tracker.on_item_added(None, None, True)

    assert records == []
    assert tracker.count == 0


def test_natural_end_emits_nothing_and_next_interruption_uses_own_pause():
    tracker, clock, records = make_tracker()

    # 1st turn: interrupted, dropped as a false interruption
    clock.now = 0.0
    tracker.on_agent_state("thinking", "speaking")
    clock.now = 2.0
    tracker.on_user_state("listening", "speaking")
    clock.now = 2.2
    tracker.on_agent_state("speaking", "listening")
    clock.now = 2.5
    tracker.on_user_state("speaking", "listening")
    clock.now = 3.0
    tracker.on_false_interruption(resumed=False)

    # 2nd turn: agent finishes speaking naturally
    clock.now = 5.0
    tracker.on_agent_state("listening", "thinking")
    tracker.on_agent_state("thinking", "speaking")
    clock.now = 8.0
    tracker.on_agent_state("speaking", "listening")
    assert len(records) == 1

    # 3rd turn: interrupted and committed
    clock.now = 10.0
    tracker.on_agent_state("listening", "thinking")
    tracker.on_agent_state("thinking", "speaking")
    clock.now = 11.0
    tracker.on_user_state("listening", "speaking")
    clock.now = 11.4
    tracker.on_agent_state("speaking", "listening")
    clock.now = 12.0
    tracker.on_item_added("message", "assistant", True)

    assert [record["seq"] for record in records] == [1, 2]
    assert [record["outcome"] for record in records] == ["false_dropped", "committed"]
    second = records[1]
    assert second["t_s"] == 11.4
    assert second["agent_said_s"] == 1.4
    assert second["user_speaking"] is True
    assert second["user_speech_s"] == 1.0


def test_resolution_without_pause_uses_resolution_time():
    tracker, clock, records = make_tracker(started_at=100.0)

    clock.now = 104.25
    tracker.on_false_interruption(resumed=False)

    assert len(records) == 1
    record = records[0]
    assert record["t_s"] == 4.25
    assert record["agent_said_s"] == 0.0
    assert record["user_speech_s"] == 0.0
    assert record["interim_words"] == 0


def test_user_speech_ended_long_before_pause_is_not_counted():
    tracker, clock, records = make_tracker()

    clock.now = 0.0
    tracker.on_user_state("listening", "speaking")
    clock.now = 1.0
    tracker.on_user_state("speaking", "listening")
    clock.now = 1.5
    tracker.on_agent_state("thinking", "speaking")
    clock.now = 4.0
    tracker.on_agent_state("speaking", "listening")
    clock.now = 4.2
    tracker.on_item_added("message", "assistant", True)

    assert records[0]["user_speech_s"] == 0.0


def test_pause_before_playout_uses_user_speech_start():
    """SDK 는 사용자가 에이전트 재생 전에 말을 시작하면 speaking→listening 없이 멈춘다
    (agent_activity on_start_of_speech). 그때 남아 있는 pause 는 이전 턴의 자연 종료라
    쓰면 안 되고, 사용자 발화 시작이 멈춤 시각이다."""
    tracker, clock, records = make_tracker()

    clock.now = 4.0
    tracker.on_agent_state("thinking", "speaking")
    clock.now = 10.0
    tracker.on_agent_state("speaking", "listening")  # natural end of the turn
    clock.now = 12.0
    tracker.on_user_state("listening", "speaking")
    clock.now = 12.8
    tracker.on_false_interruption(resumed=True)

    assert len(records) == 1
    record = records[0]
    assert record["t_s"] == 12.0
    assert record["agent_said_s"] == 0.0
    assert record["user_speech_s"] == 0.8
    assert record["user_speaking"] is True


def test_pause_before_playout_with_finished_user_speech():
    tracker, clock, records = make_tracker()

    clock.now = 4.0
    tracker.on_agent_state("thinking", "speaking")
    clock.now = 10.0
    tracker.on_agent_state("speaking", "listening")
    clock.now = 12.0
    tracker.on_user_state("listening", "speaking")
    clock.now = 12.5
    tracker.on_user_state("speaking", "listening")
    clock.now = 12.7
    tracker.on_transcript("여보세요", is_final=True)
    clock.now = 13.0
    tracker.on_item_added("message", "assistant", True)

    record = records[0]
    assert record["t_s"] == 12.0
    assert record["agent_said_s"] == 0.0
    assert record["user_speech_s"] == 0.5
    assert record["user_speaking"] is False
    assert record["final_words"] == 1


def test_first_turn_pause_without_agent_state_uses_user_speech_start():
    tracker, clock, records = make_tracker(started_at=100.0)

    clock.now = 101.0
    tracker.on_user_state("listening", "speaking")
    clock.now = 101.6
    tracker.on_false_interruption(resumed=True)

    record = records[0]
    assert record["t_s"] == 1.0
    assert record["agent_said_s"] == 0.0
    assert record["user_speech_s"] == 0.6


def test_record_starts_with_seq_then_context_and_count_increments():
    context = {
        "room_name": "room-x",
        "nc_engine": "aic",
        "nc_model": "quail_l",
        "min_duration": 0.5,
        "min_words": 2,
    }
    tracker, clock, records = make_tracker(context=context)
    assert tracker.count == 0

    clock.now = 1.0
    tracker.on_false_interruption(resumed=True)
    clock.now = 2.0
    tracker.on_item_added("message", "assistant", True)

    assert tracker.count == 2
    for record in records:
        assert list(record)[: 1 + len(context)] == ["seq", *context]
        assert {key: record[key] for key in context} == context


def test_final_shortly_before_pause_is_counted_and_consumed_once():
    tracker, clock, records = make_tracker()

    clock.now = 0.0
    tracker.on_agent_state("thinking", "speaking")
    clock.now = 1.5
    tracker.on_transcript("여보세요", is_final=True)
    clock.now = 2.0
    tracker.on_user_state("listening", "speaking")
    clock.now = 2.6
    tracker.on_transcript("아니요", is_final=True, speaker_id="spk_0")
    clock.now = 2.8
    tracker.on_agent_state("speaking", "listening")
    clock.now = 3.0
    tracker.on_agent_state("listening", "speaking")
    tracker.on_false_interruption(resumed=True)

    # 2.6 s final (0.2 s before the pause) counts; 1.5 s final is too old
    assert records[0]["t_s"] == 2.8
    assert records[0]["final_words"] == 1
    assert records[0]["final_chars"] == 3
    assert records[0]["speaker_id"] == "spk_0"

    # The resumed speech is paused again within 1 s of the consumed final.
    clock.now = 3.2
    tracker.on_agent_state("speaking", "listening")
    clock.now = 3.4
    tracker.on_item_added("message", "assistant", True)

    assert records[1]["t_s"] == 3.2
    assert records[1]["final_words"] == 0


def test_stale_pause_from_natural_end_is_replaced_by_user_speech_start():
    """SDK 는 재생 전에 끼어든 발화(SOS)에는 상태 변경 없이 오디오만 멈춘다.

    저장된 pause(이전 턴의 자연 종료)보다 사용자 발화가 늦게 시작했으면 그 pause 는
    이번 끊김의 것이 아니다. 발화 시작을 pause 로 보고 이전 턴 값은 쓰지 않는다.
    """
    tracker, clock, records = make_tracker()

    clock.now = 5.0
    tracker.on_agent_state("thinking", "speaking")
    clock.now = 9.5
    tracker.on_user_state("listening", "speaking")  # backchannel, no interruption
    clock.now = 9.8
    tracker.on_transcript("네", is_final=False)
    clock.now = 10.0
    tracker.on_agent_state("speaking", "listening")  # natural end, never resolved
    clock.now = 10.2
    tracker.on_user_state("speaking", "listening")
    clock.now = 11.5
    tracker.on_agent_state("listening", "thinking")
    clock.now = 12.0
    tracker.on_user_state("listening", "speaking")
    clock.now = 12.8
    tracker.on_false_interruption(resumed=True)

    assert len(records) == 1
    record = records[0]
    assert record["outcome"] == "false_resumed"
    assert record["t_s"] == 12.0
    assert record["agent_said_s"] == 0.0
    assert record["user_speech_s"] == 0.8
    assert record["user_speaking"] is True
    assert record["interim_words"] == 0  # the stale pause's snapshot is not used


def test_user_speech_before_first_playout_without_any_pause():
    tracker, clock, records = make_tracker()

    clock.now = 1.0
    tracker.on_user_state("listening", "speaking")
    clock.now = 1.4
    tracker.on_user_state("speaking", "listening")
    clock.now = 1.9
    tracker.on_false_interruption(resumed=True)

    assert len(records) == 1
    record = records[0]
    assert record["t_s"] == 1.0
    assert record["agent_said_s"] == 0.0
    assert record["user_speech_s"] == 0.4
    assert record["user_speaking"] is False


def test_finals_are_bounded_to_most_recent_twenty():
    tracker, clock, records = make_tracker(log_text=True)

    clock.now = 5.0
    for index in range(25):
        tracker.on_transcript(f"w{index}", is_final=True)
    tracker.on_false_interruption(resumed=False)

    assert records[0]["final_words"] == 20
    assert records[0]["text"].split()[0] == "w5"


def test_default_emit_logs_interruption_event(log_records):
    tracker = InterruptionTracker(
        started_at=0.0, clock=FakeClock(2.0), context={"room_name": "room-y"}
    )

    tracker.on_false_interruption(resumed=True)

    events = [
        record
        for record in log_records
        if record["extra"].get("event") == agent_interruption.INTERRUPTION_EVENT
    ]
    assert len(events) == 1
    assert events[0]["level"].name == "INFO"
    assert events[0]["extra"]["room_name"] == "room-y"
    assert events[0]["extra"]["outcome"] == "false_resumed"
    assert events[0]["extra"]["t_s"] == 2.0


# ------------------------------------------------------------------- bind


def test_bind_registers_handlers_and_never_raises(log_records):
    handlers: dict[str, list] = {}

    class FakeSession:
        def on(self, name):
            def deco(fn):
                handlers.setdefault(name, []).append(fn)
                return fn

            return deco

    def fire(name: str, event: Any) -> None:
        for fn in handlers[name]:
            fn(event)

    tracker, clock, records = make_tracker()
    agent_interruption.bind_interruption_tracker(FakeSession(), tracker)

    assert set(handlers) == {
        "agent_state_changed",
        "user_state_changed",
        "user_input_transcribed",
        "agent_false_interruption",
        "conversation_item_added",
    }

    clock.now = 0.0
    fire(
        "agent_state_changed",
        SimpleNamespace(old_state="thinking", new_state="speaking"),
    )
    clock.now = 1.0
    fire(
        "user_state_changed",
        SimpleNamespace(old_state="listening", new_state="speaking"),
    )
    clock.now = 1.2
    fire(
        "user_input_transcribed",
        SimpleNamespace(transcript="잠깐만요", is_final=False, speaker_id=None),
    )
    clock.now = 1.3
    fire(
        "agent_state_changed",
        SimpleNamespace(old_state="speaking", new_state="listening"),
    )
    clock.now = 1.5
    fire(
        "user_input_transcribed",
        SimpleNamespace(transcript="잠깐만요", is_final=True, speaker_id="spk_0"),
    )
    clock.now = 1.6
    fire(
        "conversation_item_added",
        SimpleNamespace(
            item=SimpleNamespace(type="message", role="assistant", interrupted=True)
        ),
    )
    clock.now = 2.0
    fire("agent_false_interruption", SimpleNamespace(resumed=True))

    assert [record["outcome"] for record in records] == ["committed", "false_resumed"]
    assert records[0]["t_s"] == 1.3
    assert records[0]["interim_words"] == 1
    assert records[0]["final_words"] == 1
    assert records[0]["speaker_id"] == "spk_0"

    class Exploding:
        @property
        def item(self):
            raise RuntimeError("boom")

    # Events with missing attributes or exploding accessors are swallowed.
    fire("conversation_item_added", Exploding())
    fire("conversation_item_added", SimpleNamespace())
    for name in handlers:
        fire(name, object())

    def broken_emit(record: dict[str, Any]) -> None:
        raise RuntimeError("sink down")

    broken = InterruptionTracker(started_at=0.0, clock=FakeClock(), emit=broken_emit)
    handlers.clear()
    agent_interruption.bind_interruption_tracker(FakeSession(), broken)
    fire("agent_false_interruption", SimpleNamespace(resumed=False))

    errors = [
        record
        for record in log_records
        if record["extra"].get("event") == "NC_INTERRUPTION_HANDLER_ERROR"
    ]
    assert len(errors) == 2  # exploding accessor + failing sink
    assert all(record["level"].name == "DEBUG" for record in errors)
