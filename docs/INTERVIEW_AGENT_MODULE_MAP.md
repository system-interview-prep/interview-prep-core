# Interview Agent module map

This is the modular boundary for the single Interview Agent.  The source tree
keeps only the package entry point at `src/modules/interviews/__init__.py`; the
implementation is grouped by delivery, application, planning, agent, core,
and evaluation responsibilities.

## Current modules reused

| Existing file | Role | Agent usage |
|---|---|---|
| `planning/planner.py` | P1 competency agenda | Pre-session planning tool |
| `planning/plan_structure.py` | deterministic plan rules | Planner domain policy |
| `planning/planner_config.py` | planning policy contract | Planner configuration |
| `planning/project_evidence.py` | CV project grounding | Candidate evidence tool |
| `planning/question_selector.py` | P2 question freeze | Frozen-question tool |
| `adapters/chat/router.py` | HTTP chat transport | Chat adapter |
| `adapters/voice/router.py` | HTTP voice and LiveKit transport | Voice adapter |
| `adapters/voice/livekit_agent.py` | LiveKit STT/TTS worker | Voice provider adapter |
| `adapters/video_calls/router.py` | Video-call lifecycle transport | Video adapter |
| `adapters/video_calls/signaling.py` | Socket.IO/WebRTC room signaling | Video transport adapter |
| `api/session_router.py` | Legacy interview session endpoints | Session API adapter |
| `core/interview_engine.py` | runtime FSM and pacing | Current deterministic kernel |
| `core/interview_types.py` | turn/stage contracts | Core domain contracts |
| `evaluation/` | P4 report generation | Async evaluation pipeline |

## LangGraph agent now wired into structured interview

| New file | Responsibility |
|---|---|
| `agent/contracts.py` | modality-neutral request/response contracts |
| `agent/state.py` | serializable command state; no DB session/provider clients |
| `agent/ports.py` | async operation boundary injected by the application layer |
| `agent/graph.py` | one StateGraph for prepare, freeze, open, respond, finish, score |
| `agent/checkpoints.py` | PostgreSQL checkpoint lifecycle on API startup |
| `application/agent_runtime.py` | binds existing planner, selector, chat runtime, evaluation to graph |
| `agent/interview_agent.py` | single graph execution entry point; keeps turn-level core compatibility |
| `adapters/input/chat.py` | chat message normalization |
| `adapters/input/voice.py` | final STT transcript normalization |
| `adapters/input/video.py` | final transcript + optional observations |

The structured endpoints for plan, question freeze, chat start/message/complete
and evaluation call `run_interview_command`. LiveKit final voice transcripts use
the same `respond` node. A final video transcript can be sent to
`POST /api/v1/interviews/sessions/{session_id}/video/message` with
`clientMessageId`, `finalTranscript`, and optional `durationSeconds`.
Video frames or behavioural observations are not scored or inferred here.

Each command has a separate checkpoint thread keyed by session, command and
event ID. A candidate-turn retry with the same ID and payload returns the
checkpointed result; a different payload is rejected when that checkpoint exists.
Other commands rerun their business guard to avoid stale plan/runtime responses. Business tables remain
the source of truth, not the LangGraph checkpoint. Production startup requires
`langgraph-checkpoint-postgres` and provisions its checkpoint tables. The
checkpoint saver uses a small PostgreSQL connection pool and strict msgpack
deserialization.

## Remaining migration work

1. Move DB orchestration from `chat_runtime.py` into smaller application services;
   the current graph delegates to it and retains its two-phase persistence.
2. Make `text_runtime.py` and the legacy `/interview/video-calls/*/chat-voice`
   endpoint use the structured graph. The latter still serves unstructured sessions.
3. Make the business write and retry path fully recoverable across a crash between
   DB commit and checkpoint. The current `clientMessageId` duplicate check does not
   fully recover every mid-turn failure, and its legacy different-payload behaviour
   differs from the graph guard; checkpointing alone is not a transaction.
4. Inject an LLM/tool port into the agent; do not call a provider directly
   from the deterministic kernel.
5. Add video observation processing as a separate coaching path. It must
   remain separate from competency and hiring decisions.
6. Define checkpoint retention/deletion together with interview transcript
   retention, since checkpoint state also contains answer text and results.
