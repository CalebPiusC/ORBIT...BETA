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
