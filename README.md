# ORBIT...BETA

A small, chat-only foundation for ORBIT. The provider seam accepts conversation history and a system prompt and returns plain text. Gemini is the only provider in this first step; there is exactly one tool, `write_note(content: str)`. A reusable confirmation gate and registration-time audit wrapper are introduced with it. Additional tools, agent mode, sub-agents, and UI are intentionally out of scope.

## First Gemini chat

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Set `GEMINI_API_KEY` in `.env` to your own key. `GEMINI_MODEL` defaults to `gemini-2.5-flash` and can be changed there. Run the one-shot chat smoke test with:

```sh
python chat.py
```

When Gemini requests `write_note`, ORBIT displays the exact proposed arguments and waits for the person to type `yes`. Any other answer, unavailable input, or non-interactive stdin declines the call. Approval appends one UTC-timestamped line to local `notes.txt`; each registered tool call is recorded as a JSON line in `audit.log`, including its arguments, confirmation decision, execution status, and duration. Both files are ignored by Git.

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

