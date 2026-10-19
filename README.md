# ORBIT...BETA

ORBIT's brain and the two screens it lives in. The provider seam accepts conversation history and a system prompt and returns plain text or a stream. Gemini handles Quick mode and the final Deep-mode fallback; Deep can use a configured OpenRouter free-model chain first. There is exactly one tool, `write_note(content: str)`, and a reusable confirmation gate plus registration-time audit wrapper exist from tool #1 — nothing runs silently. Heartbeat reminders (a missing daily plan, a check-in gap, and phone push through ntfy.sh), Agent mode, Quick/Deep response depth, and browser voice are built. Sub-agents remain out of scope; see `docs/HANDOFF.md` for current state and load-bearing rules.

**Read `docs/HANDOFF.md` before changing anything.** It states which rules are load-bearing, which files from the previous build already exist here, and what the next two phases are.

## Setup

One command, in a fresh clone:

```sh
sh setup.sh
```

It installs the dependencies into `.venv`, copies `.env.example` to `.env`, points git at `.githooks` (the secret guard does nothing until it does), and runs the offline suite. To do it by hand instead:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
git config core.hooksPath .githooks
```

Set `GEMINI_API_KEY` in `.env` to your own key. `GEMINI_MODEL` defaults to `gemini-3-flash-preview` (`config.DEFAULT_GEMINI_MODEL`, checked 2026-10-10) and can be changed in `.env` — never in a provider file.

`GEMINI_FALLBACK_MODEL` defaults to `gemini-3.8-flash` (`config.DEFAULT_GEMINI_FALLBACK_MODEL`). It is tried **once**, and only when the primary fails with 503/504 or a network error, and only before any tool has run. Leave it blank in `.env` to disable it. In the production web app and terminal REPL, model events are written to per-account SQLite analytics rows (`analytics_data`); the in-memory test factory retains the file-based audit fallback.

**Optional OpenRouter Deep chain.** Add `OPENROUTER_API_KEY` to `.env` to enable Deep-mode routing. `OPENROUTER_MODEL_1`, `OPENROUTER_MODEL_2`, and `OPENROUTER_MODEL_3` hold the pinned model ids in order; `OPENROUTER_FALLBACK_MODEL` is the final OpenRouter safety net. The example template contains three free-model choices and `openrouter/free`; pinned ids remain environment config so you can replace them as free endpoints change. If OpenRouter is not configured or all its attempts fail before output starts, Deep uses the configured Gemini primary (Europa). Fresh installs copy the updated template; existing `.env` files should add these entries without overwriting existing keys.

**Model names in Agent mode.** Gemini's Quick primary is **Titan**; its regular Gemini fallback is **Europa**. Deep tries the three pinned OpenRouter models as **Io**, **Ganymede**, and **Callisto**, in that order, then the configured OpenRouter auto-router and finally Gemini's primary as **Europa**. Names follow slot order in `providers/personas.py`; Triton and Luna remain reserved. Agent activity names each attempt (for example, `Asking Io…` then `Io didn't answer … Asking Ganymede…`). Chat mode never shows attempt names or status lines.

`ORBIT_HTTP_TIMEOUT` (default `60`, seconds) is the deadline for **every** request to the model. Leave it set. Without a deadline the SDK asks httpx to wait forever, so a firewalled or black-holed connection hangs with no reply and no error — "it is thinking" and "it is stuck" become the same thing. Raise it on a slow link; never remove it.

## Local start commands and persistence

```sh
python -m app.server    # real-provider web app, SQLite (python app/server.py is equivalent)
python run_demo.py      # stub-provider web app, SQLite
python chat.py          # terminal REPL, SQLite
python chat.py --check  # diagnostics only; does not start a chat or create the DB
./setup.sh             # setup + offline tests; not an app-start command
```

All three app launch commands use the same database, selected by `ORBIT_DB_PATH`; if unset, it is `~/.orbit/orbit.sqlite3`. The directory is created with owner-only permissions where supported. The app factory `create_app()` remains in-memory for isolated tests; production launchers call `run_app()`, which installs SQLite-backed stores. `setup.sh` configures dependencies and runs tests—it does not start the app.

`python -m app.server` needs `ORBIT_SECRET_KEY` set in `.env` (it signs the session cookie); it refuses to start without it. `run_demo.py` mints a random key for the run. Both web launchers default to `127.0.0.1:5000` with Flask's debugger disabled. For a container or sandbox preview that needs an external bind, set `ORBIT_HOST=0.0.0.0`; `PORT` can override the port.

`chat.py` is a REPL: it keeps the connection open across turns, carries history,
and a failed turn (bad key, quota, network) prints an error and lets you resend
instead of ending the session. `/help`, `/reset` and `/quit` are the only commands.
History is capped at `chat.MAX_MESSAGES` (20) so a long session cannot grow the
request forever. The loop is covered offline by `tests/test_chat.py` with a fake
provider; the live model call is a manual check.

### `chat.py` is a client, not a server

It opens no port and a browser cannot use it. It reads your keyboard and writes
to your terminal — that is the whole interface. If you want the UI, that is a
different process: `python -m app.server`, then <http://localhost:5000>.
(`run_demo.py` is the stub; a live preview of it is running in someone else's
sandbox, never on your machine.)

### When it seems to do nothing

Every turn prints `waiting on <model> — Ctrl-C cancels this turn` before it
blocks, so a long wait is never silent. Ctrl-C and provider errors end the
**turn**, not the session: your message stays queued and you can resend it.

If you get no reply at all, run the setup check rather than guessing:

```sh
python chat.py --check
```

It walks **config -> network -> model**, names the first layer that fails, and
masks your key to its first four characters in every line. It makes no tool
calls and never raises a traceback. Read the failing line:

| Output | What it means | What to do |
| --- | --- | --- |
| `[config] FAIL` | No usable key in `.env` | `cp .env.example .env` and set `GEMINI_API_KEY` |
| `[network] FAIL` | Cannot open a TCP connection to the API host | Firewall/DNS/offline. If your network needs a proxy, set `HTTPS_PROXY` (httpx reads it) |
| `[model] FAIL` with `503` or `504` (any time, including near 60s) | Google's server is slow or overloaded | Wait a few minutes and retry. The fallback model was already tried. **Not** a key or firewall problem |
| `[model] FAIL after ~60.0s` with no HTTP status | The request never came back — blocked or firewalled | Proxy in the environment. **Not** a key problem |
| `[model] FAIL` mentioning TLS/SSL/EOF | Connection broken before the API answered | Proxy or TLS interception. **Not** a key problem |
| `[model] FAIL` with `401`/`403` | Key rejected or restricted | Check the key is valid and enabled for this API |
| `[model] FAIL` with `404` | `GEMINI_MODEL` is not available to your key | Compare with the current list on ai.google.dev, then update `config.py` **and** `.env.example` together |

A key that is merely *rejected* is not quiet: it fails fast and prints
`ORBIT: [error] …`. Silence means waiting, and waiting means the deadline has
not arrived yet.

The web chat sends over `POST /api/chat` (home) and `POST /api/task/<id>/stream`
(task view) as Server-Sent Events. If a turn produces nothing, the bubble says why
(unreachable endpoint, non-stream response, empty reply) rather than leaving a
blinking caret — a silent stream is indistinguishable from a hung UI. `run_demo.py`
answers with canned text from `StubStreamingProvider`, and every stub reply starts with
`STUB:` — if you can see that prefix in the browser, you are looking at the stub, not
Gemini, and nothing is broken.

When Gemini requests `write_note`, ORBIT displays the exact proposed arguments and waits for the person to type `yes`. Any other answer, unavailable input, or non-interactive stdin declines the call. Approval appends one UTC-timestamped line to local `notes.txt`; each registered tool call is recorded as a JSON line in `audit.log`, including its arguments, confirmation decision, execution status, and duration. Both files live at the repo root and are ignored by Git. That is a known trade-off, not an oversight: your notes are data you want to keep and ideally back up, so Phase 4 should move note storage into the versioned memory store (`runtime/` or a real database) and leave `audit.log` as disposable local state. Until then, nothing should "clean up" `notes.txt`.

## Checks and repository safety

Run the offline provider, tool-flow, and secret-guard tests with:

```sh
python -m unittest discover -s tests -v
```

The model is stubbed in these tests. A live Gemini tool-call decision and a real console confirm/decline still need to be checked by hand with a local `.env` before relying on them.

The pre-commit hook scans staged Git blobs for common credential formats, including Google's `AIza` and `AQ.` key shapes. Enable it in a clone with:

```sh
git config core.hooksPath .githooks
```

`.env`, Python caches, logs, and common generated runtime-state files are ignored. `.env.example` is intentionally tracked and contains only a placeholder.

## Phase 3 — web UI from the mockup

`design/orbit_mockup.html` is the literal structure and palette for two screens.
They are implemented as real routes/templates backed by live data in `app/store.py`
(every placeholder in the mockup is rendered from the store, not hard-coded):

- `GET /` — the chat-first **home** (sidebar history, Chat/Agent mode toggle,
  composer, connected-tools row).
- `GET /task/<task_id>` — the **coding-agent task view** (the Agent panel:
  a checklist planned from the request, files named as it works, the current
  operation, approval state, live activity, side chat).

The mockup's dev-only screen switcher is removed; navigation is real state
(the Chat/Agent mode and the task id).

### Streaming provider

`ChatProvider.reply_stream(...)` is **added alongside** the one-shot `reply()`
(not a replacement). It yields `StreamChunk(kind="thinking"|"answer", text)`
as Gemini generates them (`client.models.generate_content_stream`). The UI
receives these over Server-Sent Events (`/api/chat`, `/api/task/<id>/stream`)
and reveals them progressively — replacing the mockup's timer-based fake.

The **thinking block** is Orbit narrating its reasoning in plain language. In
this phase it is generated as part of the normal response (the model is asked to
wrap a short narration in `<thinking>…</thinking>`, which `reply_stream` splits
out). It is honest model-generated narration, **not** exposed internal reasoning
tokens. A live check should confirm whether to switch to Gemini's native
thinking parts for the configured model.

### Rules carried over from Phase 2 (enforced, not decorative)

- **Approval gate.** A task's proposed changes are never applied/pushed without
  an explicit approval. `POST /api/task/<id>/approve` requires a literal
  `{"approve": true}`; `POST /api/task/<id>/apply` hard-fails with `403` unless
  the task was approved (`Store.apply_changes` raises otherwise). The note
  "No commits will be pushed without your explicit approval" is therefore the
  actual rule.
- **No connection is faked.** GitHub, Linear and Vercel all render as
  `not-connected`, because no integration is built yet. Nothing shows as active
  until it is a real integration — GitHub used to be seeded as `connected` while
  no GitHub code existed anywhere in the app.

### Run it

```sh
python -m pip install -r requirements.txt
# Real model (needs GEMINI_API_KEY in .env):
python -m app.server            # http://localhost:5000
# OR, to exercise the full UI + SSE without a key (stubbed provider):
python run_demo.py
```

Run the offline suite (model stubbed) with:

```sh
python -m unittest discover -s tests -v
```

## Browser voice and response depth

Typed chat remains the default. The microphone button is push-to-talk: hold it to
record with the browser's `MediaRecorder`, then release to upload the clip. The
server transcribes it with `faster-whisper` and submits the resulting text through
the same home-chat or task-chat endpoint as a typed message. Voice-originated turns
also request a spoken reply; typed turns do not. The server streams Edge TTS MP3
audio using `en-US-GuyNeural` at `-15Hz`, and the browser plays it as the chunks
arrive where Media Source Extensions support MP3 (otherwise it buffers the audio
before playback). On the home composer, a small chevron opens a focused voice view
with a larger Orbit mark; it reuses the same mic control and pipeline, shows idle,
listening, thinking, and speaking states, and has a clear Back to text chat control.

The default Whisper model is `base`, running on CPU with `int8`. The first voice
transcription downloads the model into the normal Hugging Face cache; uploaded
recordings are held in a temporary file only while transcribing and are deleted
afterward. Adjust `ORBIT_WHISPER_MODEL`, `ORBIT_WHISPER_DEVICE`, and
`ORBIT_WHISPER_COMPUTE_TYPE` in `.env` if the host has different resources. Browser
microphone access requires HTTPS or localhost. Uploads are capped at 25 MiB and
recordings stop after two minutes.

**ffmpeg is not required.** `faster-whisper` 1.x decodes audio with PyAV, whose
wheels bundle the FFmpeg libraries, so the WebM/Opus clips `MediaRecorder` produces
are read directly — no ffmpeg binary, no conversion step. (The older openai/whisper
subprocess path is the one that needs the program installed.) `pip install -r
requirements.txt` is therefore enough for audio decoding; the error message names
ffmpeg only when a decoder genuinely cannot be found.

**The model itself does have to be fetched once.** Loading a named model resolves it
to a Hugging Face repository (`Systran/faster-whisper-base` for `base`), so the first
recording needs either network access to huggingface.co or a pre-populated cache. A
host with neither fails with the reason spelled out, not with a generic message:
`ORBIT_WHISPER_MODEL` can point at a model directory that is already on disk.

### When voice fails, the cause is logged

The browser status line can only hold one sentence, so the real reason is written
where it can be acted on:

* **Console** — `run_app` configures a console handler on the `orbit.*` loggers
  (level via `ORBIT_LOG_LEVEL`, default `INFO`). A failed transcription logs the
  dependency report (`faster-whisper`/`ctranslate2`/PyAV versions, bundled FFmpeg
  libraries, whether the `ffmpeg` program exists and whether it is needed, and
  whether the model is in the cache) next to the full traceback of the exception
  that caused it. Without that handler, `log.exception` fell through to logging's
  last-resort handler, which prints the bare message and drops INFO lines entirely.
* **`audit.log`** — one JSON line per failure, written by `AuditLog.record_error`:
  `voice_transcription_unavailable`, `voice_transcription_failed`,
  `voice_synthesis_unavailable` or `voice_synthesis_failed`, each with
  `error_type`, `error`, the `error_chain` (so `LocalEntryNotFoundError ←
  ConnectError ← SSLZeroReturnError` survives) and the formatted `traceback`. A
  successful model load is recorded once as `voice_model_loaded`.
* **The response** — the stable one-line `error` is joined by a `reason` field with
  the specific cause, which the browser shows beside it.

Checked on this machine: `faster-whisper 1.2.1` + `PyAV 18.1.0` decode a
MediaRecorder-shaped WebM/Opus clip with **no** system ffmpeg, and the bundled
Silero VAD model means `vad_filter=True` also works offline. What did *not* work
here is the model download — `huggingface.co` is unreachable from the build sandbox
and no cache exists, which is exactly the failure now reported as
`model_cached=False` at boot and `voice_transcription_unavailable` in `audit.log`.

The **Quick / Deep** selector sits beside **Chat / Agent** and applies to the next
streamed UI reply. Quick calls the configured primary Gemini model with a 512-token
output cap. Deep uses a 4096-token cap and tries `OPENROUTER_MODEL_1`, `_2`, and `_3`
in order, then `OPENROUTER_FALLBACK_MODEL`; if OpenRouter is unavailable before any
reply text starts, it falls back to the configured Gemini primary. The selected
model remains in use after the first output chunk to avoid repeating visible text.
Agent activity names the pinned models Io, Ganymede, and Callisto; the final Gemini
fallback is Europa. If OpenRouter is not configured, Deep goes straight to Gemini
as Europa. The depth selection is shared between home and task screens for the life
of the app process.

## Browser tools (Chat mode)

In Chat mode on the home screen, Orbit can ask to use one of six tools: `write_note`,
`remember_fact`, `forget_fact`, `save_plan`, `pause_heartbeat`, `resume_heartbeat`.
The same registry and audit log as the terminal are used, with one difference: the
decision comes from a card, not a console prompt.

- Each reply may ask for **at most one** tool call. Extra calls in the same reply are
  refused and audited as `tool_call_limit`.
- The request is queued and shown as a card with the exact action in plain words. **Nothing
  runs until you click Approve.** Decline records the decision and runs nothing.
- Each card's outcome is audited (`confirmation`: `confirmed` or `declined`, `execution`:
  `succeeded`, `not_run`, or a rejection reason). The card shows the final result, and the
  next turn tells the model how it ended.
- Agent mode stays text-only. Tools are offered only in Chat mode.
- Pending cards are held in memory. Restarting the server clears them, and a card left
  pending is never run.

## Heartbeat reminders

When the web app starts (`python -m app.server` or `python run_demo.py`), a background
thread checks two things, in Africa/Lagos time:

- **Missing plan:** after `HEARTBEAT_PLAN_HOUR` (default 10:00), if no plan is saved for
  today. Save one by asking Orbit to save it in the terminal chat (`python chat.py`, you type
  `yes` to approve) or in Chat mode on the home screen (you click Approve on the card; see
  [Browser tools](#browser-tools-chat-mode)).
- **Check-in gap:** after `HEARTBEAT_CHECKIN_GAP_DAYS` (default 2) with no message on the
  home screen. A home-chat message counts as a check-in.

Each check fires at most once per day. Automatic checks are held during quiet hours
(22:00-08:00). Asking for a check directly skips the quiet-hours hold, but still fires at
most once per day. A notice appears as a card on the home screen; dismiss it with ✕. The
Reminders switch in the sidebar pauses everything. In the terminal chat, the gated
`pause_heartbeat` and `resume_heartbeat` tools do the same, and so do the Chat-mode cards. Every notice fired or dismissed is written to
`audit.log`.

Phone push: set `NTFY_TOPIC` in `.env` to a long random topic, subscribe to it in the ntfy
app, and each notice is also POSTed to `NTFY_SERVER` (default `https://ntfy.sh`). Anyone who
knows the topic can read your notices, so keep it private. Leave it blank to turn push off.

State lives in `runtime/heartbeat.json` (schedule, paused flag, notices) and
`runtime/plans.json` (dated plans). Both are gitignored. Settings are in `.env.example`.

## Current turn context and response controls

Every CLI, home-chat, and task-agent model turn receives the current date and time in
`Africa/Lagos` in its system prompt. Gemini responses that complete without visible text
write safe diagnostics to `audit.log`, including candidate finish reason, function-call
presence without text, and safety-block metadata. No prompt, reply, or function-call
arguments are written. The user-visible `model_returned_nothing` fallback and provider
stream failures are also audited for future diagnosis.

Orbit replies have compact copy, regenerate, and rating controls. Send becomes Stop while a
stream is active. The composer includes an attachment picker. File selections are shown
locally but are not uploaded to the model yet. There is no GitHub shortcut: the old one was
a bare link to github.com that read as an integration the app does not have. The
settings/account controls open a summary of the current local workspace.

### Reply actions

Every Orbit reply carries four small controls, always visible (not hover-only, so they work
on a phone): **copy**, **regenerate**, **thumb up**, **thumb down**. User messages never
have them.

- **Copy** puts that reply's text on the clipboard.
- **Regenerate** works on any Orbit reply, not only the newest one. It re-asks the same
  preceding user turn, rewrites that reply **in place** — same position, same id, later
  turns untouched, no duplicate message — and appends a line to the system prompt asking
  for a genuinely different answer rather than a paraphrase of the last one.
- **Thumbs up/down** are one binary line in `audit.log` per press (`message_rated`, with
  message id, rating, surface and timestamp — never the reply text). Pressing the rating
  already showing is a change of mind and is not logged; switching logs the new one.
  There is no form, no reason picker and no board: it is a private signal.

Stored messages carry the id these actions name (`m-…`), assigned on append and sent to the
client over the stream as the `message` event just before `done`; the row keeps it, so a
rating survives a reload. A reply that somehow has no id says so instead of showing a
button that does nothing.

### Sidebar and Analytics

The sidebar collapses to an icons-only rail (`«` / `»` beside the brand mark): the brand
mark, new conversation, search, a scrollable history of icon rows, Analytics, reminders,
connected apps and settings all stay clickable, each with a tooltip naming it. The choice
is remembered in `localStorage` (`orbit.sidebar.collapsed`) and applied before the first
paint, so a collapsed sidebar never flashes open on load.

**Analytics** (`GET /analytics`, the `◔` sidebar entry) is read from `audit.log` and
nothing else — no new tracking was added for it (`app/analytics.py`):

- messages sent per day over the last seven days, today first, with bars relative to the
  busiest day in the window;
- which model answered, with its assigned persona (Titan, Europa, Io, Ganymede, Callisto);
  older events fall back to configuration lookup and then the raw model id;
- heartbeat notices fired and dismissed, and check-ins logged;
- thumbs up / thumbs down counts.

The framing is descriptive only — "12 messages today; 48 in the last 7 days, mostly
Titan." No streaks, no comparisons, no targets, and no per-day judgement. A missing
`audit.log` reads as "No messages in the last seven days." rather than an error.

## Home screen: welcome, live status, follow-ups

- **Welcome screen** — shown only on a genuinely empty conversation, never on one with
  history. It carries the heading "Your ideas, in orbit.", the subtitle "Think it. Build
  it. Let's work on it together.", and three shortcuts — **Build something**, **Explore an
  idea**, **Review my work** — each of which sends a real prompt (see `app/starters.py`)
  through the ordinary composer path. The orbit rings turn slowly while idle and speed up
  beside the status line the moment Orbit starts responding.
- **Live status while generating** — a turn says what is actually happening: "Thinking…",
  then "Still thinking…" if the wait runs long, then "Writing your answer…" once text is
  arriving (Agent mode says "Working through it…" while it narrates). No status is shown
  that the stream cannot support: attachments are still not uploaded to the model, so no
  label claims to be reading them.
- **Follow-up chips** — after a reply that contains a code block, up to three chips appear
  above the composer: *Explain this code · Find potential bugs · Improve implementation*.
  They come from `app/followups.py`, which only offers a chip for something observable in
  the reply, and the row is hidden — not empty — on every other turn.

## Agent mode: the task panel

`POST /api/tasks` creates a task from a request you type, and `GET /task/<task_id>` shows
it. The panel is generated from the actual request (`app/taskplan.py`):

- **Steps from your request** — the checklist is planned from the request's own wording, so
  a bug report plans "Reproduce the failure" then "Fix the root cause" while a review plans
  "Read the code closely" then "Report what you find". A step only leaves *pending* when
  Orbit's narration gives evidence for it, and the sentence that moved it is shown beneath
  the step, so every tick can be read back against what Orbit said.
- **Files touched** — only files the request named or Orbit mentioned, each labelled with
  which of the two. Nothing guesses.
- **What it's doing now** — the live operation, from the stream ("Thinking…", "Asking
  Titan…", "Writing your answer…", "Waiting for your input").
- **Needs your approval** — the standing rule, plus the task's real approval state. There
  is deliberately no Approve button while nothing has been proposed; the gate is enforced
  in `Store.apply_changes` and `POST /api/task/<id>/apply` (403 without an explicit
  approval), not in a control that only looks like one.

A task that has never run has no panel at all — the request and a start control are the
whole of it. The seeded example task (`T1`) keeps its request text and nothing else: it
ships with no steps, no file diffs and no activity lines, because those used to be seeded
with invented numbers that read as progress the agent had made.

## Accounts, signup, and the open-beta layer

`run_demo.py` (and `python -m app.server`) now attach an `AccountStore`
(`runtime/accounts.json`, override with `ORBIT_ACCOUNTS_PATH`), which turns on the
product's front door. A signed-out visitor lands on a marketing page and a sign-in
wall; everything workspace-side asks for sign-in; the chat, projects and agents
stay exactly as they were once you are inside. **Any `create_app()` call without a
store — every test launch — is the single-user app it has always been: no guard, no
wall, no new chrome.** `ORBIT_SECRET_KEY` signs the session cookie and is
**required**: `run_app` refuses to start when it is unset or still the committed
default (`orbit-dev-secret`), because that value is public in this repo and would let
anyone forge a signed-in session. Make one with
`python -c "import secrets; print(secrets.token_urlsafe(48))"`. `run_demo.py` mints a
random throwaway key per run instead, so the stub launcher still starts.

What the layer does, honestly:

- **Signup/sign-in** — email + password (salted PBKDF2, 10-minute 6-attempt codes,
  per-account sessions you can inspect and revoke in Settings → Security). This
  demo build sends no email, so the verify/reset pages say so in a labelled chip
  and show the code there instead.
- **Provider sign-in is a declared demo.** The OAuth pages pass through a consent
  screen that says no request leaves the app and that sign-in *never grants access
  to any repository*; real repo connections remain a separate, still-unbuilt step.
- **Onboarding** — welcome choices that all lead somewhere real, a four-question
  personalize wizard (skipping counts as done but answers nothing), a dashboard
  checklist that closes itself as you genuinely finish things, and a skippable
  40-second tour recorded server-side so it never nags twice (Settings replays it).
- **Beta feedback** — a floating button on every signed-in page that reports via
  fetch and never navigates away, plus the `/feedback` page with your history.

The full contract, the load-bearing honesty copy, and the test layout live in
`docs/ONBOARDING.md` — read it before touching `app/accounts.py` or any `/auth/*`
route.

