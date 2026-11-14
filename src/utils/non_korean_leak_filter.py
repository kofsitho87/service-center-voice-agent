"""한국어 음성 응답에서 비한국어 누출이 시작된 문장부터 제거한다.

문장 경계가 확정될 때까지 현재 문장만 보류한다. 한글·라틴 외의 문자는
도착 즉시 검사하므로, 미완성 문장에 섞인 외국어도 발화되지 않는다.
"""

import re
import unicodedata
from dataclasses import dataclass

FALLBACK_UTTERANCE = "죄송합니다. 다시 한 번 말씀해 주시겠어요?"

_ASCII_WORD = re.compile(r"[A-Za-z]+")
_ACRONYM = re.compile(r"[A-Z]{2,4}")
_LOWERCASE = re.compile(r"[a-z]")
_ABBREVIATION = re.compile(r"(?:^|\s)(?:Dr|Mr|Ms|Mrs|Prof)\.$")
_TOOL_MARKERS = ("to=functions.", "】【", "<|")
_BOUNDARIES = frozenset("?!。\uff1f\uff01\n")


@dataclass(frozen=True)
class LeakFilterResult:
    """필터링한 발화와 최초 누출의 원인 및 제거량을 담는다."""

    text: str
    reason: str | None
    script: str | None
    dropped_chars: int
    fallback: bool


def _has_hangul(text: str) -> bool:
    """완성형과 자모를 포함한 한글 문자가 있는지 확인한다."""
    return any("HANGUL" in unicodedata.name(char, "") for char in text)


def _is_ascii_alphanumeric(char: str) -> bool:
    return char.isascii() and char.isalnum()


def _sentence_reason(sentence: str) -> str | None:
    """완성된 문장의 도구 표식과 라틴 문자 누출을 우선순위대로 검사한다."""
    if any(marker in sentence for marker in _TOOL_MARKERS):
        return "tool_marker"

    if not _has_hangul(sentence):
        words = _ASCII_WORD.findall(sentence)
        if words and not all(_ACRONYM.fullmatch(word) for word in words):
            return "latin_sentence"
        return None

    run_length = 0
    run_has_lowercase = False
    for token in sentence.split():
        if _ASCII_WORD.search(token) and not _has_hangul(token):
            run_length += 1
            run_has_lowercase |= bool(_LOWERCASE.search(token))
            if run_length >= 3 and run_has_lowercase:
                return "latin_run"
        else:
            run_length = 0
            run_has_lowercase = False
    return None


class NonKoreanLeakFilter:
    """확정된 정상 문장만 내보내는 한국어 응답 스트림 필터."""

    def __init__(self, *, language: str | None):
        self._active = bool(language and language.lower().startswith("ko"))
        self._input_chars = 0
        self._sentence: list[str] = []
        self._sentence_first: int | None = None
        self._pending_period = False
        self._whitespace = ""
        self._emitted: list[str] = []
        self._reason: str | None = None
        self._script: str | None = None
        self._leak_start: int | None = None
        self._result: LeakFilterResult | None = None

    def _mark_leak(self, reason: str, script: str | None = None) -> None:
        self._reason = reason
        self._script = script
        self._leak_start = self._sentence_first
        self._sentence.clear()
        self._pending_period = False

    def _complete_sentence(self) -> str:
        sentence = "".join(self._sentence)
        reason = _sentence_reason(sentence)
        if reason is not None:
            self._mark_leak(reason)
            return ""

        self._sentence.clear()
        self._sentence_first = None
        self._pending_period = False
        # 공백만 남겼다면 이후 누출 시 대체 발화 앞에 공백을 내보내지 않는다.
        if not sentence.strip():
            self._whitespace += sentence
            return ""
        emitted = self._whitespace + sentence
        self._whitespace = ""
        self._emitted.append(emitted)
        return emitted

    def push(self, chunk: str) -> str:
        """새 청크를 검사하고 이제 발화해도 되는 문장들을 반환한다."""
        if self._result is not None:
            raise RuntimeError("Cannot push after finish()")
        chunk_start = self._input_chars
        self._input_chars += len(chunk)
        if not self._active:
            self._emitted.append(chunk)
            return chunk
        if self._reason is not None:
            return ""

        output: list[str] = []
        for index, char in enumerate(chunk):
            # 마침표는 다음 문자가 와야 소수점·주소 내부인지 알 수 있다.
            if self._pending_period:
                previous = self._sentence[-2] if len(self._sentence) >= 2 else ""
                if not (
                    _is_ascii_alphanumeric(previous) and _is_ascii_alphanumeric(char)
                ):
                    output.append(self._complete_sentence())
                    if self._reason is not None:
                        break
                self._pending_period = False

            self._sentence.append(char)
            if self._sentence_first is None and not char.isspace():
                self._sentence_first = chunk_start + index

            name = unicodedata.name(char, "")
            if (
                unicodedata.category(char).startswith("L")
                and "HANGUL" not in name
                and "LATIN" not in name
            ):
                # 외국 문자는 문장 끝을 기다리지 않고 즉시 누출로 확정한다.
                self._mark_leak("foreign_script", name.split()[0].lower())
                break

            if char in _BOUNDARIES:
                output.append(self._complete_sentence())
                if self._reason is not None:
                    break
            elif char == ".":
                self._pending_period = not _ABBREVIATION.search("".join(self._sentence))
        return "".join(output)

    def finish(self) -> str:
        """남은 문장을 검사하고 최종 결과 또는 필요한 대체 발화를 확정한다."""
        if self._result is not None:
            return ""
        output = ""
        if self._active and self._reason is None:
            output = self._complete_sentence()
            if self._reason is None and self._whitespace:
                output += self._whitespace
                self._emitted.append(self._whitespace)
                self._whitespace = ""

        text = "".join(self._emitted)
        fallback = False
        dropped_chars = 0
        if self._reason is not None:
            text = text.rstrip()
            if not text:
                text = FALLBACK_UTTERANCE
                output = FALLBACK_UTTERANCE
                fallback = True
            assert self._leak_start is not None
            dropped_chars = self._input_chars - self._leak_start

        self._result = LeakFilterResult(
            text=text,
            reason=self._reason,
            script=self._script,
            dropped_chars=dropped_chars,
            fallback=fallback,
        )
        return output

    @property
    def result(self) -> LeakFilterResult:
        """finish 호출로 확정한 필터링 결과를 반환한다."""
        if self._result is None:
            raise RuntimeError("Call finish() before reading result")
        return self._result


def filter_non_korean_leak(text: str, *, language: str | None) -> LeakFilterResult:
    """스트림 필터와 동일한 규칙으로 전체 발화를 한 번에 검사한다."""
    leak_filter = NonKoreanLeakFilter(language=language)
    leak_filter.push(text)
    leak_filter.finish()
    return leak_filter.result
