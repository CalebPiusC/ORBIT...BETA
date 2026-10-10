# ORBIT...BETA

ORBIT's brain and the two screens it lives in. The provider seam accepts conversation history and a system prompt and returns plain text or a stream. Gemini is the only provider implemented; there is exactly one tool, `write_note(content: str)`, and a reusable confirmation gate plus registration-time audit wrapper exist from tool #1 — nothing runs silently. Agent mode, sub-agents, persistent memory, heartbeat, and voice are still out of scope; see `docs/HANDOFF.md` for what comes next and why.

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

Set `GEMINI_API_KEY` in `.env` to your own key. `GEMINI_MODEL` defaults to `gemini-3.8-flash` (`config.DEFAULT_GEMINI_MODEL`) and can be changed in `.env` — never in a provider file.

`ORBIT_HTTP_TIMEOUT` (default `60`, seconds) is the deadline for **every** request to the model. Leave it set. Without a deadline the SDK asks httpx to wait forever, so a firewalled or black-holed connection hangs with no reply and no error — "it is thinking" and "it is stuck" become the same thing. Raise it on a slow link; never remove it.

## Two ways to talk to Orbit

```sh
python chat.py          # terminal REPL
python -m app.server    # web UI on http://localhost:5000
python run_demo.py      # web UI on the STUB provider — layout only, not your model
```

Both web launchers default to `127.0.0.1:5000` with Flask's debugger disabled.
For a container or sandbox preview that needs an external bind, opt in explicitly
with `ORBIT_HOST=0.0.0.0`; `PORT` can override the port. The debugger remains off.

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
| `[model] FAIL after ~60.0s` | The request never came back — blocked or firewalled | Proxy in the environment. **Not** a key problem |
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
- `GET /task/<task_id>` — the **coding-agent task view** (step checklist,
  proposed-changes card, live activity, side chat).

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
- **GitHub only.** Only GitHub is shown as a connected tool. Linear/Vercel (and
  any other) render as `not-connected` and are never faked as active until they
  are real integrations.

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

