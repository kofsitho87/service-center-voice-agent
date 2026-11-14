"""TTS 입력 텍스트용 커스텀 transform 모음.

LiveKit 기본 transform(``filter_markdown``)은 볼드/이탤릭 정규식이 단어 경계
룩어라운드(``(?![\\w*])``)를 쓰는데, Python ``re``의 ``\\w``는 유니코드 기본이라
한글도 단어문자로 취급한다. 한국어는 조사가 공백 없이 붙으므로
``**날짜와 시간**은`` 처럼 닫는 ``**`` 뒤에 조사가 오면 패턴이 매칭되지 않아
별표가 그대로 남고, TTS가 "별표별표"로 발화한다.

여기서는 내장 ``filter_markdown`` 뒤에 체이닝하여, 조사에 붙어 새어나온
강조용 마크다운 심볼만 후처리로 제거한다.
"""

import re
from collections.abc import AsyncIterable

from livekit.agents import text_transforms

# 강조/코드용 마크다운 마커.
# - ``*`` (1~3개): 볼드/이탤릭. 조사가 붙는 한글에서 내장 필터가 가장 자주 놓친다.
# - ``_`` (2~3개): ``__bold__``/``___bolditalic___``. 단일 ``_``는 ID/이메일 등
#   정상 표기일 수 있어 제거하지 않는다.
# - ``~~``: 취소선. (범위 표기용 단일 ``~``는 건드리지 않는다.)
# - 백틱(1~3개): 인라인/블록 코드. 음성 안내에 정상적으로 등장하지 않는다.
_MARKDOWN_MARKERS = re.compile(r"\*{1,3}|_{2,3}|~~|`{1,3}")


async def strip_markdown_symbols(text: AsyncIterable[str]) -> AsyncIterable[str]:
    """조사에 붙어 새어나온 강조용 마크다운 심볼을 제거한다.

    ``*``/백틱/``~~`` 는 단일 문자(또는 인접 반복) 단위로 제거되므로 청크 경계로
    ``**`` 가 쪼개져도(예: ``...*`` + ``*은...``) 각 청크에서 개별 제거되어 안전하다.
    내장 ``filter_markdown`` 다음에 체이닝하는 것을 전제로 한다.
    """
    async for chunk in text:
        yield _MARKDOWN_MARKERS.sub("", chunk)


# 시각의 ``시``는 고유어 수 관형형으로 읽는다(1시 -> 한 시, 2시 -> 두 시).
# 13~24시는 24시간제 표기를 보존해 한자어 수로 읽는다. 유효한 시각 전체를
# 등록하면 ``21시`` 안의 ``1시``만 부분 치환되는 문제도 피할 수 있다.
_KOREAN_HOUR_READINGS = {
    0: "영",
    1: "한",
    2: "두",
    3: "세",
    4: "네",
    5: "다섯",
    6: "여섯",
    7: "일곱",
    8: "여덟",
    9: "아홉",
    10: "열",
    11: "열한",
    12: "열두",
    13: "십삼",
    14: "십사",
    15: "십오",
    16: "십육",
    17: "십칠",
    18: "십팔",
    19: "십구",
    20: "이십",
    21: "이십일",
    22: "이십이",
    23: "이십삼",
    24: "이십사",
}

_KOREAN_HOUR_REPLACEMENTS = {
    notation: f"{spoken} 시"
    for hour, spoken in _KOREAN_HOUR_READINGS.items()
    for notation in {
        f"{hour}시",
        f"{hour} 시",
        f"{hour:02d}시",
        f"{hour:02d} 시",
    }
}

# LiveKit의 replace transform은 치환 키가 스트리밍 청크 경계에서 나뉘어도
# 접미부를 버퍼링한다. 따라서 ``"2"`` + ``"시"`` 입력도 ``"두 시"``가 된다.
normalize_korean_hours = text_transforms.replace(
    _KOREAN_HOUR_REPLACEMENTS,
    case_sensitive=True,
)
