"""무진행(no-progress) 가드 — 코드 소유 카운터와 개입 경계 검증.

사용자 발화가 저신뢰 추임새("네", "어")와 예약과 무관한 주변 대화 조각뿐이면
booking 툴이 한 번도 호출되지 않은 채 통화가 끝없이 이어질 수 있다. 프롬프트
규칙(반복 금지·되묻기)이나 exit_task 는 이 상황에서 발동이 보장되지 않는다.

가드는 판단을 LLM 에 맡기지 않는다 — 연속 무진행 턴 수는 코드가 세는 사실이다.
- 사용자 턴마다 +1, booking 툴 호출(조회·스테이징·커밋 시도)마다 0으로 리셋.
- SOFT 임계값: 그 턴의 LLM 입력에 예/아니오 강제 질문 지시문을 주입.
- HARD 임계값: LLM 응답 없이(StopResponse) exit 사유 no_progress 로 태스크를
  종료하고, 안내·통화 종료는 호출자(dynamic_booking_tools)가 수행한다.
"""

from types import SimpleNamespace

import pytest
from livekit.agents import StopResponse

from tasks.booking.exit_reasons import normalize_exit_reason_code
from tasks.dynamic_booking_task import (
    NO_PROGRESS_HARD_TURNS,
    NO_PROGRESS_SOFT_TURNS,
    DynamicBookingResult,
    DynamicBookingTask,
)


def _guard_task() -> DynamicBookingTask:
    task = object.__new__(DynamicBookingTask)
    task._call_session_data = SimpleNamespace(room_name="synthetic-no-progress")
    task._no_progress_turns = 0
    task.done = lambda: False
    return task


def _turn_ctx() -> SimpleNamespace:
    messages: list[SimpleNamespace] = []

    def add_message(*, role: str, content: str, **kwargs) -> None:
        messages.append(SimpleNamespace(role=role, content=content))

    return SimpleNamespace(add_message=add_message, messages=messages)


def _user_message(text: str = "네") -> SimpleNamespace:
    return SimpleNamespace(text_content=text)


async def _run_turns(task, turn_ctx, count: int) -> None:
    for _ in range(count):
        await task.on_user_turn_completed(turn_ctx, _user_message())


class TestSoftIntervention:
    @pytest.mark.asyncio
    async def test_below_soft_threshold_injects_nothing(self):
        task = _guard_task()
        turn_ctx = _turn_ctx()
        await _run_turns(task, turn_ctx, NO_PROGRESS_SOFT_TURNS - 1)
        assert turn_ctx.messages == []

    @pytest.mark.asyncio
    async def test_soft_threshold_injects_forced_choice_instruction(self):
        task = _guard_task()
        turn_ctx = _turn_ctx()
        await _run_turns(task, turn_ctx, NO_PROGRESS_SOFT_TURNS)
        assert len(turn_ctx.messages) == 1
        injected = turn_ctx.messages[0]
        assert injected.role == "system"
        assert "Do not re-ask" in injected.content
        assert "yes or no" in injected.content
        # 회귀 고정: 지시문의 배경 설명("통화 상태가 나쁠 수 있다")과 매 턴
        # 종료 예고를 주면 LLM 이 "통화가 잘 안 들리시면 다시 말씀해 주세요"처럼
        # 고객 발화로 그대로 읽는다. SOFT 구간 지시문은 통화 품질
        # 언급 금지를 담고, 종료 예고는 담지 않는다.
        assert "call quality" in injected.content
        assert "may be ended" not in injected.content

    @pytest.mark.asyncio
    async def test_injection_repeats_every_turn_until_hard_threshold(self):
        task = _guard_task()
        turn_ctx = _turn_ctx()
        await _run_turns(task, turn_ctx, NO_PROGRESS_HARD_TURNS - 1)
        assert len(turn_ctx.messages) == (
            NO_PROGRESS_HARD_TURNS - NO_PROGRESS_SOFT_TURNS
        )

    @pytest.mark.asyncio
    async def test_termination_notice_only_in_last_two_turns_before_hard(self):
        task = _guard_task()
        turn_ctx = _turn_ctx()
        await _run_turns(task, turn_ctx, NO_PROGRESS_HARD_TURNS - 1)
        with_notice = [m for m in turn_ctx.messages if "may be ended" in m.content]
        assert len(with_notice) == 2
        assert all("may be ended" in m.content for m in turn_ctx.messages[-2:])


class TestThresholds:
    def test_thresholds_match_product_decision(self):
        # 정상 날짜 협의도 달력 블록 문답으로 툴 없이 10턴을 채울 수 있어 5/10 은
        # 오탐 종료를 낸다. 그래서 20/30 이다.
        assert NO_PROGRESS_SOFT_TURNS == 20
        assert NO_PROGRESS_HARD_TURNS == 30


class TestHardTermination:
    @pytest.mark.asyncio
    async def test_hard_threshold_completes_no_progress_exit_without_reply(self):
        completed: list[DynamicBookingResult] = []
        task = _guard_task()
        task.complete = completed.append
        turn_ctx = _turn_ctx()
        await _run_turns(task, turn_ctx, NO_PROGRESS_HARD_TURNS - 1)
        assert completed == []

        with pytest.raises(StopResponse):
            await task.on_user_turn_completed(turn_ctx, _user_message())

        [result] = completed
        assert result.outcome == "exited"
        assert result.exit_reason_code == "no_progress"

    @pytest.mark.asyncio
    async def test_hook_is_inert_after_task_completion(self):
        # 커밋/이탈 직후 잔여 발화가 들어와도 이미 끝난 태스크에서 complete 를
        # 다시 호출하면 RuntimeError 다. done 이면 세지도 개입하지도 않는다.
        task = _guard_task()
        task.done = lambda: True
        turn_ctx = _turn_ctx()
        await _run_turns(task, turn_ctx, NO_PROGRESS_HARD_TURNS + 1)
        assert turn_ctx.messages == []
        assert task._no_progress_turns == 0


class TestProgressReset:
    @pytest.mark.asyncio
    async def test_search_call_resets_counter_even_on_invalid_args(self):
        # 툴 호출 자체가 "예약 조건이 담긴 대화가 진행 중" 이라는 관측 사실이다.
        # 인자 검증에 실패해도 리셋은 유지된다.
        task = _guard_task()
        task._no_progress_turns = NO_PROGRESS_HARD_TURNS - 1
        task._pending_earliest_offer = None
        result = await task.search_schedules(
            start_date="2026-09-01",
            end_date="2026-09-01",
            mode="earliest_for_booking",
        )
        assert result["status"] == "error"
        assert task._no_progress_turns == 0

    @pytest.mark.asyncio
    async def test_stage_call_resets_counter_even_on_unknown_service(self):
        task = _guard_task()
        task._no_progress_turns = NO_PROGRESS_HARD_TURNS - 1
        task._services = [{"id": 1, "name": "휴대폰 점검"}]
        task._advisors_by_id = {}
        ctx = SimpleNamespace(disallow_interruptions=lambda: None)
        result = await task.stage_booking(
            ctx, service_id=999, datetime="2026-09-01 10:00"
        )
        assert result["status"] == "error"
        assert task._no_progress_turns == 0

    @pytest.mark.asyncio
    async def test_reset_defers_soft_intervention_again(self):
        task = _guard_task()
        turn_ctx = _turn_ctx()
        await _run_turns(task, turn_ctx, NO_PROGRESS_SOFT_TURNS - 1)
        task._note_progress()
        await _run_turns(task, turn_ctx, NO_PROGRESS_SOFT_TURNS - 1)
        assert turn_ctx.messages == []


class TestNoProgressExitBoundary:
    def test_result_accepts_no_progress_exit(self):
        result = DynamicBookingResult(outcome="exited", exit_reason_code="no_progress")
        assert result.exit_reason_code == "no_progress"

    def test_llm_exit_tool_cannot_emit_no_progress(self):
        # no_progress 는 가드 전용 사유다. exit_task 의 화이트리스트에 없어
        # LLM 이 스키마를 우회해 전달해도 other 로 정규화된다.
        assert normalize_exit_reason_code("no_progress") == "other"
