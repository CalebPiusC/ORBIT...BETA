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
| 3 — UI from a real file, not a description | **Done**, offline-verified | `design/orbit_mockup.html` → `app/templates/`, `app/server.py`, `app/store.py` |
| 4 — memory, heartbeat, voice | **Not started** | — |
| 5 — second provider, agent mode | **Not started** | seam exists: `ChatProvider` + `max_tool_rounds` |
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
   code disagreed. Now both say `gemini-3.8-flash`. **This is the recurring lesson:** model
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
| `python -m app.server` | real `reply_stream()` lazily | SSE, streaming | text only — **no tool dispatch**, by design (§1.3) |
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
4. **The mockup is the spec.** `design/orbit_mockup.html` is the literal structure and
   palette for the two screens. "Arena keeps changing the UI" was mostly UI being
   re-derived from a description instead of from a file. If a change conflicts with the
   mockup, the mockup wins or the change is explicitly proposed first.
5. **No placeholder is faked as live.** Only GitHub renders as connected; Linear/Vercel
   render `not_connected`. Data comes from `app/store.py`, never hard-coded in a template.
6. **The approval gate is enforced in the store, not in copy.** `Store.apply_changes`
   raises unless a task was explicitly approved; `POST /api/task/<id>/apply` returns 403.
   The sentence "No commits will be pushed without your explicit approval" is a rule, not
   a caption.
7. **Streaming never replaces gating.** `reply_stream()` is text-only, added *beside*
   `reply()`. Function-calling turns keep using `reply()` so the gate runs synchronously.
   Do not "improve" the UX by streaming a tool-using turn until the gate has a UI path.
8. **Every turn ends visibly.** Each stream terminates in an `answer`, or an `error`
   saying why there is none, then `done` — server-side (`_sse_response`) and client-side
   (`postSSE`'s `onFinish`, including a rejected `fetch` and a non-event-stream response).
   A silent stream is indistinguishable from a hung UI, and that ambiguity has already
   cost this project a debugging session. Same rule in the terminal: a model error prints
   and keeps the REPL open, it does not end the program.
9. **Know which entry point you launched.** `run_demo.py` is the stub and says so in its
   banner and in its replies; never conclude "the UI is broken" or "the model is broken"
   before checking which of the two you started.

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
- **Nothing in §0.4 has been seen in a real browser.** The client behavior was verified by
  serving the app and driving the rendered pages in jsdom (Enter and click both reach
  `/api/chat`, the reply lands in the thread, each failure prints a notice, the task view
  steers without throwing). That proves the JS, not the network path. SSE through the
  Arena preview proxy could not be probed from inside the sandbox at all — egress to the
  preview host is blocked — so a "web box does nothing" report should be triaged by
  checking whether the `POST /api/chat` line ever appears in the server log. No log line
  means browser or proxy; a log line with no visible reply means streaming/buffering.

---

## 3. Known debt, with the decision already made

- **`notes.txt` is ignored by Git while being the user's data.** `write_note` appends to
  `notes.txt` at the repo root and `.gitignore` lists it. `tools/notes.py`'s tool
  description still says "the local notes.txt". Phase 4 should move note storage into the
  versioned memory store and leave `audit.log` as disposable local state. Until then do
  not delete or relocate `notes.txt` — it is somebody's notes.
- **`Store` is in-memory scaffolding.** The seeded conversations, task, steps, changes and
  activity are real data structures the templates render, but they are seeded, and
  `home_thread`/mode changes die with the process. Phase 4 replaces this with the
  persistent store; keep the read paths (`conversations_grouped`, `get_task`) as the
  interface and swap what is behind them.
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
- Do not touch design/orbit_mockup.html or app/templates/ in this phase.
- Commit messages say which rule a change protects when it touches one.
- Branch: work toward main in the same session; do not leave branches dangling.

Finish by reporting: what you ran, what passed, what you did NOT verify, and the exact
command you used. If you changed any file listed in handoff §1, explain why the
property still holds.
```

---

## 5. Phase 4 and 5, when you get there

Not this builder's task, but the constraints are decided now so nobody invents them later:

- **Memory / heartbeat / voice** land on top of `app/store.py`'s existing read paths, and
  every store keeps the `user_id` field. Heartbeat state is local runtime state
  (`runtime/`, ignored). Voice is an input/output mode on the same provider seam, not a
  second brain.
- **Agent mode** reuses the same gate and `max_tool_rounds`, with the ceiling raised from
  the current 5 to a deliberate 6 model calls per turn — one number, in `config.py`,
  asserted by a test. A second provider (stronger Gemini tier for agent turns, a free
  tier as fallback) is additive because `ChatProvider` is the only thing call sites know.
