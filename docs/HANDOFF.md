# ORBIT — Handoff

Read this before you change anything. It is short on purpose: the failure mode this
project keeps hitting is not missing features, it is a builder changing files it was
not told were load-bearing.

---

## 0. Ground truth about this repository

The plan this project runs on (Phase 0 → 6) assumed a fresh, empty repo. That is not
what is here. **Phases 0–3 already exist in this checkout**, from the build that came
before, and they are in better shape than the plan expected:

| Plan phase | Status here | Where |
| --- | --- | --- |
| 0 — repo hygiene, secret guard | **Done**, with one gap (see 0.2) | `.gitignore`, `.env.example`, `check_secrets.py`, `.githooks/pre-commit`, `setup.sh` |
| 1 — provider seam, multi-model from the start | **Done** | `providers/base.py` (`ChatProvider`), `providers/gemini.py`, `config.py`, `chat.py` |
| 2 — tools + confirmation gate in the same pass | **Done** | `tools/registry.py` (gate + audit wrap), `tools/confirmation.py`, `tools/notes.py` |
| 3 — UI from a real file, not a description | **Done**, offline-verified | `design/orbit_ui_spec.html` + `app/static/orbit.css` → `app/templates/`, `app/server.py`, `app/store.py` |
| 4 — memory, heartbeat, browser voice | **Partly done**: memory and heartbeat foundations exist; browser push-to-talk, speech replies, and Quick/Deep selection are built in Phase 4c and offline-tested | `heartbeat/`, `memory/`, `app/voice.py`, `app/static/app.js`, `runtime/heartbeat.json`, `runtime/plans.json` |
| 5 — second provider, agent mode | **Partly done**: Agent UI plus an OpenRouter chain for Deep-mode streaming exist; no general second-provider selection or sub-agents | `providers/openrouter.py`, `providers/gemini.py`, `ChatProvider`, task stream in `app/server.py` |
| 6 — `user_id` from the start | **Seeded**, no persistence yet | `app/store.py` (`DEFAULT_USER_ID`, `user_id` on `Conversation`/`Task`/`User`, `conversations_for`, `tasks_for`) |

Offline suite: all green (`sh setup.sh` re-runs them). `google-genai` 1.75.0.

### 0.1 What "fresh repo" would and would not buy you

Rewriting history has one real benefit: the previous build reached you through
`arena/xxxxx` branches and merged PRs, so the current single-commit snapshot does not
record *why* the load-bearing rules are load-bearing. That is what this document is for.
It buys you nothing on code. **Do not retype `providers/`, `tools/`, `config.py`,
`chat.py`, or `app/` to feel like you started over.** They pass their tests and their
logic is repo-independent.

### 0.2 Two Phase-0 gaps that were live until today

1. **The secret guard was never armed.** `.githooks/pre-commit` existed, but a clone has
   to be told `git config core.hooksPath .githooks` or the hook simply never runs. `setup.sh`
   now does it, and `tests/test_secret_guard.py` verifies the hook itself blocks
   credential-shaped blobs in a throwaway repo. Anything that removes `setup.sh` or its
   hooks step re-opens this.
2. **The default model was a dead one.** `config.DEFAULT_GEMINI_MODEL` was
   `gemini-2.5-flash`, which Google announced for shutdown on 2026-10-20 — ten days from
   this writing. `.env.example` already said `gemini-3.8-flash`, so the template and the
   code disagreed. On 2026-10-10 the default changed to `gemini-3-flash-preview` (the model that
   answered a live check on the builder's machine), with `gemini-3.8-flash` as the single
   fallback (`GEMINI_FALLBACK_MODEL`). Both files must change together. **This is the recurring lesson:** model
   ids rot. They must live in exactly one place (`config.py`, overridable by `.env`) and a
   builder must never hard-code one in a provider, a route, or a test.

### 0.3 Naming, so nobody "discovers" missing files

The previous build had `audit.py` and `confirm.py`. Here the same duties live in
`tools/registry.py` (`AuditLog`, `ToolRegistry`) and `tools/confirmation.py`
(`ConfirmationGate`, `ConsoleConfirmationGate`). The split is fine; the names are not the
point. Do not create `audit.py`/`confirm.py` shims to match an old memory of the layout.

### 0.4 The two chat entry points, and what was wrong with both

There are two ways to talk to Orbit and they are not the same thing:

| Entry point | Provider | Turn model | Notes |
| --- | --- | --- | --- |
| `python chat.py` | real `GeminiProvider.reply()` | one-shot before, **REPL now** | keeps history across turns, capped at `MAX_MESSAGES`; a provider error ends the turn, not the session; `/help` `/reset` `/quit`; goes through the gated tool registry, so `write_note` still asks for `yes` |
| `python -m app.server` | real `reply_stream()` lazily | SSE, streaming | Chat mode offers the six tools (`allow_tools=True`); one call per reply is queued as a card and runs only when the person clicks Approve (`app/actions.py`). Agent mode is text-only |
| `python run_demo.py` | `StubStreamingProvider` | SSE, streaming | canned replies. "Hello from Orbit — how can I help?" means you are looking at the stub, not your model |

Three defects were found and fixed while diagnosing "the terminal replies and quits,
the web box does nothing"; all three are silent-failure bugs, which is the pattern to
watch for:

1. **`chat.py` exited after one message.** It was written as a one-shot smoke test, so
   replying and quitting was correct behavior that read like a crash.
2. **Steering a task froze the bubble.** `streamTurn` pointed `targetThink` at `null` for
   every non-initial turn, so the first `thinking` chunk threw and the answer never
   rendered. Narration now lands in the bubble's own thinking block.
3. **Failure paths printed nothing.** `postSSE` had no rejection handler, no content-type
   check, and no empty-stream case, so a refused `fetch` or an empty reply looked exactly
   like a slow model. Each now shows a line in the bubble, and `bootstrapHome`/
   `bootstrapTask` report if `app.js` never loaded instead of leaving a dead input.

Also fixed: `/task/<id>` used to re-run the opening model call on *every* page load,
burning a call and appending a duplicate message. The route now passes `stream_initial`
and the client obeys it (`tests/test_app.py::test_opening_turn_is_offered_once_not_once_per_refresh`).

These are DOM/promise behaviors the Python suite could not see. `tests/test_app.py::
SilentFailureTests` now pins the server half (empty stream and a provider that raises
before the first chunk must both emit an `error` event before `done`).

---

## 1. The rules that are load-bearing

These are not style preferences. Each one exists because the previous build violated it.

1. **Nothing executes without an explicit human "yes".** `ToolRegistry.register()` wraps
   every handler: argument check → confirmation gate → run → audit. There is no fail-open
   path and no registration without a gate (`ToolRegistry.__init__` raises if
   `confirmation_gate is None`). A new tool that does not go through `registry.register`
   does not ship.
2. **The gate must survive the SDK.** `GeminiProvider._generation_config` sets
   `AutomaticFunctionCallingConfig(disable=True)`, because if the SDK auto-executes
   function calls, it bypasses the confirmation gate entirely. If you "simplify" that
   config, you have removed the safety property while all the tests still pass.
3. **Every dispatch is audited, including refusals.** Unknown tool, invalid arguments,
   declined confirmation, `tool_call_limit`, `invalid_model_response` — all recorded as
   JSON lines in `audit.log`. `AuditLog.record` runs in a `finally`, so an audit line
   exists even when the handler raises.
4. **The sheet and the stylesheet are the spec.** `design/orbit_ui_spec.html` is the
   token and component reference; `app/static/orbit.css` is the single stylesheet every
   page loads, and `DesignSpecSyncTests` fails the build if the five accent tokens in the
   two disagree. "Arena keeps changing the UI" was mostly UI being re-derived from a
   description instead of from a file, so: if a change conflicts with the sheet, the sheet
   wins or the change is explicitly proposed first. `design/orbit_mockup.html` — the old
   two-screen mockup — is gone: it described a palette and a set of screens this product
   no longer uses, and leaving it in place was how a second, parallel design survived.
5. **No placeholder is faked as live.** Only GitHub renders as connected; Linear/Vercel
   render `not_connected`. Data comes from `app/store.py`, never hard-coded in a template.
6. **The approval gate is enforced in the store, not in copy.** `Store.apply_changes`
   raises unless a task was explicitly approved; `POST /api/task/<id>/apply` returns 403.
   The sentence "No commits will be pushed without your explicit approval" is a rule, not
   a caption.
7. **Streaming never replaces gating.** `reply_stream(allow_tools=True)` may yield one
   `ToolProposal` and then stops; it never executes. The proposal is queued in
   `app/actions.py` and runs only through `PendingActions.decide()` after an explicit
   Approve on the card, which uses the same `ToolRegistry.dispatch` (gate → run → audit).
   `DeferredConfirmationGate` answers only for the decision the person just made. Do not
   let a streamed proposal run on its own, and do not replace the card with a blocking
   prompt.
8. **Every turn ends visibly.** Each stream terminates in an `answer`, or an `error`
   saying why there is none, then `done` — server-side (`_sse_response`) and client-side
   (`postSSE`'s `onFinish`, including a rejected `fetch` and a non-event-stream response).
   A silent stream is indistinguishable from a hung UI, and that ambiguity has already
   cost this project a debugging session. Same rule in the terminal: a model error prints
   and keeps the REPL open, it does not end the program.
9. **Know which entry point you launched.** `run_demo.py` is the stub and says so in its
   banner and in its replies; never conclude "the UI is broken" or "the model is broken"
   before checking which of the two you started.
10. **Every wait has a deadline and a label; every setup failure names a layer.**
   `config.DEFAULT_REQUEST_TIMEOUT_SECONDS` (60, overridable via `ORBIT_HTTP_TIMEOUT`) is
   passed to `types.HttpOptions(timeout=seconds * 1000)` on the client, so it covers
   `reply()` and `reply_stream()` alike. The `* 1000` is milliseconds and is pinned by a
   test — do not "simplify" it, and do not delete it: leaving `timeout` unset is **not**
   neutral. The SDK then hands `timeout=None` to httpx, and an explicit `None` disables
   the timeout instead of falling back to httpx's 5s client default, so a blocked
   connection blocks forever. That is the bug this rule exists for, and it was verified
   offline against a socket that accepts and then stays silent: hangs without a timeout,
   raises at the deadline with one. Before blocking, `chat.py` prints
   `waiting on <model> — Ctrl-C cancels this turn`, and `python chat.py --check` walks
   config → network → model and names the first failing layer. `--check` masks the key to
   its first four characters and never raises a traceback.
11. **The web launchers never expose Werkzeug's debugger.** `run_app()` defaults to
   `127.0.0.1` and always sets `debug=False`. A container or sandbox preview may opt into
   an external bind with `ORBIT_HOST=0.0.0.0` (and `PORT` for the port); do not turn on
   debug mode, especially on an externally reachable address.
12. **Quick/Deep and voice are additions, not new chat paths.** Quick/Deep affects the
   streamed UI provider request only; Quick keeps the Gemini primary/fallback chain, while
   Deep tries the three configured OpenRouter model slots, then its configured auto-router,
   then Gemini's primary as Europa. Failover is allowed only before output starts, and Agent
   activity identifies the actual slot. Keep typed input as the default. Voice transcripts re-enter `/api/chat` or
   `/api/task/<id>/stream`; they must not bypass tool approval, task approval, history, or
   safety gates. Audio uploads are bounded and temporary, and generated MP3 bytes are
   streamed from the server rather than synthesized in the browser.

---

## 2. What is honestly unverified

Everything below passes offline with the model stubbed. None of it has been proven against
a live key in this checkout. Say so in any status report instead of implying otherwise.

- A real Gemini call through `chat.py` (`python chat.py` with a key in `.env`, then several
  turns — multi-turn history carrying is exercised only against a fake provider).
- A real tool-call decision end-to-end: model proposes `write_note` → console prompt →
  `yes` → line appended → audit line.
- A real decline path, and the non-interactive stdin decline (both are stub-tested).
- Whether to move from the `<thinking>…</thinking>` narration convention to the model's
  native thinking parts. Current behaviour is honest *narration*, not raw reasoning.
- Whether `gemini-3.8-flash` accepts the exact call shape used here
  (`system_instruction`, `function_declarations`, no `temperature`/`top_p`/`top_k`/
  `thinking_budget` — several of those are being deprecated as parameters, so leaving
  them out is deliberate; do not add them back).
- **The setup check has never run against a live endpoint.** `python chat.py --check` was exercised with an injected provider and an injected connector, and its mechanism (that an unset `HttpOptions.timeout` means an unbounded wait) was proven against a silent local socket. What is unproven is the shape of a *real* failure: a genuinely firewalled host, a real `401`, a real `404`. The hint text in `chat._model_hint` is reasoned from the error names, not observed from one.
- **Nothing in §0.4 has been seen in a real browser.** The client behavior was verified by
  serving the app and driving the rendered pages in jsdom (Enter and click both reach
  `/api/chat`, the reply lands in the thread, each failure prints a notice, the task view
  steers without throwing). That proves the JS, not the network path. SSE through the
  Arena preview proxy could not be probed from inside the sandbox at all — egress to the
  preview host is blocked — so a "web box does nothing" report should be triaged by
  checking whether the `POST /api/chat` line ever appears in the server log. No log line
  means browser or proxy; a log line with no visible reply means streaming/buffering.
- **Reply actions (copy/regenerate/ratings), the collapsed sidebar, and Analytics are
  offline-verified.** `tests/test_message_actions.py` drives the real `app.js` in jsdom
  (toggle persists, ratings POST once, a mid-conversation regenerate re-sends the same
  turn with the id) and the Flask routes with a scripted provider. What is *not* proven:
  that a live model returns a genuinely different answer to the regenerate prompt (the
  instruction is sent, the difference is the model's to make), and that a long-lived
  `audit.log` stays parseable — the reader is tested against synthetic logs, including
  torn and unparseable lines, not against months of real ones.
- **Voice is offline-tested with fakes, not live end-to-end.** The browser microphone,
  real Whisper model download/transcription, Edge TTS network stream, and audio playback
  have not been exercised in a real browser. Tests cover the service adapters and Flask
  endpoints without contacting Whisper or Microsoft. The preview must be HTTPS (or
  localhost) for microphone access.

---

## 3. Known debt, with the decision already made

- **`notes.txt` is ignored by Git while being the user's data.** `write_note` appends to
  `notes.txt` at the repo root and `.gitignore` lists it. `tools/notes.py`'s tool
  description still says "the local notes.txt". Phase 4 should move note storage into the
  versioned memory store and leave `audit.log` as disposable local state. Until then do
  not delete or relocate `notes.txt` — it is somebody's notes.
- **`Store` is in-memory scaffolding.** The seeded conversations and the example task `T1`
  are seeded data the templates render, but `home_thread`/mode changes die with the
  process. Phase 4 replaces this with the persistent store; keep the read paths
  (`conversations_grouped`, `get_task`, `conversations_for`, `tasks_for`) as the interface
  and swap what is behind them. The task's *steps, files and activity* are no longer
  seeded at all — they come from `Task.run`, which is `None` until the task runs
  (§5.4.4). Do not add seed data back to make the panel look busier.
- **Ratings are a private local signal, not a feedback system.** `message_rated` goes to
  `audit.log` and nowhere else — no form, no category, no aggregation across people, and
  nothing that reads it except `/analytics`. Do not grow it into a feedback board; if a
  future build wants reasons or comments, that is a different feature with its own
  consent story, not this one.
- **`user_id` is seeded, not threaded.** `DEFAULT_USER_ID` comes from the environment and
  every record carries it, so the column exists before the second user does. Do not build
  auth, login, or multi-tenancy now — just keep writing queries through the
  `*_for(user_id)` helpers so the seam stays real.
- **One branch, always.** Work on a branch that merges into `main` within the session or is
  deleted. The UI regressions traced back to "which version is current", not to bad code.

---

## 4. Phase 0/1 — the prompt to hand the next builder

Copy everything inside the block. It is written for a repo that already contains this
checkout's `providers/`, `tools/`, `config.py`, `chat.py`, `app/`, and `design/` — because
retyping tested code is how a project loses it.

```text
You are working on ORBIT, a personal AI assistant, in an existing repository. Read
docs/HANDOFF.md and README.md first. They contain rules that are load-bearing because
they were each broken once already. Do not restructure files to match a layout from a
previous project.

Goal of this phase: a clean repo foundation plus a provider abstraction that is
multi-model from the start, proven with one model in chat mode. Most of it already
exists in this repo. Your job is to VERIFY, not to rewrite.

Do these in order. Stop and report after each; do not batch them into one commit.

1. Repo foundation.
   - Run: sh setup.sh. It must install deps, arm .githooks, create .env from
     .env.example if missing, and pass the offline suite in full.
   - Confirm .gitignore covers .env, __pycache__/, *.pyc, audit.log and runtime state,
     and that the real .env is not tracked: it must not appear in git ls-files. Never
     stage or commit a .env that holds a real key, not even temporarily.
   - Confirm git config core.hooksPath prints .githooks in your clone. If not, the
     secret guard is decorative and that is a bug to fix, not a detail.
   - Confirm check_secrets.py rejects a credential-shaped blob. Do not paste a real or
     real-shaped key into any file, commit message, or this chat to prove it; the
     existing test constructs one at runtime in a throwaway repo for exactly that
     reason.

2. Provider seam.
   - Read providers/base.py. ChatProvider exposes reply() and reply_stream(); the
     StreamChunk/ChatMessage types are provider-neutral. No Gemini types may appear in
     base.py. If you find any, that is a seam leak and the fix belongs here, not in
     call sites.
   - Confirm config.DEFAULT_GEMINI_MODEL and .env.example agree, and that no provider,
     route, template, or test hard-codes a model id. Check the id is still
     generally available today; if it has been deprecated since this doc was written,
     update both places in one commit and say which date you checked against.
   - Confirm GeminiProvider only builds a real client when no client is injected, so
     tests stay offline.

3. The gate, from tool #1.
   - Read tools/registry.py and confirm register() cannot produce a tool that skips
     confirmation, and ToolRegistry.__init__ rejects a missing gate. Confirm
     automatic function calling is disabled in the Gemini config (handoff §1.2).
   - Run one live check with your key: ask for a note, approve at the console, and show
     the appended notes.txt line and the matching audit.log entry. Then run it again and
     decline, and show that nothing was written. Both cases, or say you did not test them.

4. Prove the seam with a second provider, no UI work.
   - Add providers/stub.py (deterministic, offline, no network) and a test that runs the
     same chat turn through GeminiProvider and the stub and asserts the interface, not
     the prose. This is the one place where new code is preferred over existing code:
     multi-model is only real when a second implementation exists.
   - Do not add Groq, a second Gemini tier, agent mode, or memory yet.

Constraints:
- No secrets, tokens, or key-like strings in any committed file, log, or screenshot.
- No placeholder rendered as if it were live.
- Do not touch design/orbit_ui_spec.html, app/static/orbit.css or app/templates/ in this phase.
- Commit messages say which rule a change protects when it touches one.
- Branch: work toward main in the same session; do not leave branches dangling.

Finish by reporting: what you ran, what passed, what you did NOT verify, and the exact
command you used. If you changed any file listed in handoff §1, explain why the
property still holds.
```

---

## 5. Phase 4 and 5 — current status and constraints

The shared store seam remains `app/store.py`; records keep their `user_id` field.
Heartbeat state is local runtime state (`runtime/heartbeat.json`, `runtime/plans.json`;
both ignored). Voice is an input/output path on the same provider seam, not a second
brain.

### 5.1 Heartbeat as built (Phase 4b)

- **Where it runs:** one daemon thread inside the web process, started by `run_app` at
  boot. `create_app()` never builds it, so tests do not touch `runtime/`.
- **Checks:** missing plan (after `HEARTBEAT_PLAN_HOUR`, default 10, valid 8-21) and
  check-in gap (`HEARTBEAT_CHECKIN_GAP_DAYS`, default 2). Plan hour 8-21 keeps the check
  outside quiet hours (22:00-08:00), so it can always fire. A home-chat message is the check-in.
- **Once a day:** each check records its local date in `runtime/heartbeat.json` and never
  fires twice on one date. A restart reads that file, so it neither refires nor resets the
  check-in timer. A first run starts the timer rather than nagging.
- **Quiet hours** hold automatic checks. A direct check (`POST /api/heartbeat/check`)
  bypasses quiet hours only; the once-a-day rule still applies.
- **Push:** `NTFY_TOPIC` (set in `.env`, never hard-coded or logged) receives one plain
  HTTP POST per notice. Push failures are recorded on the notice and in the audit log,
  without the topic.
- **Pause:** a sidebar switch and the gated `pause_heartbeat` / `resume_heartbeat` tools.
  Pause is stored in the state file, so the terminal chat and the web scheduler agree.
- **Audit:** `heartbeat_notice_fired`, `heartbeat_notice_dismissed`, `heartbeat_paused`,
  `heartbeat_resumed`, `heartbeat_push_failed`. These record ids, kinds and timings, never
  message text or the topic.
- **Known limits:** the scheduler only runs while the web app runs; the terminal chat
  does not run checks. The six tools are available in the terminal chat (`chat.py`) and in
  the browser's Chat mode (cards, see README "Browser tools"). The sidebar switch is the
  browser's pause control. ntfy.sh could not be reached from the build sandbox, so the phone
  push was verified against a local HTTP server only.
### 5.2 Browser voice and Quick/Deep (Phase 4c)

- **Typed chat remains the default.** Press-and-hold mic controls record through the
  browser's `MediaRecorder`; release uploads the clip to `/api/voice/transcribe`. The
  returned transcript is placed into the existing composer and sent through the ordinary
  `/api/chat` or `/api/task/<id>/stream` route. A chevron by the home mic opens a focused
  voice-chat view: it reuses that same mic element and pipeline, enlarges the Orbit mark
  for idle/listening/thinking/speaking states, and returns to text chat through Back.
- **Transcription is local to the server.** `app/voice.py` lazily loads
  `faster-whisper` (`base`, CPU, `int8` by default). The browser upload is capped at
  25 MiB, recordings stop at two minutes, and audio is stored in a temporary file only
  during transcription; it is removed in `finally`. The first call downloads the model
  to the standard Hugging Face cache. Settings: `ORBIT_WHISPER_MODEL`,
  `ORBIT_WHISPER_DEVICE`, `ORBIT_WHISPER_COMPUTE_TYPE`.
- **Voice failures log their real cause, not just the fallback sentence.** The browser
  only gets one line, so `VoiceServiceUnavailable` carries a short `reason` for the UI
  *and* the chained original exception. `run_app` configures a console handler on
  `orbit.*` (`ORBIT_LOG_LEVEL`, default INFO) — without it `log.exception` fell through
  to logging's last-resort handler, which prints the bare message and drops INFO — and
  the layer that finds the cause logs the traceback once, with a dependency report
  (engine/decoder versions, bundled FFmpeg libraries, `ffmpeg` program present or not
  and whether it is needed, model cache state). `AuditLog.record_error` writes the same
  failure to `audit.log` as `voice_transcription_unavailable` /
  `voice_transcription_failed` / `voice_synthesis_unavailable` / `voice_synthesis_failed`
  with `error_type`, `error`, `error_chain` and `traceback`; a successful load is
  `voice_model_loaded`. Client-side rejects (empty, undecodable) stay 400s and are not
  audited as faults.
- **ffmpeg is not a dependency for reading the browser's clips.** `faster-whisper` 1.x
  decodes through PyAV, whose wheels bundle the FFmpeg libraries (checked on this
  machine: a MediaRecorder-shaped WebM/Opus clip decoded with no `ffmpeg` program on
  PATH, and the Silero VAD model ships inside the wheel so `vad_filter=True` works
  offline). Only a missing/broken PyAV, or a codec the bundled libraries lack, is
  reported as a decoder problem — and then the message names ffmpeg as the fix.
  Confirmed 2026-10-12: `huggingface.co` is unreachable from the build sandbox and no
  model cache exists, so `base` cannot load here; that is now visible at boot
  (`model_cached=False`) and in `audit.log` instead of surfacing as a generic message.
- **Voice replies are streamed.** A turn submitted by push-to-talk requests
  `/api/voice/speech`; Edge TTS streams MP3 using `en-US-GuyNeural` at `-15Hz`.
  The browser uses Media Source Extensions where MP3 streaming is supported and a Blob
  playback fallback otherwise. Typed turns remain text-only.
- **Quick/Deep is per stream, shared across both screens.** Quick uses the configured
  primary Gemini model and `max_output_tokens=512`. Deep uses `4096` and tries the three
  configured OpenRouter model ids in order, then the configured auto-router, then Gemini's
  primary as Europa. Failover is allowed only before the first visible text/tool delta;
  afterwards an error is surfaced rather than restarting and duplicating output. The
  `OPENROUTER_MODEL_1/2/3` values and `OPENROUTER_FALLBACK_MODEL` live in `.env`; no pinned
  OpenRouter model id belongs in provider code. Agent activity uses Io, Ganymede, Callisto
  for the pinned slots. The selection lives in `Store` for the web process lifetime.
- **Agent mode streams through the same provider seam.** Task replies use narration and
  optional model-attempt labels; Chat mode remains the only browser mode that offers the
  six gated tools. Deep Chat-mode tool proposals can come from OpenRouter, but are still
  returned as proposals and are never executed before approval. There are no sub-agents.
  Keep approvals enforced by `Store` and `PendingActions` before any future execution path.

### 5.3 Lagos time context, empty-stream audit, and UI polish

- Every CLI, home-chat, and task-agent prompt gets a freshly computed `Africa/Lagos`
  date/time context (`memory/time_context.py`). It uses an aware instant and explicitly
  tells the model that this context is authoritative for time-sensitive answers.
- A Gemini stream that completes without visible text logs `model_empty_response` with
  safe response metadata: candidate finish reasons, function-call names and
  function-call-without-text flags, prompt/candidate safety blocks and ratings, response
  ids/model version, and usage counters. Function-call arguments and prompt/reply text are
  excluded. The browser's user-visible empty-reply fallback is separately recorded as
  `model_returned_nothing`; provider exceptions are recorded as `model_stream_failed`.
- Sidebar search filters local history; conversation/task headers show their current
  names; settings/account controls open an account summary; Orbit message actions support
  copy, latest-turn regenerate, and local feedback state. The send control becomes Stop
  during streaming. Motion respects `prefers-reduced-motion`.
- Composer attachment controls currently open the file picker but do not upload files to
  the model. Selected filenames are shown locally with that limitation stated. The GitHub
  shortcut opens GitHub; it does not choose a repository.

### 5.4 Welcome screen, truthful status labels, follow-up chips, and the dynamic Agent panel

Four UI behaviours were rebuilt around one rule: **a status, a step or a suggestion that
cannot be traced to something real does not ship.** Each of the four had a version of the
same failure before this — a thing that looked like information but was decoration.

**Files:** `app/starters.py`, `app/followups.py`, `app/taskplan.py` (new);
`app/store.py`, `app/server.py`, `app/static/app.js`, `app/templates/{layout,home,task}.html`;
tests in `tests/test_taskplan.py`, `tests/test_followups.py` and `tests/test_app.py`.

#### 5.4.1 Welcome screen (empty conversations only)

- Rendered when — and only when — `Store.active_conversation_id is None` **and** the home
  thread is empty. A returning conversation never sees it again; the server decides, not
  the client (`home()` passes `welcome=…`).
- Heading "Your ideas, in orbit.", subtitle "Think it. Build it. Let's work on it
  together.", and the time-based greeting above it.
- The three shortcuts are real prompts (`app/starters.WELCOME_STARTERS`), sent through
  `sendPrompt()` → the ordinary composer path. They are not labels that fill the box.
- **Orbital motion:** the same orbit mark carries both states. Idle on the welcome screen
  it turns slowly (`rings-slow`, 20s); when a turn starts the block takes
  `is-responding`, which hides the heading and shortcuts, shrinks the mark and re-times it
  (`1.6s`). The status bar sets `no-rings` in that case so there is never a second mark.
  `prefers-reduced-motion` stops both — the mark stays visible, just still.

#### 5.4.2 Status labels while generating

- `TurnStatus` in `app.js` has exactly three truthful phases: `Thinking…` → `Still
  thinking…` (after 12s with nothing back) → `Writing your answer…` on the first chunk.
  Agent mode says `Working through it…` while narration arrives. The label clears on
  `done`, on `error`, and on stop.
- **"Analyzing the provided files…" deliberately does not exist.** Attachments are still
  not uploaded to the model (§5.3), so that label would be a lie. It stays out until
  uploads are real. A test pins this (`TurnStatusLabelTests`).
- The task topbar's old standing `<div class="working-indicator">Orbit is working</div>`
  is gone: the indicator is now hidden unless a run is in flight, and it shows the real
  operation.

#### 5.4.3 Follow-up chips

- `app/followups.py` offers chips for exactly one observable property: the last Orbit
  reply contains a fenced code block. Then it offers *Explain this code · Find potential
  bugs · Improve implementation*, each a real prompt naming the work.
- Everything else offers nothing, and the row is hidden rather than left empty. Chips are
  computed server-side (so they are testable offline), streamed as a `followups` event
  **after** `done`, rendered on page load from the stored thread, and cleared the moment a
  new turn starts or is regenerated. A failed or empty turn offers none.

#### 5.4.4 The Agent panel is planned, never seeded

The old panel shipped with the task: four fixed steps (`Read repository context` →
`Extract order status logic` → `Run tests` → `Ready for your review`) with invented
details ("6 files scanned", "4 tests passing"), two invented diffs (`orders.py 38 → 61`)
and three invented activity lines. All of it is gone, and `Store.Task` no longer has
`steps` or `changes` fields.

- `app/taskplan.plan_steps(request)` builds the checklist from the request's wording:
  intent keywords plus the leading clause, so "Fix the crash in checkout.py" plans
  *Reproduce the failure* → *Fix the root cause* while "Review the diff in orders.py"
  plans *Read the code closely* → *Report what you find*. A request that names the work
  does not get a duplicate generic step beside it.
- A step only leaves `pending` when `apply_narration()` finds one of its keywords in
  Orbit's narration, and the matching sentence is kept as the step's evidence line — so
  every tick can be read back against what Orbit actually said. Steps Orbit never
  mentioned stay `pending`. A finished turn completes the step it was narrating.
- `files_in()` + `add_files()` record files the request named or Orbit mentioned, each
  labelled `request` or `orbit`. No file is ever guessed, and a task where neither
  happened shows the empty line instead.
- The run (`TaskRun`) lives on `Task.run` and is `None` until the task runs. The panel is
  rendered from it and nothing else: **a task that has never run has no panel at all** —
  the request and a start control are the whole of it.
- `POST /api/tasks` creates a task from a request typed in the UI; the task page's
  `+ New task` opens that composer. The title comes from the request text (no model call).
- The stream carries the panel: `task_state` events (opening state, then only on change)
  with steps, files, operation, approval state and the activity list. Activity rows are
  now real events — model status lines, step transitions with their evidence, and turn
  ends — and the section is hidden when there are none.
- **Approvals.** The rule line stays ("No commits will be pushed without your explicit
  approval"), and the section reports the task's real state. There is no Approve button:
  with nothing proposed there is nothing to approve, and a control that only looks like a
  gate is worse than the honest line. The gate itself is untouched — `Store.apply_changes`
  raises without an explicit approval and `POST /api/task/<id>/apply` answers 403, both
  pinned by `ApprovalGateTests`. **If a future build proposes real changes, the control
  and the panel section must arrive together, through `PendingActions`.**

### 5.5 Reply actions, a collapsed sidebar, and Analytics read from audit.log

**Rule for this phase:** a control that cannot act honestly does not ship, and a view may
only show what something already recorded. Nothing here added new tracking — Analytics
reads the existing `audit.log`, and the one new record (`message_rated`) is the ratings
themselves.

**Files:** `app/analytics.py` (new); `app/store.py`, `app/server.py`,
`app/static/app.js`, `app/templates/{layout,home,task}.html`,
`app/templates/{_sidebar,analytics}.html` (new); tests in `tests/test_message_actions.py`.

#### 5.5.1 Message ids, and reply actions on every Orbit reply

- `ChatMessage.id` (`m-<10 hex>`) is assigned once, on append (`Store.append_message`,
  `Store.add_chat`). `Store.find_message(id)` locates a message across every conversation
  and task chat; `Store.replace_message(owner_id, id, body)` rewrites the body in place,
  keeping position and id. The id reaches the client as the `message` SSE event
  immediately before `done`, and the template also renders it, so a rating survives a
  reload.
- The four controls (copy · regenerate · thumbs up · thumbs down) sit under **every** Orbit
  reply and never under a user message. `.message-actions` is always visible at
  `opacity: 0.55` (full on hover/focus), which replaces the old hover-only rule: a phone
  cannot hover, so hover-only meant "unavailable on mobile".
- **Regenerate is per message, not per conversation.** `POST /api/chat` accepts an optional
  `message_id` naming any Orbit reply; that reply is rewritten in place, the turns after it
  are left alone, and the model is told (`REGENERATE_INSTRUCTION`) to answer the same
  question genuinely differently. Without `message_id` the old newest-reply behaviour
  remains. `POST /api/task/<id>/stream` has the same `message_id` branch, and both answer
  409 when the named message is not an Orbit reply or has no user turn behind it.
- **A rating is one line, not a form.** `POST /api/messages/<id>/rating` with
  `{"rating": "up"|"down"}` writes `message_rated` (message id, rating, surface, owner id,
  reply length — never the text) and answers 400 for anything else, 404 for an id that is
  not a stored message. The client logs a fresh press only: un-pressing the active rating is
  a local change of mind, and switching logs the new rating.

#### 5.5.2 The sidebar collapses

- `«`/`»` in the sidebar head toggles `.sidebar--collapsed`: a 58px rail that keeps the
  brand mark, new conversation, search, history (icon rows, scrollable), Analytics,
  reminders, connected apps and settings clickable, each with a `title` naming it. The
  toggle carries `aria-pressed` and swaps its own label between "Collapse sidebar" and
  "Expand sidebar".
- The choice lives in `localStorage` under `orbit.sidebar.collapsed` and is applied **before
  the first paint** (a small inline script in `layout.html` marks the root, `app.js` swaps
  it for the real class), so a collapsed sidebar never flashes open. A blocked
  `localStorage` (private mode) leaves the sidebar expanded and the toggle working.
- The three screens share one sidebar include (`app/templates/_sidebar.html`). Its route
  (`/analytics`) has no heartbeat view, so the reminders row renders empty there rather than
  showing an invented state.

#### 5.5.3 Analytics

- `app/analytics.build_analytics(path, *, now=None, window_days=7)` returns a report from
  the JSON lines in `audit.log`: `messages_per_day` (today first, `bar_percent` relative to
  the busiest day), `models` (model id + persona name + count), `heartbeat_fired`,
  `heartbeat_dismissed`, `checkins`, `ratings_up`, `ratings_down`, a one-line `summary`, and
  `notes`. Torn and unparseable lines are skipped; naive timestamps are read as UTC.
- Counted events: `chat_message_received` (now written by both chat paths, home and task),
  `model_answered`, `message_rated`, `heartbeat_notice_fired`,
  `heartbeat_notice_dismissed`, `heartbeat_checkin_recorded`. New model-answer events
  record the persona actually used (important when one Gemini id is Titan in Quick and
  Europa as Deep's last resort). Older records use `providers.personas.model_persona_map()`;
  an unconfigured model keeps its own id as the label.
- The view (`GET /analytics`) is neutral by construction: counts, days and shares, with no
  streak, comparison, target or "you should" wording. A missing log is reported as having
  nothing to count, not as a failure.
