"""LLM 고객 연기 공유 모듈.

`tools/customer-sim.py`(PTY 텍스트 콘솔)와 `tools/audio_customer_sim.py`
(LiveKit 오디오 트랙 러너)가 같은 고객 LLM 로직을 쓰도록 분리했다.

세션 객체는 덕 타이핑으로 받는다 — `persona: dict`,
`actor_directions: list[str]`, `transcript_lines() -> list[tuple[str, str]]`
를 제공하면 어떤 세션 구현이든 사용할 수 있다.
"""

from __future__ import annotations

import os
import threading

from langchain.chat_models import init_chat_model

SKIP_REPLY = "SKIP"
WAIT_ONLY_MARKERS = ("잠시만 기다려", "잠시만 기다려주세요", "처리 중", "확인 중")
QUESTION_MARKERS = ("?", "습니까", "까요", "겠어요", "주세요")


def is_wait_only_agent_message(text: str) -> bool:
    """응답 없이 기다리면 되는 안내인지 판별한다."""
    text = text.strip()
    if not text:
        return True
    if "?" in text or "겠어요" in text or "습니까" in text:
        return False
    return any(marker in text for marker in WAIT_ONLY_MARKERS)


def agent_message_needs_reply(text: str) -> bool:
    """고객 LLM이 이 에이전트 발화에 답해야 하는지 판별한다.

    대기 안내가 아니면 인사·일반 안내도 대화를 이어가야 하므로 True.
    """
    text = text.strip()
    return bool(text) and not is_wait_only_agent_message(text)


def is_question_agent_message(text: str) -> bool:
    """고객의 직접 답변을 요구하는 질문·요청형 발화인지 판별한다."""
    if not agent_message_needs_reply(text):
        return False
    return any(marker in text for marker in QUESTION_MARKERS)


def pick_agent_message_for_reply(messages: list[str]) -> str | None:
    """디바운스 윈도우 안에서 고객이 답할 대상 발화를 고른다 (질문 우선)."""
    for message in reversed(messages):
        if is_question_agent_message(message):
            return message
    for message in reversed(messages):
        if agent_message_needs_reply(message):
            return message
    return None


def is_skip_reply(reply: str) -> bool:
    """모델이 SKIP 주변에 구두점·따옴표를 붙여도 무응답으로 처리한다."""
    return reply.strip().strip("\"'`.。!,").strip().upper() == SKIP_REPLY


def resolve_customer_model(explicit: str | None = None) -> str:
    # gpt-5.3-chat-latest 는 2026-08 기준 OpenAI 에서 deprecated(404) 되어
    # 고객 발화가 한 마디도 생성되지 않은 채 무음 종료로 빠진다.
    if explicit:
        return explicit
    if env_model := os.getenv("CUSTOMER_SIM_MODEL"):
        return env_model
    if os.getenv("OPENAI_API_KEY"):
        return "openai:gpt-5.6-luna"
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic:claude-sonnet-4-20250514"
    return "openai:gpt-5.6-luna"


def ensure_model_api_key(model_name: str) -> None:
    provider = model_name.split(":", 1)[0]
    key_env = {
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
    }.get(provider)
    if key_env and not os.getenv(key_env):
        raise SystemExit(
            f"{key_env}가 설정되지 않았습니다. "
            f"프로젝트 루트 `.env`에 키를 추가하거나 `--model`을 사용하세요."
        )


CUSTOMER_SYSTEM_PROMPT = """당신은 가상 전자기기 서비스센터(Example Device Care)에 전화를 거는 고객 역할 연기자입니다.

## 페르소나
- 이름: {name}
- 데모 확인번호(6자리): {verification_code}
- 나이/성별: {age_gender}
- 성격·말투: {personality}
- 전화 목적: {goal}
- 시나리오: {scenario}

## 규칙
- 실제 음성 통화처럼 짧은 구어체 한국어로 1~2문장만 답하세요.
- 에이전트가 이름을 물으면 위 페르소나 값으로 답하세요.
- 데모 확인번호 6자리를 입력하라고 하면 페르소나의 확인번호 6자리 숫자만 공백·설명 없이 답하세요. 실제 생년월일·주민등록번호는 절대 말하지 마세요.
- "잠시만 기다려주세요"처럼 응답이 필요 없는 안내면 {skip} 만 출력하세요.
- 질문·확인 요청에만 답하세요. JSON, 따옴표, 설명 없이 고객 대사만 출력하세요.
{directions_block}"""


class CustomerActor:
    def __init__(self, session, model_name: str):
        self.session = session
        # gpt-5 계열은 temperature 변경을 지원하지 않는다 (기본값 1만 허용)
        kwargs = {} if model_name.startswith("openai:gpt-5") else {"temperature": 0.7}
        self.model = init_chat_model(model_name, **kwargs)
        self._lock = threading.Lock()

    def should_respond(self, agent_content: str) -> bool:
        return agent_message_needs_reply(agent_content)

    def _directions_block(self) -> str:
        if not self.session.actor_directions:
            return ""
        joined = "\n".join(f"- {item}" for item in self.session.actor_directions)
        return f"\n## 연기 지시 (최우선 반영)\n{joined}\n"

    def generate_reply(self, agent_content: str) -> str | None:
        if not self.should_respond(agent_content):
            return None

        persona = self.session.persona
        history = self.session.transcript_lines()
        history_text = "\n".join(f"{role}: {content}" for role, content in history)
        system = CUSTOMER_SYSTEM_PROMPT.format(
            name=persona.get("name", ""),
            verification_code=persona.get("verification_code", ""),
            age_gender=persona.get("age_gender", ""),
            personality=persona.get("personality", ""),
            goal=persona.get("goal", ""),
            scenario=persona.get("scenario", ""),
            skip=SKIP_REPLY,
            directions_block=self._directions_block(),
        )
        user = (
            f"{history_text}\n\n"
            f"에이전트(방금): {agent_content}\n\n"
            f"고객 대사 또는 {SKIP_REPLY}:"
        )

        with self._lock:
            response = self.model.invoke(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ]
            )

        content = getattr(response, "content", response)
        if isinstance(content, list):
            content = " ".join(str(part) for part in content)
        reply = str(content).strip()
        if not reply or is_skip_reply(reply):
            return None
        return reply
