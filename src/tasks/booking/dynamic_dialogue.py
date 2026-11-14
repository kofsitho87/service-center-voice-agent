"""Dynamic booking entry facts read from the inherited dialogue.

Whether there is dialogue to continue and whether the caller already stated a
booking condition are countable facts, so code decides them, not the prompt.
"""

from __future__ import annotations

import re
from typing import Any


def _has_prior_dialogue(chat_ctx: Any) -> bool:
    """진입 시점 chat_ctx에 이어받을 대화가 실제로 있는지.

    인사말 분기를 LLM 판단에 맡기면 "chat_ctx가 비어 있는지 확인하세요"를 어기고
    첫 응대용 정본 인사말을 그대로 내보내는 일이 생긴다 — 통화 43902에서 고객이
    이미 "다음 주 수요일에 예약하고 싶어요"라고 말한 뒤인데도 "예약
    도와드리겠습니다"로 응대를 새로 열었다. 비었는지 여부는 코드가 셀 수 있는
    사실이므로 여기서 정하고, 프롬프트에는 해당하는 한쪽 지시문만 넣는다.
    """
    items = getattr(chat_ctx, "items", None)
    if not items:
        return False
    return any(
        getattr(item, "type", None) == "message"
        and getattr(item, "role", None) in ("user", "assistant")
        and (getattr(item, "text_content", None) or "").strip()
        for item in items
    )


# 예약 조건을 담을 수 없는 단독 응답. DTMF 확인 절차의 정답이 대부분 여기 속한다.
_ACKNOWLEDGEMENT_ONLY_UTTERANCES = frozenset(
    {
        "네",
        "네네",
        "넵",
        "넹",
        "예",
        "예예",
        "응",
        "어",
        "음",
        "맞아",
        "맞어",
        "맞아요",
        "맞습니다",
        "그래",
        "그래요",
        "좋아",
        "좋아요",
        "아니",
        "아뇨",
        "아니요",
        "아니오",
        "아닙니다",
    }
)

_UTTERANCE_NOISE = re.compile(r"""[\s.,!?~…·'"''""]""")


def _has_condition_bearing_user_utterance(chat_ctx: Any) -> bool:
    """고객이 예약 조건을 담았을 수 있는 발화를 실제로 했는지.

    `_has_prior_dialogue` 는 assistant 안내 발화까지 세므로, DTMF 선택과
    확인번호 확인만 거친 진입도 "이어받을 대화 있음" 으로 판정된다. 그 상태에서
    조건을 읽어 조회하라는 continuation 지시문이 들어가면 읽을 조건이 없는
    모델이 프롬프트에 있는 날짜(달력의 오늘)를 인자로 지어낸다 — 통화 61402 는
    고객 발화가 확인 응답 "네" 하나뿐인데도 진입 턴에 당일 조회가 나갔고,
    빈 결과를 "오늘 예약 가능한 시간이 없습니다" 로 안내한 뒤 끊겼다.

    그래서 인사말 생략 여부(`_has_prior_dialogue`)와 조회 지시 여부(이 함수)를
    분리한다. 확인 응답 단독 발화는 어떤 예약 조건도 담을 수 없으므로 조건
    발화로 세지 않는다. 고객이 "오늘 예약해주세요" 처럼 실제 조건을 말한
    경우에는 True 가 되어 진입 턴 조회가 그대로 살아 있다.
    """
    items = getattr(chat_ctx, "items", None)
    if not items:
        return False
    for item in items:
        if getattr(item, "type", None) != "message":
            continue
        if getattr(item, "role", None) != "user":
            continue
        text = (getattr(item, "text_content", None) or "").strip()
        if not text:
            continue
        if _UTTERANCE_NOISE.sub("", text) in _ACKNOWLEDGEMENT_ONLY_UTTERANCES:
            continue
        return True
    return False
