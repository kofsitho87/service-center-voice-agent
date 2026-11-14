"""TTS 입력 텍스트 transform 테스트."""

import pytest

from utils.tts_text_filters import normalize_korean_hours


async def _chunks(*values: str):
    for value in values:
        yield value


async def _normalize(*values: str) -> str:
    return "".join([chunk async for chunk in normalize_korean_hours(_chunks(*values))])


@pytest.mark.asyncio
async def test_normalize_korean_hours_uses_native_hour_readings():
    result = await _normalize("점심시간은 오후 1시부터 2시까지예요.")

    assert result == "점심시간은 오후 한 시부터 두 시까지예요."


@pytest.mark.asyncio
async def test_normalize_korean_hours_handles_streaming_chunk_boundaries():
    result = await _normalize("점심시간은 오후 ", "1", "시부터 ", "2", "시까지예요.")

    assert result == "점심시간은 오후 한 시부터 두 시까지예요."


@pytest.mark.asyncio
async def test_normalize_korean_hours_handles_padded_and_24_hour_times():
    result = await _normalize("오전 09 시부터 21시까지")

    assert result == "오전 아홉 시부터 이십일 시까지"


@pytest.mark.asyncio
async def test_normalize_korean_hours_leaves_other_units_unchanged():
    result = await _normalize("2월 2일에 2분 동안 대기해 주세요.")

    assert result == "2월 2일에 2분 동안 대기해 주세요."
