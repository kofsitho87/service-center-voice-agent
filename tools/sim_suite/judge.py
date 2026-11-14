"""LLM 저지(레이어 2).

결정론적 체크가 잡지 못하는 대화 품질(근거 기반 안내, 확인 절차, 명시적
요청 수용, 종료 매너)을 루브릭으로 평가한다. 평가 범위는 **이번 세션의
대화 품질**로 한정한다 — 세션 간 자원 경합(같은 슬롯을 다른 통화가 먼저
가져가는 등)은 FAIL 사유가 아니다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .events import ParsedEvents
from .schema import Scenario

DEFAULT_JUDGE_MODEL = "openai:gpt-5.4-mini"

JUDGE_SYSTEM_PROMPT = """당신은 가상 전자기기 서비스센터 인바운드 AI 상담 에이전트의 통화 1건을 채점하는 엄격한 심사관입니다.

## 평가 범위
- 오직 이번 세션의 대화 품질만 평가합니다.
- 다음을 확인합니다.
  1. 근거 기반: 주어진 서비스센터 자료에 없는 정보를 지어내지 않았는가.
  2. 확인 절차: 예약을 확정하기 전에 고객이 동의한 내용을 정확히 확인했는가.
  3. 명시적 요청 수용: 고객이 분명히 요청한 것(상담원 연결, 취소, 종료 등)을
     지연 없이 수행했는가.
  4. 진행성: 같은 질문/안내를 불필요하게 반복하며 맴돌지 않았는가.
  5. 마무리: 추가 문의를 확인하고 정중하게 통화를 마쳤는가.
- 시나리오 루브릭이 주어지면 그 항목을 최우선 기준으로 삼습니다.
- FAIL은 반드시 구체적인 루브릭 항목 위반이나 고객 발화와의 직접 모순을 근거로
  해야 합니다. 루브릭이 요구하거나 허용한 행동을 일반적인 선호를 이유로 실패 처리하지
  마세요. 루브릭에 없는 새로운 금지 조건을 만들지 마세요.

## 평가 제외 (FAIL 사유가 아님)
- 여러 통화가 동시에 같은 예약 슬롯을 다투어 생긴 실패 등 세션 간 자원 경합.
- 텍스트 모드의 한계(DTMF 실제 톤 없음, 무음 재현 불가, warm transfer의 SIP
  연결이 실제로 성립하지 않음).
- 테스트 하네스가 발화하는 응답지연 종료, 콘솔 정리, 상태표시. 이 정보는 통화
  기록에서 제거되며 별도의 harness 판정이 담당합니다.
- 이 입력에는 도구의 인자와 반환값이 없고 도구 이름만 있습니다. 따라서 도구 호출 뒤
  에이전트가 말한 서비스 항목의 전체 이름·신규/기존 고객 구분·상담사·조회 일정이 고객 발화에
  없다는 이유만으로 지어냈다고 판정하지 마세요. 고객 발화나 루브릭과 명백히 모순될
  때만 문제로 지적하세요.
- 콘솔의 발화와 도구 호출 기록은 비동기로 수집됩니다. 도구 이름 목록만으로 발화와
  도구 호출 사이의 선후관계를 추정하지 마세요. 정확한 실행 순서는 별도 테스트에서
  검증하며, 여기서는 실제 발화 내용과 고객 동의 여부를 평가하세요.
- 루브릭이 기기 문제를 센터의 서비스 항목으로 연결하도록 요구하면 그 연결 자체를 진단이나
  hallucination으로 보지 마세요. `stage_booking` 이후 상담사 선택이나 최종 확인을 한 번
  더 받는 것은 도구가 요구하는 정상 절차일 수 있으므로, 최종적으로 같은 조건에 명시적
  동의를 받았다면 불필요 반복으로 판정하지 마세요.
- 시스템이 고정적으로 발화하는 녹음·문자·방문 준비 안내는 해당 시나리오 루브릭이
  명시적으로 검증하도록 요구하지 않는 한 평가하지 마세요.
- 개발 환경 데이터의 우연한 부재(빈 일정 등) 자체. 단, 그 상황을 에이전트가
  어떻게 안내했는지는 평가 대상입니다.

## 출력
반드시 아래 JSON 객체 하나만 출력하세요. 코드펜스, 설명, 머리말 금지.
{"verdict": "PASS" 또는 "FAIL", "reasons": ["판정 근거 문장", ...], "issues": ["발견한 문제", ...]}
- reasons 는 1~4개, issues 는 문제가 없으면 빈 배열.
- 루브릭 항목 중 하나라도 명백히 위반되면 FAIL 입니다."""


@dataclass
class JudgeResult:
    verdict: str  # PASS | FAIL | ERROR
    reasons: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    model: str = ""
    raw: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "reasons": self.reasons,
            "issues": self.issues,
            "model": self.model,
            "raw": self.raw if self.verdict == "ERROR" else "",
        }


def _strip_code_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    return stripped


def parse_judge_output(text: str, *, model: str = "") -> JudgeResult:
    """모델 출력에서 JSON을 방어적으로 추출한다. 실패하면 verdict=ERROR."""
    raw = text or ""
    candidate = _strip_code_fences(raw)
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return JudgeResult("ERROR", model=model, raw=raw.strip())
    try:
        data = json.loads(candidate[start : end + 1])
    except json.JSONDecodeError:
        return JudgeResult("ERROR", model=model, raw=raw.strip())
    if not isinstance(data, dict):
        return JudgeResult("ERROR", model=model, raw=raw.strip())

    verdict = str(data.get("verdict", "")).strip().upper()
    if verdict not in {"PASS", "FAIL"}:
        return JudgeResult("ERROR", model=model, raw=raw.strip())

    def _as_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [str(item) for item in value]
        return [str(value)]

    return JudgeResult(
        verdict=verdict,
        reasons=_as_list(data.get("reasons")),
        issues=_as_list(data.get("issues")),
        model=model,
    )


def build_judge_model(model_name: str):
    """customer_actor 와 같은 방식으로 langchain 채팅 모델을 만든다."""
    from customer_actor import ensure_model_api_key
    from langchain.chat_models import init_chat_model

    ensure_model_api_key(model_name)
    # gpt-5 계열은 temperature 변경을 지원하지 않는다 (기본값만 허용).
    kwargs = {} if model_name.startswith("openai:gpt-5") else {"temperature": 0}
    return init_chat_model(model_name, **kwargs)


def build_judge_prompt(scenario: Scenario, parsed: ParsedEvents) -> str:
    persona = scenario.persona
    tools = ", ".join(parsed.tools) if parsed.tools else "(없음)"
    handoffs = "\n".join(f"- {text}" for text in parsed.handoffs) or "- (없음)"
    transcript = parsed.quality_transcript_text() or "(대화 없음)"
    return (
        f"## 시나리오\n"
        f"- id: {scenario.id}\n"
        f"- 제목: {scenario.title}\n"
        f"- 의도/난이도: {scenario.intent} / {scenario.difficulty}\n"
        f"- 고객 목표: {persona.get('goal', '')}\n"
        f"- 고객 연기 스크립트:\n{persona.get('scenario', '')}\n\n"
        f"## 루브릭 (이 항목으로 채점)\n{scenario.expect.judge}\n\n"
        f"## 통화 기록\n{transcript}\n\n"
        f"## 도구 호출 순서\n{tools}\n\n"
        f"## 핸드오프\n{handoffs}\n\n"
        f"위 통화를 채점하고 JSON 한 개만 출력하세요."
    )


def judge_session(
    scenario: Scenario,
    parsed: ParsedEvents,
    *,
    model_name: str = DEFAULT_JUDGE_MODEL,
    model: Any = None,
) -> JudgeResult:
    """루브릭으로 통화를 채점한다. 모델 오류도 verdict=ERROR로 흡수한다."""
    try:
        chat = model if model is not None else build_judge_model(model_name)
        response = chat.invoke(
            [
                {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                {"role": "user", "content": build_judge_prompt(scenario, parsed)},
            ]
        )
    except Exception as exc:
        return JudgeResult("ERROR", model=model_name, raw=f"judge call failed: {exc}")

    content = getattr(response, "content", response)
    if isinstance(content, list):
        content = " ".join(str(part) for part in content)
    return parse_judge_output(str(content), model=model_name)
