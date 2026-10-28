# Voice Conversation Architecture

## Product objective

The voice interview must feel like one continuous human conversation while the
question plan, scoring boundary, audit transcript, and selected locale remain
deterministic. Naturalness is therefore not a single TTS setting. It is the
combined result of microphone cleanup, turn detection, intent handling,
decision latency, wording, synthesis, playout, and recovery.

## Current measured baseline

Evidence from session `52bc5090-b304-431d-861e-a5bc7a5309f9`:

| Stage | Observed |
| --- | ---: |
| Adaptive interruption decision | 426-539 ms |
| Interview Core response | 2,623-2,695 ms |
| ElevenLabs time to first audio | 178-238 ms |
| Opening audio duration | 6,975 ms |

The dominant delay is Interview Core, not speech synthesis. A probe could also
require two sequential model calls: evaluation followed by probe generation.
The evaluator now returns a guarded `probe_question` in the same structured
response, with the old generator retained as a fallback.

## Architecture decision

Keep the cascaded `STT -> deterministic Interview Core -> TTS` pipeline rather
than replacing it with an unconstrained speech-to-speech model. The cascaded
pipeline preserves exact approved questions, locale enforcement, transcripts,
idempotency, and evaluation boundaries. Optimize the stages and overlap safe
work instead.

```text
browser audio
  -> echo/noise/gain processing
  -> VAD + STT turn boundary
  -> transport intent gate
  -> one-pass Core evaluation/next action
  -> spoken-language renderer
  -> streaming TTS
  -> synchronized playout + canonical transcript
```

## Responsibilities by layer

### Audio input

- Browser echo cancellation, noise suppression, and automatic gain control stay
  enabled.
- Add server-side voice isolation only after an A/B test on real Vietnamese
  recordings; aggressive filtering can remove quiet consonants.
- Measure false activation separately for headphones, laptop speakers, and
  external microphones.

### STT and endpointing

- Lock the provider language to the session locale. English technical words are
  vocabulary inside a Vietnamese turn, not permission to switch the session.
- Use AssemblyAI Universal-3.6 Pro with `turn_detection="stt"`; align provider
  and LiveKit VAD thresholds.
- Preserve complete interim/final words and confidence in telemetry. Exact-match
  correction is allowed only for proven short artifacts such as `Yeah -> Dạ`.
- Do not lower silence thresholds globally to chase latency. Interview answers
  contain thinking pauses; forced endpoints fragment answers.

### Conversation policy

Transport intents are resolved before Interview Core:

| Situation | Expected behavior |
| --- | --- |
| `Dạ`, `ừm` while AI speaks | Backchannel; do not advance the plan |
| `Hello`, `alo` around playout | Greeting/recovery; replay the canonical question |
| `Khoan`, `chờ chút` | Yield the floor and wait |
| `Bị đứt đoạn`, `nói lại` | Replay; never persist as an answer |
| Candidate corrects the AI | Stop, acknowledge briefly, process as content |
| Candidate asks what the question means | Clarify once without leaking rubric |
| Candidate asks to stop | Explicit confirmation flow |
| Background noise/echo | False interruption recovery; no new transcript turn |

An interrupted transcript is a playout artifact, not a new canonical question.
For a backchannel or probable TTS echo, resume only the unheard suffix when the
playout prefix is reliable; replay the canonical question only when the SDK
cannot align the delivered prefix. Neither path may call Interview Core.

The deployed acoustic stack is browser AEC/noise suppression/AGC, followed by
LiveKit BVC on the worker, then Silero VAD (`0.50` activation, `0.35`
deactivation, `200 ms` minimum speech). A real interruption must last `500 ms`;
an interruption with no lexical transcript resumes after `1.0 s`. BVC can be
disabled explicitly for non-Cloud deployments, but the worker must log that it
is falling back rather than silently claiming noise isolation.

### Interview Core

- Make one structured model call per normal turn. It returns intent,
  sufficiency, missing aspect, neutral acknowledgement, and an optional single
  probe.
- Validate every generated probe for locale, length, one-question scope, and
  rubric leakage. Fall back to the legacy guarded generator only when invalid.
- Keep acknowledgements neutral and short. Avoid repetitive praise and do not
  reveal scoring.
- Separate semantic content from spoken rendering in the next iteration so chat
  can remain precise while voice uses shorter clauses and natural punctuation.

### TTS

- Flash v2.5 and WebSocket auto mode are appropriate for low latency when the
  complete utterance is already available.
- A native Vietnamese voice is mandatory for production quality. The current
  generic voice is English-trained and will retain an accent even with
  `language="vi"`.
- Tune stability/speed only after selecting the correct voice. Voice identity
  has a larger effect on Vietnamese naturalness than small stability changes.
- Keep questions concise. Punctuation and sentence boundaries should carry
  pauses; avoid manually splitting one question into independent TTS requests.

## Latency service levels

Measure per turn and report p50/p95, not only averages.

| Metric | Target p50 | Target p95 |
| --- | ---: | ---: |
| True barge-in stop | <= 450 ms | <= 700 ms |
| Final transcript after speech | <= 500 ms | <= 900 ms |
| Core decision, normal next question | <= 900 ms | <= 1,500 ms |
| Core decision, probe | <= 1,200 ms | <= 2,000 ms |
| TTS first audio | <= 250 ms | <= 450 ms |
| End of user speech to first AI audio | <= 1,400 ms | <= 2,300 ms |

If Core exceeds the p95 budget, use a delayed neutral listening cue rather than
an immediate filler on every turn. The cue must not be persisted as interview
content and must never overlap the next question.

## Evaluation suite

The text golden tests are necessary but not sufficient. Add a versioned audio
corpus with consented speakers and expected event timelines:

- At least 20 Vietnamese speakers across North/Central/South accents.
- Quiet room, fan noise, cafe speech, laptop-speaker echo, and headset audio.
- Short acknowledgements, code-switching technical answers, long thinking
  pauses, self-correction, clarification, repeat, hold, abort, and genuine
  barge-in.
- For every sample: expected transcript, accepted variants, turn boundary,
  transport intent, whether Core is called, expected language, and latency
  budget.

Release gates:

1. No locale switch in 100% of locale-lock scenarios.
2. No Core advance for 100% of repeat/hold/backchannel scenarios.
3. No fragmented canonical AI question after recovery.
4. Vietnamese short-utterance intent accuracy >= 98% on the golden corpus.
5. Word error rate and entity error rate reported separately for Vietnamese
   ordinary speech and English technical terms.
6. End-to-first-audio p95 meets the table above on the deployment region.

## Phased implementation

1. **Correctness:** locale lock, replay/hold/backchannel policy, canonical
   transcript repair, and state-transition golden tests.
2. **Latency:** single-pass Core decisions, per-stage timing, prompt/context
   reduction, then delayed listening cues only for slow-tail turns.
3. **Voice quality:** native Vietnamese voice, spoken renderer, pronunciation
   dictionary, and regional-accent listening tests.
4. **Acoustic robustness:** real audio corpus, enabled server noise isolation,
   device profiles, and threshold tuning from measured false positives.
5. **Continuous quality:** dashboard p50/p95, STT corrections, interruption
   confusion matrix, replay rate, and provider errors by release.

## Primary references

- LiveKit turn tuning: https://docs.livekit.io/agents/logic/turns/tuning/
- LiveKit AssemblyAI STT: https://docs.livekit.io/agents/models/stt/assemblyai/
- LiveKit latency metrics: https://docs.livekit.io/testing/observability/data/
- ElevenLabs latency: https://elevenlabs.io/docs/api-reference/reducing-latency
- ElevenLabs voice/language guidance:
  https://elevenlabs.io/docs/eleven-creative/playground/text-to-speech

