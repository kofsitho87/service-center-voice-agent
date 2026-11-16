"""한국어 통화에서 LLM 발화에 섞여 나온 비한국어 텍스트 제거 규칙 테스트.

gpt-5.6-luna 가 한국어 발화 뒤나 중간에 내부 추론 메모나 다른 언어 단어
(영어·키릴·구자라트·데바나가리·칸나다·한자 등)를 섞어 TTS 로 읽히는 문제를
막는다. 누출 부분은 실제로 관찰되는 누출 패턴을 본떠 만든 예시이고, 케이스
ID 는 각 예시가 고정하는 규칙이나 경계를 나타낸다.

규칙: 처음 걸린 문장부터 턴 끝까지 버린다. 한 문장이 여러 규칙에 걸리면
아래 순서가 앞선 쪽이 reason 이 된다.

1. ``foreign_script``: 한글·라틴 외 문자가 1자라도 있다.
2. ``tool_marker``: ``to=functions.`` · ``】【`` · ``<|`` 가 있다.
3. ``latin_sentence``: 한글 없는 문장에 영어 단어가 1개 이상 있다.
   (영어가 전부 2~4자 대문자 약어면 통과: ``OK``, ``SSD``)
4. ``latin_run``: 한글 문장 안에 영어 토큰이 공백으로 3개 이상 이어진다.
   (전부 대문자인 이름·약어 run 은 통과: ``PARK HYE JUNG``)

문장 분리: ``?`` ``!`` ``。`` 뒤는 항상, ``.`` 뒤는 앞뒤가 모두 영문/숫자가
아닐 때(``2.5``, ``co.kr`` 유지), 줄바꿈. ``Dr.`` ``Mr.`` ``Ms.`` ``Mrs.``
``Prof.`` 뒤는 나누지 않는다.

지운 뒤 남은 텍스트가 없으면 FALLBACK 문구를 말한다. 도구 호출 청크는 이
필터의 범위가 아니다.
"""

import pytest

from utils.non_korean_leak_filter import (
    FALLBACK_UTTERANCE,
    NonKoreanLeakFilter,
    filter_non_korean_leak,
)

KO = "ko-KR"

# 바뀌면 안 되는 발화.
KEEP_CASES = [
    pytest.param("화면이 많이 깨졌군요. 점검 예약을 잡아드릴까요?", id="pure-korean"),
    pytest.param(
        "본 통화는 AI 응대로 진행되며, 통화 내용은 녹음됩니다.", id="acronym-ai"
    ),
    pytest.param("LCD 패널은 점검 후 교체해요.", id="acronym-lcd"),
    pytest.param("SSD와 PC 점검 모두 가능합니다.", id="acronym-ssd-pc"),
    pytest.param(
        "WANG YUAN 고객님, 예약이 확인되었습니다.", id="latin-name-two-tokens"
    ),
    pytest.param(
        "PARK HYE JUNG 고객님 맞으실까요?", id="latin-name-three-upper-tokens"
    ),
    pytest.param("보호필름은 CLEAR Pro 제품으로 교체합니다.", id="product-two-tokens"),
    pytest.param(
        "자세한 내용은 www.example.co.kr 에서 보실 수 있어요.", id="url-one-token"
    ),
    pytest.param("오후 2시 30분, 3시, 4시가 가능합니다.", id="numbers-only"),
    pytest.param("보호 케이스는 3.5mm 두께부터 있어요.", id="decimal-not-split"),
    pytest.param("Mr. Smith 상담사님으로 예약할까요?", id="title-abbreviation"),
    pytest.param("잠시만요... 확인해 볼게요.", id="ellipsis"),
    pytest.param(
        "충전 단자가 헐거워진 경우에는 ‘휴대폰 점검 - 단자 접촉 불량 등 긴급 점검’으로 예약할 수 있습니다.",  # noqa: RUF001
        id="excerpt-quotes-and-dash",
    ),
    pytest.param("OK.", id="upper-acronym-sentence"),
    pytest.param("", id="empty"),
    # 한계: 한국어로 쓴 추론 메모는 잡을 수 없다.
    pytest.param("5개 일정 중 나머지 2개도 있음", id="korean-meta-note-undetectable"),
]

# (입력, 기대 출력, reason). 기대 출력이 FALLBACK_UTTERANCE 면 전부 지워진 경우다.
LEAK_CASES = [
    # 발화 끝에 붙은 누출.
    pytest.param(
        "어떤 점이 궁금하신지 말씀해 주세요. Battery diagnosis etc can answer limited.",
        "어떤 점이 궁금하신지 말씀해 주세요.",
        "latin_sentence",
        id="reasoning-note-at-end",
    ),
    pytest.param(
        "충전 후에 어떤 불편함이 있으신가요? battery etc.",
        "충전 후에 어떤 불편함이 있으신가요?",
        "latin_sentence",
        id="lowercase-note-after-question",
    ),
    pytest.param(
        "상담원과 바로 연결되지 않아 예약 변경 요청을 메모로 남기려고 합니다. "
        "상담원에게 전달할 내용을 말씀해 주세요. 예를 들면 가능한 예약 날짜나 "
        "변경하고 싶은 내용 등을 말씀하시면 됩니다. "
        "relays a message to a representative.\n",
        "상담원과 바로 연결되지 않아 예약 변경 요청을 메모로 남기려고 합니다. "
        "상담원에게 전달할 내용을 말씀해 주세요. 예를 들면 가능한 예약 날짜나 "
        "변경하고 싶은 내용 등을 말씀하시면 됩니다.",
        "latin_sentence",
        id="english-paraphrase-at-end",
    ),
    pytest.param(
        "소형기기 점검으로 예약을 도와드릴까요? afspraak",
        "소형기기 점검으로 예약을 도와드릴까요?",
        "latin_sentence",
        id="single-word",
    ),
    pytest.param(
        "원하시는 날짜와 시간을 말씀해 주세요. निश्चित? No, Korean only.",
        "원하시는 날짜와 시간을 말씀해 주세요.",
        "foreign_script",
        id="devanagari",
    ),
    pytest.param(
        "휴대폰 점검으로 접수할게요. 10일은 어려우시다는 뜻으로 이해했는데, "
        "언제가 괜찮으세요? дату or time?",
        "휴대폰 점검으로 접수할게요. 10일은 어려우시다는 뜻으로 이해했는데, "
        "언제가 괜찮으세요?",
        "foreign_script",
        id="cyrillic",
    ),
    pytest.param(
        "10월 6일 오후 5시는 자리가 없습니다. 같은 날에는 오후 2시 30분, 3시, "
        "4시가 가능합니다. 이 중 어느 시간이 좋으세요? სხვა",
        "10월 6일 오후 5시는 자리가 없습니다. 같은 날에는 오후 2시 30분, 3시, "
        "4시가 가능합니다. 이 중 어느 시간이 좋으세요?",
        "foreign_script",
        id="georgian",
    ),
    pytest.param(
        "두 예약 모두 취소하시겠다는 뜻인가요, 아니면 다른 말씀을 하신 걸까요? 国产",
        "두 예약 모두 취소하시겠다는 뜻인가요, 아니면 다른 말씀을 하신 걸까요?",
        "foreign_script",
        id="han",
    ),
    pytest.param(
        "김 상담사님과 이 상담사님 중 어느 분으로 예약할까요? "
        "ವೈದ್ಯ assistant to=functions.search_schedules",
        "김 상담사님과 이 상담사님 중 어느 분으로 예약할까요?",
        "foreign_script",
        id="kannada-and-tool-marker",
    ),
    pytest.param(
        "예약 가능한 시간을 말씀해 주세요. よろしくお願いします。",
        "예약 가능한 시간을 말씀해 주세요.",
        "foreign_script",
        id="japanese-kana",
    ),
    # 공백 없이 붙은 누출.
    pytest.param(
        "노트북 상담으로 예약하겠습니다. 원하시는 날짜와 시간대가 있으신가요?աբաթ",
        "노트북 상담으로 예약하겠습니다. 원하시는 날짜와 시간대가 있으신가요?",
        "foreign_script",
        id="armenian-glued",
    ),
    pytest.param(
        "예약 신청을 원하시는 날짜와 시간을 말씀해주세요.日時",
        "예약 신청을 원하시는 날짜와 시간을 말씀해주세요.",
        "foreign_script",
        id="han-glued-after-period",
    ),
    pytest.param(
        "그 날로 예약해 드릴까요?Walgreens",
        "그 날로 예약해 드릴까요?",
        "latin_sentence",
        id="latin-glued-after-question",
    ),
    pytest.param(
        "10월 16일 금요일 오전에는 9시 30분, 10시, 10시 30분이 가능합니다. "
        "이 중 어느 시간으로 예약 신청할까요?5개 일정 중 나머지 2개도 있음"
        "We need need handle user says remaining 2? They likely request all? "
        "We must not",
        "10월 16일 금요일 오전에는 9시 30분, 10시, 10시 30분이 가능합니다. "
        "이 중 어느 시간으로 예약 신청할까요?",
        "latin_run",
        id="korean-meta-glued",
    ),
    # 한국어 단어 자리에 외국어 단어가 들어간 경우: 그 문장부터 버린다.
    pytest.param(
        "휴대폰 정기점검으로 접수해 드릴게요. 언제 방문을 원하시나요? "
        "તારીખ와 시간대를 말씀해 주세요.",
        "휴대폰 정기점검으로 접수해 드릴게요. 언제 방문을 원하시나요?",
        "foreign_script",
        id="gujarati-date",
    ),
    pytest.param(
        "휴대폰 점검으로 예약하시겠어요? તારીખ과 시간도 함께 말씀해 주세요.",
        "휴대폰 점검으로 예약하시겠어요?",
        "foreign_script",
        id="gujarati-date-2",
    ),
    pytest.param(
        "노트북 상담으로 접수할게요. 언제 방문을 원하시나요? "
        "તારીખ/시간을 말씀해 주세요.",
        "노트북 상담으로 접수할게요. 언제 방문을 원하시나요?",
        "foreign_script",
        id="gujarati-date-slash",
    ),
    pytest.param(
        "노트북 상담 예약을 원하시는군요. 언제 방문을 원하세요? "
        "तारीख와 시간대를 말씀해 주세요.",
        "노트북 상담 예약을 원하시는군요. 언제 방문을 원하세요?",
        "foreign_script",
        id="devanagari-date",
    ),
    pytest.param(
        "네, 예약 날짜는 2026년 9월 23일입니다. ಇನ್ನ 더 필요한 안내가 있으실까요?",
        "네, 예약 날짜는 2026년 9월 23일입니다.",
        "foreign_script",
        id="kannada",
    ),
    pytest.param(
        "휴대폰 점검으로 예약하실까요? नियमित점검 목적이면 휴대폰 점검으로 접수합니다.",
        "휴대폰 점검으로 예약하실까요?",
        "foreign_script",
        id="devanagari-glued-to-hangul",
    ),
    pytest.param(
        "침수 기기 수리는 전화로 예약 신청이 어렵습니다. 예약 가능한 서비스는 휴대폰 점검, "
        "노트북 상담, 소형기기 점검, 배터리 점검, 화면 점검, 데이터 백업 상담입니다. "
        "इनमें 어떤 서비스를 예약하시겠어요?",
        "침수 기기 수리는 전화로 예약 신청이 어렵습니다. 예약 가능한 서비스는 휴대폰 점검, "
        "노트북 상담, 소형기기 점검, 배터리 점검, 화면 점검, 데이터 백업 상담입니다.",
        "foreign_script",
        id="question-sentence-lost",
    ),
    pytest.param(
        "어떤 서비스를 원하시는지 말씀해 주세요. उदाहरण: 화면이 깨졌어요, 배터리가",
        "어떤 서비스를 원하시는지 말씀해 주세요.",
        "foreign_script",
        id="devanagari-example",
    ),
    pytest.param(
        "노트북 상담으로 예약하시네요. 원하시는 날짜나 시간대가 있으실까요? ವೈದ್ಯಕೀಯ 상담? \n",
        "노트북 상담으로 예약하시네요. 원하시는 날짜나 시간대가 있으실까요?",
        "foreign_script",
        id="kannada-2",
    ),
    pytest.param(
        "તારીખ와 시간을 말씀해 주세요.",
        FALLBACK_UTTERANCE,
        "foreign_script",
        id="leak-in-first-sentence",
    ),
    # 추론 메모 안에 한국어 인용이 섞인 경우: 누출 이후는 한국어여도 버린다.
    pytest.param(
        '예약하실 날짜와 시간을 말씀해 주세요. flexible condition? Korean. "상담 '
        "가능한 날짜와 시간을 정해 주세요. 예를 들어 평일 오후처럼 가능한 조건으로 "
        '말씀하셔도 됩니다." But if they said 상담은 상담, likely unclear. Keep.',
        "예약하실 날짜와 시간을 말씀해 주세요.",
        "latin_sentence",
        id="reasoning-with-korean-quote",
    ),
    pytest.param(
        "휴대폰 점검과 노트북 상담 중 어떤 서비스로 예약할까요? Repair-related "
        "follow-up is not clearly listed. Could say if 액정 교체 후 점검, "
        "general? We should not invent. Ask one.",
        "휴대폰 점검과 노트북 상담 중 어떤 서비스로 예약할까요?",
        "latin_sentence",
        id="reasoning-after-question",
    ),
    pytest.param(
        "희망하시는 날짜와 시간을 말씀해 주세요. ближай? Korean only, avoid "
        'Russian. Could ask "가장 빠른 일정". They affirmed appointment. Need no '
        "search until date or earliest booking intent? They haven't said booking? "
        "yes. Procedure says",
        "희망하시는 날짜와 시간을 말씀해 주세요.",
        "foreign_script",
        id="cyrillic-then-reasoning",
    ),
    pytest.param(
        "액정 교체 예약으로 도와드릴게요. 언제 방문을 원하시나요? તારીખ? Need "
        'Korean only, typo slipped. use "날짜와 시간". \n',
        "액정 교체 예약으로 도와드릴게요. 언제 방문을 원하시나요?",
        "foreign_script",
        id="gujarati-then-reasoning",
    ),
    pytest.param(
        '점검 예약 날짜와 시간을 말씀해 주세요. Kef? Need Korean only. "원하시는 '
        '날짜와 시간" one question.',
        "점검 예약 날짜와 시간을 말씀해 주세요.",
        "latin_sentence",
        id="short-latin-word-then-reasoning",
    ),
    pytest.param(
        "침수 기기 수리는 외관 점검과 내부 진단 등 상태 확인 후 수리 여부와 방법을 "
        "정합니다. 부식 정도나 메인보드 상태에 따라 부품 교체 또는 보드 수리가 될 "
        "수 있어요.  \nFresh appointment? Could offer booking only if wants. But "
        'end answer naturally. Use Korean. Ensure not "사랑 갈치". \u2003\n',
        "침수 기기 수리는 외관 점검과 내부 진단 등 상태 확인 후 수리 여부와 방법을 "
        "정합니다. 부식 정도나 메인보드 상태에 따라 부품 교체 또는 보드 수리가 될 "
        "수 있어요.",
        "latin_sentence",
        id="newline-separated",
    ),
    pytest.param(
        "현재 전화 연결은 계속 진행되지 않지만, 상담원에게 전화 통화를 원하신다는 "
        "내용을 전달할 수 있습니다. 추가로 남기실 내용이 있으면 말씀해 주세요. "
        'relayed message or refusal? They gave meaningful content "통화 요청". '
        "Since asked what additional, this is meaningful. Must call tool now. "
        '"예약 변경 요청입니다." But intent already relayed, message additional. '
        "Call. However prior assistant asked exact question. Yes.】【。%timeout "
        "to=functions.collect_leave_message.commented 】【：】【“】【"  # noqa: RUF001
        'message":"예약 변경 요청입니다."}',
        "현재 전화 연결은 계속 진행되지 않지만, 상담원에게 전화 통화를 원하신다는 "
        "내용을 전달할 수 있습니다. 추가로 남기실 내용이 있으면 말씀해 주세요.",
        "latin_sentence",
        id="anonymized-tool-call-in-text",
    ),
    pytest.param(
        "점검으로 예약하겠습니다. 원하시는 날짜와 시간이 있으실까요? datetime 없이 "
        "조회 가능, but need ask because booking intent no date per instructions "
        "says earliest_for_booking if no date/time. Yet likely call search "
        "earliest_for_booking immediately, not ask. We have service 5947, date "
        "range. Must tool same response. Use earliest_for_booking. Note tool "
        "previous weird unavailable create tool, now use search. Don't speak "
        "alongside? call.",
        "점검으로 예약하겠습니다. 원하시는 날짜와 시간이 있으실까요?",
        "latin_run",
        id="latin-run-in-korean-sentence",
    ),
    # 도구 호출 형식 표식.
    pytest.param(
        "원하시는 상담사님을 말씀해 주세요. 상담사님 to=functions.search_schedules",
        "원하시는 상담사님을 말씀해 주세요.",
        "tool_marker",
        id="tool-marker-in-korean-sentence",
    ),
    pytest.param(
        "예약을 도와드릴게요.<|channel|>commentary",
        "예약을 도와드릴게요.",
        "tool_marker",
        id="tool-marker-precedes-latin-sentence",
    ),
    # 영어를 쓰는 고객에게도 같은 규칙을 적용한다.
    pytest.param(
        "Yes, English consultation and appointment are available. How may I help you?",
        FALLBACK_UTTERANCE,
        "latin_sentence",
        id="english-only-reply",
    ),
    pytest.param(
        "죄송하지만 한국어로만 안내해 드리고 있습니다. "
        "Korean-speaking assistance is available.",
        "죄송하지만 한국어로만 안내해 드리고 있습니다.",
        "latin_sentence",
        id="english-translation",
    ),
    pytest.param(" (no", FALLBACK_UTTERANCE, "latin_sentence", id="fragment-only"),
    # 감수하는 오탐: 한글 문장 안의 3단어 이상 대소문자 혼합 제품명.
    pytest.param(
        "네, 가능합니다. 점검 접수는 Example Device Care 센터에서 진행해요.",
        "네, 가능합니다.",
        "latin_run",
        id="accepted-false-positive-product-name",
    ),
]

SCRIPT_CASES = [
    pytest.param("말씀해 주세요. તારીખ와 시간", "gujarati", id="gujarati"),
    pytest.param("말씀해 주세요. निश्चित?", "devanagari", id="devanagari"),
    pytest.param("말씀해 주세요. ಇನ್ನ 더", "kannada", id="kannada"),
    pytest.param("말씀해 주세요. дату", "cyrillic", id="cyrillic"),
    pytest.param("말씀해 주세요.日時", "cjk", id="cjk"),
    pytest.param("말씀해 주세요. სხვა", "georgian", id="georgian"),
    pytest.param("말씀해 주세요?աբաթ", "armenian", id="armenian"),
    pytest.param("말씀해 주세요. よろしく", "hiragana", id="hiragana"),
    pytest.param("말씀해 주세요. Battery etc.", None, id="latin-has-no-script"),
]


def _stream(
    text: str, chunks: list[str] | None = None
) -> tuple[str, NonKoreanLeakFilter]:
    leak_filter = NonKoreanLeakFilter(language=KO)
    emitted = [leak_filter.push(chunk) for chunk in (chunks or [text])]
    emitted.append(leak_filter.finish())
    return "".join(emitted), leak_filter


def test_fallback_utterance_is_fixed_korean_phrase():
    assert FALLBACK_UTTERANCE == "죄송합니다. 다시 한 번 말씀해 주시겠어요?"


@pytest.mark.parametrize("text", KEEP_CASES)
def test_keeps_korean_speech_unchanged(text: str):
    result = filter_non_korean_leak(text, language=KO)

    assert result.text == text
    assert result.reason is None
    assert result.script is None
    assert result.dropped_chars == 0
    assert result.fallback is False


@pytest.mark.parametrize(("text", "expected", "reason"), LEAK_CASES)
def test_drops_from_first_leaking_sentence(text: str, expected: str, reason: str):
    result = filter_non_korean_leak(text, language=KO)

    assert result.text == expected
    assert result.reason == reason
    assert result.dropped_chars > 0
    assert result.fallback is (expected == FALLBACK_UTTERANCE)


@pytest.mark.parametrize(("text", "script"), SCRIPT_CASES)
def test_reports_unicode_script_of_first_foreign_letter(text: str, script: str | None):
    assert filter_non_korean_leak(text, language=KO).script == script


def test_dropped_chars_counts_from_leaking_sentence_to_end():
    leak = "Battery diagnosis etc can answer limited."

    result = filter_non_korean_leak(
        f"어떤 점이 궁금하신지 말씀해 주세요. {leak}", language=KO
    )

    assert result.dropped_chars == len(leak)


def test_fallback_counts_whole_reply_as_dropped():
    text = "Yes, English consultation and appointment are available."

    assert filter_non_korean_leak(text, language=KO).dropped_chars == len(text)


@pytest.mark.parametrize("language", ["en-US", None])
def test_non_korean_call_language_is_not_filtered(language: str | None):
    text = "Yes, we are open until 6 PM. How may I help you?"

    result = filter_non_korean_leak(text, language=language)

    assert result.text == text
    assert result.reason is None


def test_stream_passes_chunks_through_for_non_korean_call():
    leak_filter = NonKoreanLeakFilter(language="en-US")

    assert leak_filter.push("Yes, we are ") == "Yes, we are "
    assert leak_filter.push("open.") == "open."
    assert leak_filter.finish() == ""


# (청크, 각 push 직후까지 내보낸 누적 텍스트, finish 후 누적 텍스트, reason).
# 문장 경계가 확정된 문장만 내보낸다. 공백 처리는 구현에 맡기므로 rstrip 비교.
STREAM_CASES = [
    pytest.param(
        ["화면이 많이 ", "깨졌군요. ", "점검 예약을 ", "잡아드릴까요?"],
        [
            "",
            "화면이 많이 깨졌군요.",
            "화면이 많이 깨졌군요.",
            "화면이 많이 깨졌군요. 점검 예약을 잡아드릴까요?",
        ],
        "화면이 많이 깨졌군요. 점검 예약을 잡아드릴까요?",
        None,
        id="clean-sentences-emitted-at-boundary",
    ),
    pytest.param(
        ["가능합니다", ".", "日時"],
        ["", "", "가능합니다."],
        "가능합니다.",
        "foreign_script",
        id="period-at-chunk-end-waits-for-next-char",
    ),
    pytest.param(
        ["크기는 2", ".", "5mm입니다."],
        ["", "", ""],
        "크기는 2.5mm입니다.",
        None,
        id="decimal-across-chunks",
    ),
    pytest.param(
        ["말씀해 주세요. ", "Need"],
        ["말씀해 주세요.", "말씀해 주세요."],
        "말씀해 주세요.",
        "latin_sentence",
        id="unterminated-leak-judged-at-finish",
    ),
    pytest.param(
        ["말씀해 주세요. ", "Battery ", "diagnosis ", "etc."],
        ["말씀해 주세요."] * 4,
        "말씀해 주세요.",
        "latin_sentence",
        id="latin-leak-streamed",
    ),
    pytest.param(
        [
            "휴대폰 점검으로 예약하실까요? ",
            "नियमित",
            "점검 목적이면 휴대폰 점검으로 접수합니다.",
        ],
        ["휴대폰 점검으로 예약하실까요?"] * 3,
        "휴대폰 점검으로 예약하실까요?",
        "foreign_script",
        id="nothing-emitted-after-leak",
    ),
    pytest.param(
        ["원하시는 날짜와 시간대가 있으신가요?", "աբաթ"],
        ["원하시는 날짜와 시간대가 있으신가요?"] * 2,
        "원하시는 날짜와 시간대가 있으신가요?",
        "foreign_script",
        id="question-mark-emits-immediately",
    ),
    pytest.param(
        ["5개 일정 중 ", "나머지 2개도 있음", "We need ", "handle it."],
        ["", "", "", ""],
        FALLBACK_UTTERANCE,
        "latin_run",
        id="korean-head-of-leaking-sentence-never-emitted",
    ),
]


@pytest.mark.parametrize(
    ("chunks", "emitted_after_each_push", "final_text", "reason"), STREAM_CASES
)
def test_stream_emits_only_confirmed_clean_sentences(
    chunks: list[str],
    emitted_after_each_push: list[str],
    final_text: str,
    reason: str | None,
):
    leak_filter = NonKoreanLeakFilter(language=KO)
    emitted = ""

    for chunk, expected in zip(chunks, emitted_after_each_push, strict=True):
        emitted += leak_filter.push(chunk)
        assert emitted.rstrip() == expected

    emitted += leak_filter.finish()
    assert emitted.rstrip() == final_text
    assert leak_filter.result.reason == reason


def test_stream_finish_without_input_returns_nothing():
    leak_filter = NonKoreanLeakFilter(language=KO)

    assert leak_filter.finish() == ""
    assert leak_filter.result.fallback is False


@pytest.mark.parametrize(
    "text",
    [pytest.param(case.values[0], id=case.id) for case in KEEP_CASES + LEAK_CASES],
)
@pytest.mark.parametrize("chunking", ["whole", "per-char"])
def test_stream_matches_batch_result(text: str, chunking: str):
    chunks = list(text) if chunking == "per-char" else [text]

    streamed, leak_filter = _stream(text, chunks)
    batch = filter_non_korean_leak(text, language=KO)

    assert streamed.rstrip() == batch.text.rstrip()
    assert leak_filter.result.reason == batch.reason
    assert leak_filter.result.script == batch.script
    assert leak_filter.result.dropped_chars == batch.dropped_chars
    assert leak_filter.result.fallback == batch.fallback
