# AI Mock Interview Demo

Python LiveKit Agents demo for a two-stage mock interview:

1. Self Introduction
2. Past Experience

The implementation uses separate LiveKit agents for the two stages and a shared state-machine controller to make transitions deterministic and idempotent.

## Architecture Summary

`AgentSession[InterviewSessionData]` owns shared interview state. `SelfIntroductionAgent` starts the interview, asks for a brief introduction, and transitions when enough information is collected. `PastExperienceAgent` then asks about previous roles, projects, responsibilities, achievements, and impact.

All transition requests go through `InterviewController`. This keeps only one active stage at a time and prevents duplicate handoffs from racing tool calls, timeouts, or manual skip requests.

```text
LiveKit Room
  -> AgentServer entrypoint
  -> AgentSession[InterviewSessionData]
  -> SelfIntroductionAgent
       -> complete_self_introduction tool
       -> timeout fallback
       -> InterviewController.transition_to_past_experience(...)
  -> PastExperienceAgent
       -> complete_past_experience tool
```

## State Machine

```text
not_started
  -> self_introduction
  -> transitioning_to_past_experience
  -> past_experience
  -> completed
```

`InterviewSessionData` explicitly tracks:

- `current_stage`
- `stage_started_at`
- `transition_reason`
- `self_intro_completed`
- `past_experience_completed`
- `prompt_sent_for_stage`
- `fallback_triggered`
- `past_experience_timeout_count`
- `past_experience_completion_reason`
- `past_experience_active_area`
- `past_experience_asked_areas`
- `past_experience_skipped_areas`

## Transition Behavior

Normal transition happens when `SelfIntroductionAgent` calls `complete_self_introduction(summary)`. If the user explicitly asks to skip ahead, `skip_to_past_experience(note)` uses the same controller path with `manual_override`.

Supported transition reasons:

- `normal_completion`
- `timeout_fallback`
- `manual_override`

The controller uses an `asyncio.Lock` and state checks so multiple transition requests cannot create multiple handoffs.

## Timeout Fallback

When the self-introduction stage starts, the controller schedules a timeout using `SELF_INTRO_TIMEOUT_SECONDS`. If no normal transition happens before the timeout:

1. The timeout calls `transition_to_past_experience(reason=timeout_fallback)`.
2. The controller verifies the current state is still `self_introduction`.
3. The controller marks `fallback_triggered=True`.
4. The timeout callback updates the LiveKit session to `PastExperienceAgent`.

If a normal or manual transition happens first, the timeout task is cancelled.

Past Experience also has a configurable inactivity fallback using `PAST_EXPERIENCE_TIMEOUT_SECONDS`.
The timer is armed only while the active state is `past_experience`, cancelled as soon as the
candidate starts speaking, and rearmed when the agent becomes idle after a prompt. Each timeout
skips the active topic in a deterministic topic cursor and asks the next fixed question, so the
agent does not repeat prompts such as "I am still waiting for your response." After
`PAST_EXPERIENCE_MAX_TIMEOUTS` consecutive inactivity fallbacks, the controller completes the stage
with `timeout_fallback` so the workflow cannot stall indefinitely.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -e .
Copy-Item .env.example .env
```

Fill in `.env`:

```text
LIVEKIT_URL=
LIVEKIT_API_KEY=
LIVEKIT_API_SECRET=
```

The default models use LiveKit Inference:

```text
LIVEKIT_STT_MODEL=deepgram/nova-3
LIVEKIT_LLM_MODEL=openai/gpt-4.1-mini
LIVEKIT_TTS_MODEL=cartesia/sonic-3
SELF_INTRO_TIMEOUT_SECONDS=90
PAST_EXPERIENCE_TIMEOUT_SECONDS=60
PAST_EXPERIENCE_MAX_TIMEOUTS=3
```

## Run
construct a .env first
use linux

Console mode:

```powershell
python -m mock_interview.main console
```

Development server mode:

```powershell
python -m mock_interview.main dev
```

Production-style mode:

```powershell
python -m mock_interview.main start
```

## Test

```powershell
python -m unittest discover -s tests
```

The tests cover:

- normal self-introduction transition
- timeout fallback transition
- duplicate transition suppression
- concurrent transition requests
- prompt guard behavior
- inactive past-experience timeout progression and completion
- deterministic past-experience topic skipping without repeated fallback prompts
- invalid state validation
