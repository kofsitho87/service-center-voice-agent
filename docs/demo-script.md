# Demo Script

Before you start: `uv run python tools/demo.py reset`. The default caller is the
synthetic customer DC-1001, who already has one inspection appointment.

## A. Booking (about 1 minute)

Voice: `uv run python src/agent-cli.py console`. Text:
`uv run python src/agent-cli-text.py console --text`.

1. Say "휴대폰 액정이 깨져서 다음 주 화요일 오후에 점검 예약하고 싶어요."
2. Pick a time from the three offered, for example "오후 1시요."
3. If asked which advisor, say "상담사 A요."
4. When the full confirmation is read back, say "네."
5. The agent announces the booking and ends the call.

Run `uv run python tools/demo.py show`: a new `agent` row appears.

## B. Correction and interruption (about 1.5 minutes)

1. Ask for a laptop consultation. Refuse the first time offered ("그 시간은 안 돼요,
   목요일 오후는요?").
2. At the confirmation question, ask "주말에도 운영하세요?" and then say "네,
   진행해 주세요."
3. Expected: the agent answers from the center info, presents the proposal again,
   and commits only after a fresh "네". The fresh-consent check blocks the stale
   consent.

## C. Safety and handoff (about 1 minute)

1. Say "배터리가 부풀었어요."
2. Expected: stop-use advice, then "직원 연결을 원하시나요?".
3. Say "아니요, 예약할게요." Booking continues.
4. Variation: say "네". The handoff is simulated, no call is placed, and the
   fallback (leave a message) runs.

## D. Cancellation (about 1 minute)

1. Say "예약 취소하려고요." Pick the earliest appointment and confirm.
2. Run `tools/demo.py show`: the row state is `CANCELLED` (kept for audit).

## E. After the calls

Run `uv run python conversation_analysis/main.py --show`. It prints one result
per call: resolution, tools used, booking events, reservation ID, and duration.

## Offline proof (no credentials)

```bash
uv run pytest tests            # 1800+ tests, about 25 s
uv run pytest tests/scenarios  # S01-S21 only
```
