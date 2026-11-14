"""에이전트 발화가 주입된 FAQ 답변에서 나온 것인지 판정하는 문자열 매처.

FAQ 를 시스템 프롬프트에 직접 주입하면서 `[INFO_LOOKUP]` developer 태그를
남길 주체가 사라졌다. 이 모듈은 LLM 을 쓰지 않고, 완성된 에이전트 발화를
메모리에 있는 FAQ 답변 원문과 문자 n-gram 으로 겹쳐보는 것만으로 그 판정을
대신한다. 추론도 네트워크 호출도 없으므로 발화 직후 동기적으로 판정할 수 있고,
통화가 곧바로 끊겨도 태그가 누락되지 않는다.

## 왜 "질문" 이 아니라 "답변" 끼리 비교하나

- 거절 발화("안내 자료에는 해당 내용이 없어…")는 어떤 FAQ 원문과도 겹치지
  않으므로, 별도 예외 처리 없이 임계값 아래로 떨어진다. 질문 기준으로 맞추면
  "질문은 FAQ 와 비슷한데 실제로는 답하지 못한" 통화를 성공으로 오판한다.
- 발신자는 같은 질문을 제각각 표현하지만, 프롬프트가 "Answer ONLY from the
  matched entry's answer. Relay facts EXACTLY as written" 를 강제하므로 답변
  쪽 어휘 겹침이 훨씬 안정적이다.

## 왜 Jaccard 가 아니라 포함도(containment)인가

프롬프트가 "extract only the part that answers the question" 을 지시하므로
에이전트 발화는 FAQ 원문의 일부만 담는다. 대칭 유사도를 쓰면 긴 FAQ 항목이
부당하게 불리해지므로, "발화 n-gram 중 FAQ 답변에 포함된 비율" 을 쓴다.

임계값(`DEFAULT_THRESHOLD`)은 과거 통화 데이터로 실측해 조정할 것을 전제로 한
잠정값이다.
"""

import os
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Optional

# 문자 n-gram 크기. 한국어는 조사가 공백 없이 붙어 어절 단위 비교가 불안정하므로
# 2-gram 을 쓴다.
NGRAM_SIZE = 2

# 발화 n-gram 중 FAQ 답변에 포함된 비율의 하한.
#
# 아직 실데이터로 확정하지 못한 잠정값이다. 참고한 관측치:
#   - 합성 데이터: 정상 FAQ 응대 0.60~0.82, 비응대(인사·거절·예약) 0.06~0.14
#   - 실통화 1,816건(source calibration set): 비응대 최고 0.214
#   - 실통화 정상 응대: 0.300 / 0.615 (표본 2건)
# 0.300 사례는 FAQ 원문이 "5000원" 처럼 극단적으로 짧아, 대화체로 풀어 말한
# 발화가 원문에 담길 수 없는 경우다. FAQ 원문이 짧을수록 점수가 낮아진다.
#
# 통화 경로를 건드리지 않고 조정할 수 있도록 환경변수로 덮어쓸 수 있다.
DEFAULT_THRESHOLD = float(os.getenv("FAQ_MATCH_THRESHOLD", "0.65"))

# 정규화 후 이 길이 미만이면 판정하지 않는다. "네, 가능합니다" 같은 짧은 응답은
# 변별력이 없어 아무 항목에나 높은 점수로 걸린다.
MIN_NORMALIZED_LENGTH = 8

# 숫자는 TTS 규칙에 따라 발화에서 한글로 변환된다("5,000원" → "오천 원").
# 원문과 형태가 달라지므로 양쪽 모두에서 제거하고 비교한다.
_STRIP_PATTERN = re.compile(r"[\d\s]|[^\w]", re.UNICODE)


@dataclass(frozen=True)
class FaqMatchResult:
    """매칭 결과. 임계값 미달이어도 최고점 항목을 담아 튜닝/디버깅에 쓴다."""

    index: int  # 1-based. 프롬프트에 주입된 번호와 일치한다.
    score: float
    matched: bool


def normalize(text: str) -> str:
    """비교용 정규화: 유니코드 정규화 후 숫자·공백·구두점 제거."""
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", text)
    return _STRIP_PATTERN.sub("", normalized).lower()


def char_ngrams(text: str, n: int = NGRAM_SIZE) -> set[str]:
    """문자 n-gram 집합. 길이가 n 미만이면 문자열 자체를 단일 원소로 담는다."""
    if not text:
        return set()
    if len(text) < n:
        return {text}
    return {text[i : i + n] for i in range(len(text) - n + 1)}


def containment_score(utterance: str, faq_answer: str) -> float:
    """발화 n-gram 중 FAQ 답변에 포함된 비율(0.0 ~ 1.0).

    발화가 FAQ 답변의 일부만 옮긴 경우에도 높은 점수가 나오도록 방향을
    고정한다(발화 ⊆ FAQ). 반대 방향으로 계산하면 긴 FAQ 항목이 불리해진다.
    """
    utterance_grams = char_ngrams(normalize(utterance))
    if not utterance_grams:
        return 0.0

    answer_grams = char_ngrams(normalize(faq_answer))
    if not answer_grams:
        return 0.0

    return len(utterance_grams & answer_grams) / len(utterance_grams)


def match_faq_answer(
    utterance: str,
    faq_answers: Sequence[str],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    min_length: int = MIN_NORMALIZED_LENGTH,
) -> Optional[FaqMatchResult]:
    """발화가 어느 FAQ 답변에서 나왔는지 판정한다.

    Args:
        utterance: 완성된 에이전트 발화.
        faq_answers: 프롬프트에 주입된 순서와 동일한 FAQ 답변 목록.
        threshold: 이 값 이상이면 `matched=True`.
        min_length: 정규화 후 이 길이 미만인 발화는 판정하지 않는다.

    Returns:
        최고점 항목의 `FaqMatchResult`. 판정 대상이 아니면(발화가 너무 짧거나
        FAQ 가 비어 있으면) None. 임계값 미달은 None 이 아니라
        `matched=False` 로 돌려주어 근사치를 로그로 남길 수 있게 한다.
    """
    if not faq_answers:
        return None
    if len(normalize(utterance)) < min_length:
        return None

    best_index = 0
    best_score = 0.0
    for index, answer in enumerate(faq_answers, start=1):
        score = containment_score(utterance, answer)
        if score > best_score:
            best_index = index
            best_score = score

    if best_index == 0:
        return None

    return FaqMatchResult(
        index=best_index,
        score=best_score,
        matched=best_score >= threshold,
    )
