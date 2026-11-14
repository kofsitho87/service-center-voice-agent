# Evaluation

There are three kinds of evidence:

- **Offline.** Runs need no credentials or network. `tests/conftest.py`
  blocks non-loopback sockets, pins the business clock to Monday 2026-11-16
  09:30 KST, and isolates the demo database and outbox per test. These tests
  check application contracts with scripted model turns, not language
  understanding.
- **Live text.** The real text entrypoint runs with an LLM-simulated caller
  and an LLM judge.
- **Live voice.** STT and TTS run with a microphone and speakers.

## Offline result

The command is `uv run pytest tests` (about 22 s).

| Suite | Passed | xfail |
| --- | --- | --- |
| `tests/characterization/` (unit and characterization tests) | 1747 | 1 |
| `tests/scenarios/` (S01–S21) | 26 | 0 |
| `tests/integration/`, `tests/unit/` (fake backend, reservation write, post-call consumer) | 34 | 0 |
| **Total** | **1807** | **1** |

The xfail is a known failure:
`test_operator_schedule_lookup_timing::test_empty_exact_date_lookup_only_describes_requested_date`.
The "no slots" message does not mention the requested date.

## Scenario matrix

The paths in the matrix mean:

- **Task:** the `DynamicBookingTask` tools run for real.
- **Parent:** the real `create_new_appointment` body runs, including the
  reservation write.
- **Hook:** `submit_current_reservation` runs with real session data.

All paths use the real `booking_api` client against the fake backend.

| ID | Scenario | Path exercised | Result |
| --- | --- | --- | --- |
| S01 | Normal booking | Task → Parent → DB | Exactly one row after explicit consent; the task alone writes nothing |
| S02 | Refusal | Task | Zero writes; the rejected slot is no longer offered |
| S03 | Time change | Task → Parent | The old consent gets `staging_mismatch`; only the new slot is saved |
| S04 | Slot taken before commit | Task → Parent → DB | The write returns CONFLICT. There is no double booking, the booking tool is re-entered, and the taken advisor is not offered again |
| S05 | Retry | Parent ×2 | The request is replayed; one row |
| S06 | No availability | Task | Empty weekend search; an invented slot cannot be staged |
| S07 | FAQ interrupts a proposal | Task → Parent | The commit is blocked with `consent_not_fresh` unless consent is the single user turn after the latest proposal; after re-staging and fresh consent, it commits. A commit with no reply is also blocked |
| S08 | Valid cancellation | Lookup → Hook | Only the owned target is cancelled |
| S09 | Foreign reference | Lookup → Hook | Not listed; modify and cancel return NOT_FOUND; no mutation, no disclosure |
| S10 | Tool failure | Hook | ERROR is never announced as success; the caller is handed off to staff |
| S11 | Invalid date or boundary | Task | Weekend, after-hours, past, and beyond-horizon times cannot be staged |
| S12 | Stale confirmation | Task | A restored candidate gets `staging_requires_revalidation` |
| S13 | Handoff busy/available | Trunk client (HMAC) + gateway | Busy → available sequence; the SIP dial is simulated and the target is redacted |
| S14 | Prompt injection | Task | A commit without a stage, an unknown advisor, and the wrong skill are all blocked |
| S15 | Concurrent requests | Hook ×2 (parallel) | One ACCEPTED, one CONFLICT; a 6-way backend test is in `tests/integration` |
| S16 | Session ends awaiting consent | Task | No write; nothing appears in the safe payload |
| S17 | Modification | Hook | The target moves, the old slot is released, and no new rows are created |
| S18 | DTMF menu | Keypad dial_info + parser + lookup + code validator | Keys 1–4 map to the booking tools; demo code `000202` matches the customer |
| S19 | Interrupted booking resumes | Task ×2 | Preferences and rejections survive; prior consent does not |
| S20 | Queue disconnect cleanup | Warm-transfer queue on fakeredis | Occupancy is released, a duplicate release is safe, and no SIP attempt is made |
| S21 | Call end and local analysis | Publisher → consumer → SQLite | One safe event, processed once despite redelivery; malformed lines are skipped and offsets resume |

## Live regression suite

The command is `uv run python tools/sim-suite.py`. The agent and the simulated
caller use `openai:gpt-5.6-luna`; the judge uses `openai:gpt-5.4-mini`. Three
calls run at a time against the shared fake backend.

| Scenario | Outcome | Business | Quality (judge) | Harness |
| --- | --- | --- | --- | --- |
| `create_easy_01_phone_inspection` | booking_created | PASS | PASS | CLEAN |
| `create_medium_01_refuse_then_other_time` | booking_created | PASS | PASS | CLEAN |
| `create_medium_02_faq_interrupts_confirmation` | booking_created | PASS | PASS | CLEAN |
| `safety_01_battery_swelling_decline_staff` | booking_created | PASS | PASS | CLEAN |
| `cancel_easy_01_confirm` | booking_cancelled | PASS | PASS | CLEAN |

This is one run of 5 scenarios. It is a smoke-level signal, not an accuracy
estimate.

## Live text observations

- **Booking:** search → time choice → advisor choice → full read-back →
  "네" → commit → one stored row.
- **FAQ during confirmation:** the agent answered from the center information,
  presented the proposal again, and committed after fresh consent. The
  confirmation question was repeated once.
- **Battery swelling:** the agent gave stop-use advice, asked whether to
  connect staff, and continued the booking after "no".
- **Cancellation with two appointments:** the agent listed both, confirmed the
  chosen one, and cancelled only that row.
- **Post-call:** `conversation_analysis/main.py` saved one result per call
  (resolution, tools, reservation ID, duration). Re-running reads no new events.

Issues found and fixed during live testing:

1. Ordinary device faults were treated as safety evidence, so bookings were
   derailed into a memo handoff. Safety evidence now covers battery swelling,
   overheating, smoke, and burning only.
2. The battery-safety handoff connected without asking. It now asks first.
3. The booking success line repeated the service word.
4. The local console room name ("console") made every call after the first
   look like a duplicate to the consumer. Results are now keyed by room plus
   start time.

Known cosmetic issues:

- 12:00 is read as "오후 12시".
- The leave-message flow says "오후으로".

## Live voice

- **Providers:** STT is `elevenlabs:scribe_v2_realtime`. TTS is
  `cartesia:sonic-3` with the public Korean voice "Yeji - Clarity Desk"
  (`1a602be4-3b57-4bef-a270-dfa5efed0541`).
- **Loopback check (no microphone):** Cartesia synthesized "다음 주 화요일 오후 두 시에
  휴대폰 점검 예약하고 싶어요." and Scribe transcribed it as "… 오후 2시에 …". The
  only difference is the numeral style.
- **Microphone-to-speaker run:** not yet performed
  (`uv run python src/agent-cli.py console`).

## Known limits

1. **Fresh consent.** The check blocks a commit when zero or two or more user
   turns follow the proposal. It cannot tell whether the single following turn
   is really consent; that judgment stays with the model. A "yes" split across
   two speech turns triggers a re-confirmation.
2. **Connected warm transfer.** It is not simulated. The staff handoff always
   takes the fallback path (retry, then leave a message).
