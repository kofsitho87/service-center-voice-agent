import asyncio
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import yaml
from livekit import api
from livekit.agents import (
    AgentEvent,
    ChatContext,
    JobContext,
)
from loguru import logger

from data import CallSessionData
from external_apis.types.response_types import ServiceItem
from utils.i18n import WEEKDAY_NAMES_BY_LANGUAGE


def load_prompt(filename, variant: str | None = None, **kwargs):
    """Load a prompt from a YAML file and format with variables.

    variant: instructions 키 변형 선택자. 예: variant="context" 이면
        "<lang>_context_instructions" -> "context_instructions" 를 먼저 찾고,
        없으면 기본 체인("<lang>_instructions" -> "instructions" -> "ko_instructions")
        으로 폴백한다.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    prompt_path = os.path.join(script_dir, "../", "prompts", filename)

    try:
        with open(prompt_path) as file:
            prompt_data = yaml.safe_load(file)

            language_code = str(
                kwargs.get("language_code") or kwargs.get("language") or "ko-KR"
            )

            # 기본 변수들 설정
            # The business is in Seoul, so every caller language uses Asia/Seoul.
            timezone = "Asia/Seoul"
            now = datetime.now(tz=ZoneInfo(timezone))

            # 언어별/변형별 instructions 선택 (하위 호환).
            # 우선순위: variant 키("<lang>_<variant>_instructions" -> "<variant>_instructions")
            #   -> "<lang>_instructions"(예: ko_instructions / en_instructions)
            #   -> "instructions" -> "ko_instructions" -> ""
            # 단일 "instructions:" 키만 가진 기존 프롬프트는 그대로 동작한다.
            lang_prefix = language_code.split("-")[0]
            key_candidates: list[str] = []
            if variant:
                key_candidates += [
                    f"{lang_prefix}_{variant}_instructions",
                    f"{variant}_instructions",
                ]
            key_candidates += [
                f"{lang_prefix}_instructions",
                "instructions",
                "ko_instructions",
            ]
            instructions = next(
                (
                    str(prompt_data[key])
                    for key in key_candidates
                    if prompt_data.get(key)
                ),
                "",
            )

            weekdays = WEEKDAY_NAMES_BY_LANGUAGE.get(
                language_code, WEEKDAY_NAMES_BY_LANGUAGE["en-US"]
            )
            weekday = weekdays[now.weekday()]

            if language_code == "ko-KR":
                current_date_str = now.strftime("%Y년 %m월 %d일")
                current_time_str = now.strftime("%H시 %M분")
                formatted_date_with_weekday = now.strftime(
                    f"%Y년 %m월 %d일 ({weekday})"
                )
                formatted_datetime = now.strftime("%Y년 %m월 %d일 %H시 %M분")
            else:
                # Avoid OS locale dependencies: keep numeric date/time, but localize weekday labels.
                current_date_str = now.strftime("%Y-%m-%d")
                current_time_str = now.strftime("%H:%M")
                formatted_date_with_weekday = f"{current_date_str} ({weekday})"
                formatted_datetime = f"{current_date_str} {current_time_str}"

            localized_datetime = f"{formatted_date_with_weekday} {current_time_str}"

            default_vars = {
                "formatted_datetime": formatted_datetime,
                "current_date": current_date_str,
                "current_time": current_time_str,
                "day_of_week": weekday,
                "current_weekday": weekday,
                "formatted_date_with_weekday": formatted_date_with_weekday,
                "localized_datetime": localized_datetime,
            }

            # 사용자 정의 변수와 기본 변수 결합
            format_vars = {**default_vars, **kwargs}

            # 템플릿 변수 치환
            return instructions.format(**format_vars)
    except (FileNotFoundError, yaml.YAMLError) as e:
        logger.bind(event="PROMPT_LOAD_ERROR", filename=filename).opt(
            exception=e
        ).error("Failed to load prompt file; using empty instructions")
        return ""
    except KeyError as e:
        logger.bind(
            event="PROMPT_TEMPLATE_KEY_MISSING", filename=filename, key=str(e)
        ).warning("Missing prompt template variable; using unformatted instructions")
        return instructions


def normalize_phone(phone: str) -> str:
    """Remove all non-digit characters from phone number and validate."""
    digits = re.sub(r"\D", "", phone)
    if not digits or not digits.startswith("0") or not (9 <= len(digits) <= 11):
        raise ValueError("Invalid phone number.")
    return digits


VERIFICATION_CODE_LENGTH = 6


def normalize_verification_code(verification_code: str | None) -> str | None:
    """Return the demo verification code as a plain 6-digit string.

    Separators and spaces are ignored. Empty input returns None; anything that is
    not exactly 6 digits raises ValueError.
    """
    if verification_code is None or verification_code == "":
        return None

    digits = re.sub(r"\D", "", verification_code)
    if len(digits) != VERIFICATION_CODE_LENGTH:
        raise ValueError(
            f"Invalid verification code. Please provide {VERIFICATION_CODE_LENGTH} digits."
        )
    return digits


def sanitize_for_path(name: str) -> str:
    """파일 경로/S3 키/URL에서 안전하도록 room 이름을 정규화한다.

    SIP room 이름에는 '+'(전화번호) 등 특수문자가 포함될 수 있는데,
    '+'는 HTTP URL 경로에서 공백(또는 '%2B')으로 취급되어 저장된 S3 키와
    요청 URL이 달라지는 문제가 있다. 영숫자와 '.', '_', '-' 외의 문자를
    '_'로 치환해 어디서든 깨지지 않도록 만든다.
    """
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


DEFAULT_EGRESS_ACTIVE_WAIT_TIMEOUT_SEC = 2.0
DEFAULT_EGRESS_ACTIVE_OBSERVE_TIMEOUT_SEC = 15.0
DEFAULT_RECORDING_ANCHOR_SHUTDOWN_WAIT_TIMEOUT_SEC = 1.0

_EGRESS_TERMINAL_STATUSES = (
    api.EgressStatus.EGRESS_COMPLETE,
    api.EgressStatus.EGRESS_FAILED,
    api.EgressStatus.EGRESS_ABORTED,
    api.EgressStatus.EGRESS_LIMIT_REACHED,
)


async def wait_for_egress_active(
    lkapi: api.LiveKitAPI,
    egress_info: api.EgressInfo,
    timeout: float | None = None,
    poll_interval: float = 0.1,
) -> api.EgressInfo:
    """egress 파이프라인이 실제로 녹음을 시작할 때까지 기다린다.

    `start_egress()`는 egress 작업이 큐에 접수된 시점
    (EGRESS_STARTING)에 리턴한다. 실제 파이프라인이 워커를 할당받아 룸에
    참가하고 트랙을 구독해 인코딩을 시작(EGRESS_ACTIVE)하기까지는 시간이 더
    걸리는데, 그 사이에 그리팅 TTS가 흘러나가면 앞부분이 녹음 파일에 남지
    않는다(실제 사례: 그리팅이 "안녕하" 없이 "세요"부터 시작).

    발신자 입장에서는 이 대기가 그대로 그리팅 지연이 되므로 timeout으로 상한을
    둔다(기본 2초, env `EGRESS_ACTIVE_WAIT_TIMEOUT_SEC`). 시간 안에 ACTIVE가
    되지 않아도 예외를 던지지 않고 마지막으로 확인한 상태를 반환한다 —
    녹음보다 통화가 우선이다.
    """
    if timeout is None:
        timeout = float(
            os.getenv(
                "EGRESS_ACTIVE_WAIT_TIMEOUT_SEC",
                DEFAULT_EGRESS_ACTIVE_WAIT_TIMEOUT_SEC,
            )
        )

    if egress_info.status == api.EgressStatus.EGRESS_ACTIVE:
        return egress_info

    loop = asyncio.get_running_loop()
    started_at = loop.time()
    deadline = started_at + timeout
    latest = egress_info

    while True:
        try:
            response = await lkapi.egress.list_egress(
                api.ListEgressRequest(egress_id=egress_info.egress_id)
            )
        except Exception as e:
            logger.bind(
                event="EGRESS_ACTIVE_WAIT_ERROR",
                egress_id=egress_info.egress_id,
                room_name=egress_info.room_name,
            ).opt(exception=e).warning(
                "Failed to poll egress status; continuing without waiting"
            )
            return latest

        if response.items:
            latest = response.items[0]

            if latest.status == api.EgressStatus.EGRESS_ACTIVE:
                logger.bind(
                    event="EGRESS_ACTIVE",
                    egress_id=latest.egress_id,
                    room_name=latest.room_name,
                    waited_sec=round(loop.time() - started_at, 3),
                ).info("Egress is recording")
                return latest

            if latest.status in _EGRESS_TERMINAL_STATUSES:
                logger.bind(
                    event="EGRESS_ACTIVE_WAIT_ABORTED",
                    egress_id=latest.egress_id,
                    room_name=latest.room_name,
                    egress_status=api.EgressStatus.Name(latest.status),
                ).warning("Egress reached a terminal state before becoming active")
                return latest

        remaining = deadline - loop.time()
        if remaining <= 0:
            logger.bind(
                event="EGRESS_ACTIVE_WAIT_TIMEOUT",
                egress_id=egress_info.egress_id,
                room_name=egress_info.room_name,
                egress_status=api.EgressStatus.Name(latest.status),
                timeout_sec=timeout,
            ).warning(
                "Egress did not become active in time; "
                "starting session anyway (call takes priority over recording)"
            )
            return latest

        await asyncio.sleep(min(poll_interval, remaining))


async def setup_egress(
    ctx: JobContext,
    call_session_data: CallSessionData,
    egress_storage_type: Literal["local", "s3"] = "s3",
) -> tuple[api.LiveKitAPI, api.EgressInfo, str, float | None]:
    """Set up audio-only room recording with the unified StartEgress API.

    Self-hosted LiveKit Server must be v1.13.5 or later.

    Args:
        egress_storage_type: 'local' or 's3'. Defaults to 's3'

    Returns:
        (LiveKitAPI, EgressInfo, 녹음 파일 경로, 녹음 시작 시각)

        마지막 값은 녹음 파일의 t=0에 대응하는 epoch seconds 이며, ACTIVE 를
        확인하지 못했으면 None 이다.
    """

    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    filename = f"recording-{timestamp}.ogg"
    company_id = call_session_data.business_data.client_id
    safe_room_name = sanitize_for_path(ctx.room.name)

    storage = None
    if egress_storage_type == "local":
        # 로컬 저장소 경로 (프로젝트 루트의 recordings/ 디렉토리)
        project_root = Path(__file__).parent.parent.parent
        local_dir = project_root / "recordings" / safe_room_name
        local_dir.mkdir(parents=True, exist_ok=True)
        audio_filepath = str(local_dir / filename)

        file_output = api.FileOutput(
            file_type=api.EncodedFileType.OGG,
            filepath=audio_filepath,
        )
        audio_file_url = audio_filepath
    else:
        # S3 저장소
        audio_filepath = (
            f"inbound-agent/{os.getenv('ENV')}/{company_id}/{safe_room_name}.ogg"
        )

        file_output = api.FileOutput(
            file_type=api.EncodedFileType.OGG,
            filepath=audio_filepath,
        )
        storage = api.StorageConfig(
            s3=api.S3Upload(
                access_key=os.getenv("AWS_ACCESS_KEY_ID"),
                secret=os.getenv("AWS_SECRET_ACCESS_KEY"),
                bucket=os.getenv("AWS_BUCKET"),
                region=os.getenv("AWS_REGION"),
            ),
        )
        audio_file_url = f"https://{os.getenv('AWS_BUCKET')}.s3.{os.getenv('AWS_REGION')}.amazonaws.com/{audio_filepath}"

    req = api.StartEgressRequest(
        room_name=ctx.room.name,
        template=api.TemplateSource(audio_only=True),
        outputs=[api.Output(file=file_output)],
        storage=storage,
    )

    lkapi = api.LiveKitAPI()
    egress_info = await lkapi.egress.start_egress(req)

    logger.bind(
        event="SETUP_EGRESS_SUCCESS",
        egress_storage_type=egress_storage_type,
        audio_file_url=audio_file_url,
        room_name=ctx.room.name,
    ).info("Setup egress successfully")

    # 요청 접수(EGRESS_STARTING)와 실제 녹음 시작(EGRESS_ACTIVE) 사이에 세션이
    # 시작되면 그리팅 앞부분이 녹음되지 않는다. 상한을 둔 채로 대기한다.
    egress_info = await wait_for_egress_active(lkapi, egress_info)

    recording_started_at = capture_recording_started_at(
        egress_info,
        room_name=ctx.room.name,
    )

    return lkapi, egress_info, audio_filepath, recording_started_at


def capture_recording_started_at(
    egress_info: api.EgressInfo,
    *,
    room_name: str,
) -> float | None:
    """Capture the local-clock recording anchor after observing ACTIVE."""
    if egress_info.status != api.EgressStatus.EGRESS_ACTIVE:
        logger.bind(
            event="RECORDING_STARTED_AT",
            room_name=room_name,
            recording_started_at=None,
            egress_status=api.EgressStatus.Name(egress_info.status),
        ).debug("Recording start anchor not captured")
        return None

    # 대화 이벤트와 같은 프로세스 clock을 사용해야 녹음 파일 내 상대 위치를
    # 계산할 수 있다. LiveKit started_at은 서버 clock이라 로그 비교에만 쓴다.
    recording_started_at = time.time()
    livekit_started_at = (
        egress_info.started_at / 1_000_000_000 if egress_info.started_at else None
    )
    logger.bind(
        event="RECORDING_STARTED_AT",
        room_name=room_name,
        recording_started_at=recording_started_at,
        livekit_started_at=livekit_started_at,
        clock_delta_sec=round(recording_started_at - livekit_started_at, 3)
        if livekit_started_at
        else None,
    ).debug("Recording start anchor captured")
    return recording_started_at


async def observe_recording_start(
    ctx: JobContext,
    call_session_data: CallSessionData,
) -> None:
    """Continue observing egress after the initial greeting gate times out."""
    lkapi = call_session_data.lkapi_ref.get("instance")
    egress_info = call_session_data.lkapi_ref.get("egress_info")
    if lkapi is None or egress_info is None:
        return

    timeout = float(
        os.getenv(
            "EGRESS_ACTIVE_OBSERVE_TIMEOUT_SEC",
            DEFAULT_EGRESS_ACTIVE_OBSERVE_TIMEOUT_SEC,
        )
    )
    latest = await wait_for_egress_active(
        lkapi,
        egress_info,
        timeout=timeout,
    )
    call_session_data.lkapi_ref["egress_info"] = latest
    call_session_data.recording_started_at = capture_recording_started_at(
        latest,
        room_name=ctx.room.name,
    )

    if call_session_data.recording_started_at is None:
        logger.bind(
            event="RECORDING_START_NOT_OBSERVED",
            room_name=ctx.room.name,
            egress_id=latest.egress_id,
            egress_status=api.EgressStatus.Name(latest.status),
            timeout_sec=timeout,
        ).warning("Recording did not become active during background observation")


async def initialize_recording(
    ctx: JobContext,
    call_session_data: CallSessionData,
    *,
    egress_storage_type: Literal["local", "s3"] = "s3",
) -> None:
    """Call recording is disabled; no egress request is made.

    The demo never persists raw audio. The egress-based implementation is kept
    as ``_initialize_recording_with_egress`` (unused).
    """
    logger.bind(event="RECORDING_DISABLED", room_name=ctx.room.name).info(
        "Call recording disabled in demo"
    )


async def _initialize_recording_with_egress(
    ctx: JobContext,
    call_session_data: CallSessionData,
    *,
    egress_storage_type: Literal["local", "s3"] = "s3",
) -> None:
    """Egress-based recording (unused in the demo).

    Set up egress and continue ACTIVE observation without blocking the greeting.
    """
    try:
        (
            call_session_data.lkapi_ref["instance"],
            call_session_data.lkapi_ref["egress_info"],
            call_session_data.recording_filepath,
            call_session_data.recording_started_at,
        ) = await setup_egress(
            ctx,
            call_session_data,
            egress_storage_type=egress_storage_type,
        )

        if call_session_data.recording_started_at is None:
            observer_task = asyncio.create_task(
                observe_recording_start(ctx, call_session_data)
            )
            call_session_data.lkapi_ref["recording_observer_task"] = observer_task
            logger.bind(
                event="RECORDING_START_OBSERVATION_CONTINUED",
                room_name=ctx.room.name,
                egress_id=call_session_data.lkapi_ref["egress_info"].egress_id,
            ).debug("Continuing to observe recording start in background")
        else:
            logger.bind(
                event="RECORDING_START_SUCCESS",
                room_name=ctx.room.name,
                egress_id=call_session_data.lkapi_ref["egress_info"].egress_id,
            ).debug("Recording started successfully")
    except Exception as e:
        logger.bind(event="RECORDING_START_ERROR", room_name=ctx.room.name).opt(
            exception=e
        ).error("Failed to start recording")


async def wait_for_recording_anchor(
    call_session_data: CallSessionData,
    timeout: float = DEFAULT_RECORDING_ANCHOR_SHUTDOWN_WAIT_TIMEOUT_SEC,
) -> None:
    """Wait briefly for an in-flight observer before serializing call analysis."""
    if call_session_data.recording_started_at is not None:
        return

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    try:
        setup_task = call_session_data.lkapi_ref.get("recording_setup_task")
        if setup_task is not None and not setup_task.done():
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            await asyncio.wait_for(asyncio.shield(setup_task), timeout=remaining)

        if call_session_data.recording_started_at is not None:
            return

        observer_task = call_session_data.lkapi_ref.get("recording_observer_task")
        if observer_task is None or observer_task.done():
            return

        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError
        await asyncio.wait_for(asyncio.shield(observer_task), timeout=remaining)
    except TimeoutError:
        logger.bind(
            event="RECORDING_ANCHOR_WAIT_TIMEOUT",
            room_name=call_session_data.room_name,
            timeout_sec=timeout,
        ).warning("Recording start was not observed before analysis payload creation")
    except Exception as e:
        logger.bind(
            event="RECORDING_ANCHOR_WAIT_ERROR",
            room_name=call_session_data.room_name,
        ).opt(exception=e).warning(
            "Failed while waiting for recording start observation"
        )


async def stop_egress(ctx: JobContext, lkapi_ref: dict[str, Any]):
    lkapi = lkapi_ref.get("instance")
    egress_info = lkapi_ref.get("egress_info")
    if not lkapi or not egress_info:
        logger.bind(event="STOP_EGRESS_SKIP", room_name=ctx.room.name).warning(
            "Egress not initialized, skipping stop"
        )
        return

    try:
        stop_req = api.StopEgressRequest(egress_id=egress_info.egress_id)
        await lkapi.egress.stop_egress(stop_req)
        await lkapi.aclose()
    except Exception as e:
        logger.bind(event="STOP_EGRESS_ERROR", room_name=ctx.room.name).opt(
            exception=e
        ).error("Failed to stop egress")


# 계층 구조를 평면화하는 함수
def flatten_services(
    services: list[ServiceItem],
    *,
    display_mode: Literal["parent_with_child", "child_only"] = "parent_with_child",
) -> list[dict[str, Any]]:
    flattened = []

    for parent_dept in services:
        # 자식 부서가 있는 경우
        if parent_dept.services and len(parent_dept.services) > 0:
            for child_dept in parent_dept.services:
                if child_dept.operatorCount > 0:
                    service_name = (
                        child_dept.name
                        if display_mode == "child_only"
                        else f"{parent_dept.name} - {child_dept.name}"
                    )
                    flattened.append(
                        {
                            "service_id": child_dept.serviceId,
                            "service_name": service_name,
                            "keywords": list(child_dept.keywords),
                        }
                    )
        else:
            # 자식 부서가 없는 경우 (최상위 부서)
            if parent_dept.operatorCount > 0:
                flattened.append(
                    {
                        "service_id": parent_dept.serviceId,
                        "service_name": parent_dept.name,
                        "keywords": list(parent_dept.keywords),
                    }
                )

    return flattened


def write_transcript_sync(room_name: str, history: ChatContext):
    current_date = datetime.now().strftime("%m-%d_%H:%M")

    # This example writes to the temporary directory, but you can save to any location
    filename = f"transcripts/conversations/{current_date}.json"

    # Create transcripts directory if it doesn't exist
    os.makedirs("transcripts/conversations", exist_ok=True)

    cleaned_history = []
    for item in history.items:
        cleaned_history.append(item.model_dump())

    with open(filename, "w") as f:
        json.dump(cleaned_history, f, indent=2, ensure_ascii=False)

    print(f"Transcript for {room_name} saved to {filename}")

    return cleaned_history


async def write_transcript(room_name: str, history: ChatContext):
    return write_transcript_sync(room_name, history)


def write_transcript_events_sync(events: list[AgentEvent]) -> None:
    current_date = datetime.now().strftime("%Y%m%d_%H%M%S")

    # This example writes to the temporary directory, but you can save to any location
    filename = f"transcripts/events/events_{current_date}.json"

    # Create transcripts directory if it doesn't exist
    os.makedirs("transcripts/events", exist_ok=True)

    with open(filename, "w") as f:
        json.dump(
            [
                event.model_dump()
                for event in events
                if event.type in ("conversation_item_added", "function_tools_executed")
            ],
            f,
            indent=2,
            ensure_ascii=False,
        )

    print(f"Transcript events saved to {filename}")


async def write_transcript_events(events: list[AgentEvent]) -> None:
    write_transcript_events_sync(events)


# Dev/test-only dial_info fixtures. Not baked into the Docker image
# (excluded via .dockerignore); production reads dial_info from job metadata.
DIAL_INFO_DIR = Path(__file__).resolve().parents[1] / "dial_info"


def load_dial_info_file(path: str | Path) -> dict[str, Any]:
    """Load a dev/test dial_info fixture from JSON or YAML."""
    dial_info_path = Path(path)
    with dial_info_path.open(encoding="utf-8") as file:
        suffix = dial_info_path.suffix.lower()
        if suffix in {".yaml", ".yml"}:
            dial_info = yaml.safe_load(file)
        elif suffix == ".json":
            dial_info = json.load(file)
        else:
            raise ValueError(
                f"Unsupported dial_info file extension: {dial_info_path.suffix}"
            )

    if not isinstance(dial_info, dict):
        raise ValueError(
            f"Expected a mapping in dial_info file, got {type(dial_info).__name__}"
        )
    return dial_info


def resolve_dial_info_path(default_name: str) -> str:
    """Resolve a dev dial_info fixture path.

    Honors DIAL_INFO_FILE, then the legacy DIAL_INFO_JSON override; otherwise
    falls back to src/dial_info/<default_name>.
    """
    return os.getenv("DIAL_INFO_FILE") or os.getenv(
        "DIAL_INFO_JSON", str(DIAL_INFO_DIR / default_name)
    )
