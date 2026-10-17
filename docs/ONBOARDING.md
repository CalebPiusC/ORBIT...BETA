# Onboarding, accounts, and the open-beta layer

ORBIT serves two distinct façades from one Flask app. A signed-out visitor gets a
product landing page and a sign-in wall; a signed-in user gets the workspace — home,
projects, agents, dashboard, settings. The facade split is decided at startup, not
by fake state: `run_app()` attaches an `AccountStore` to `create_app()` and only then
does the guard exist. **Every test launch and every `create_app()` call that passes no
store runs exactly the single-user app it has always been — no wall, no changes.**

This document is the contract for the layer: what exists, what is deliberately a
demo, and which rules are load-bearing when you touch any of it.

## Running it

```sh
python run_demo.py       # stub provider + accounts wall — the full UI, no keys
python -m app.server     # real provider (GEMINI_API_KEY) + accounts wall
```

Both bind `127.0.0.1:5000` by default with the debugger off. `ORBIT_HOST=0.0.0.0`
opts into an external bind for containers and sandboxes; `PORT` overrides the port.

Environment knobs for this layer:

| Variable            | Default                  | Meaning                                        |
| ------------------- | ------------------------ | ---------------------------------------------- |
| `ORBIT_ACCOUNTS_PATH` | `runtime/accounts.json`  | Where accounts/sessions/feedback persist.      |
| `ORBIT_SECRET_KEY`    | none — **required**      | Flask session secret. `run_app` refuses to start when it is unset or still the committed default `orbit-dev-secret`. |
| `ORBIT_HOST`, `PORT`  | `127.0.0.1`, `5000`      | Bind address / port.                           |

`runtime/` is git-ignored; the store file must never be committed.

## Storage: `app/accounts.py`

One `AccountStore` class, in-memory when `path is None` (tests), file-backed via
`memory.jsonstore.JsonStore` (atomic tmp-file rename, fcntl lock) otherwise. The
document is one dict:

```
{ "version": 1, "accounts": [...], "codes": {...}, "feedback": [...] }
```

Rules that hold everywhere in this file:

- **Passwords are salted PBKDF2-HMAC-SHA256** (`DEFAULT_ITERATIONS = 120_000`), never
  stored in any reversible form, and never returned by any `by_*` reader. OAuth-made
  accounts have no hash at all until the owner sets one in Settings → Security
  (that form correctly skips the current-password field).
- **All codes — email verification and password reset — die after 10 minutes**
  (`CODE_TTL_MINUTES`) and after 6 wrong attempts (`CODE_MAX_ATTEMPTS`) the account
  locks that purpose and the UI says so. Attempt counting lives in the store, not
  in templates.
- **Sessions are rows, not magic.** `start_session`, `session_valid`, `end_session`,
  `revoke_other_sessions`. The Security tab lists them with a human label
  (`session_label` parses the user agent) and marks "This device". Changing a
  password revokes every other session on purpose; "Sign out everywhere else" is
  disabled when there is nothing left to revoke.
- `delete_account` removes the account, its codes, and its feedback in one
  `JsonStore.update` so a torn state is impossible; the gate on the page requires
  typing the account's exact email address.

## The guard and the honesty rules for `next`

A `before_request` hook signs every page request in. The allow-lists are tiny and
declared once (`ACCOUNTS_PUBLIC_ENDPOINTS`, `ACCOUNTS_PENDING_ENDPOINTS`):

- signed out → landing page at `/`, everything else redirects to `/auth/login`;
- signed in but unverified → held at `/auth/verify` until the code checks out;
- verified → the whole workspace.

`?next=` must start with exactly one `/`. `//evil.example` falls back to
`/dashboard` — there is a test pinning this.

## OAuth in a demo build: never lie

There are no real OAuth credentials in a public beta demo, so none are pretended.
`POST /auth/oauth/<provider>` shows a consent/interstitial page that says in plain
words that it is a **demo provider, no request leaves this app**, lets you review or
edit the identity it will hand you, and only completes on an explicit continue.

Two sentences are load-bearing copy (they live in `app/accounts.py` as
`OAUTH_DISCLAIMER` / `OAUTH_SCOPE_COPY` and are pinned by tests — do not soften or
break them):

- the consent page: *"…it **never grants access to any repository**."*
- Settings → Integrations: *"…**does not grant access to any repository**."*

Provider *sign-in* (GitHub/Google/GitLab) is deliberately separate from provider
*integrations* — and there are none. Every row on the Integrations tab reads "not
connected", because no repository integration is built yet; sign-in granting identity
must never be mistaken for repository access. The integrate tab says "visibly disabled"
for what is not connected.

## First-run flow

1. **Signup** collects name + email + password only. Repopulation on error, field-
   level messages, a strength meter that mirrors `password_issues()` — the server
   rule and the client meter must never disagree.
2. **Verify** — an 8-digit form (digits stripped of anything else client- and
   server-side). This demo build sends no email; the page says so in a labelled
   demo chip and shows the code there instead. Resend is rate-limited by the
   store's expiry semantics.
3. **Welcome** (`/welcome`) offers four real destinations — create, import, connect
   a provider, or just explore — plus "Take the 40-second tour". No forced setup.
4. **Personalize** (`/onboarding`, the question wizard) is one page, four questions
   (role, experience, interests, languages). Skipping counts as *done* — the
   checklist never nags about it again — but skips answer nothing and the
   dashboard says so.
5. **Dashboard** (`/dashboard`) is the signed-in home base: greeting, "jump back
   in" strip when there is work in flight, the 4-item checklist
   (verify → personalize → first project → tour) that hides only when everything
   is done or the ✕ is pressed for good, and recommendations derived from the
   wizard answers (`recommendations_for` — interests and roles map to specific
   agents and starters; no invented activity).

### The tour

First dashboard visit sets `data-tour-autostart`; `app/static/onboarding.js` runs
a 6-step spotlight tour (start paths → sidebar → checklist → feedback button →
outro). Every step is skippable, Escape closes, and any end state posts
`POST /api/onboarding/tour` with `state ∈ {done, skipped}` — anything else is a
400. `new → done|skipped` never repeats on its own; Settings → Account → *Replay
the tour* is the only way back. Steps whose targets are not on the current page
are skipped automatically (e.g. the sidebar step on a narrow screen).

### Creating the first project

`POST /projects` accepts `mode=guided` (in-place stepper on `/create`) with an
optional `first_task` — the task is seeded with its `project_id` set and the
redirect is `?guided=1` — or stays the classic describe handoff (`plan=1` → the
Main Agent opens with the brief). Both are real projects; neither fakes progress.

## Settings (`/settings`)

Six tabs — Profile, Preferences, Integrations, Notifications, Privacy & security,
Account. Each section saves independently (303 back with `?saved=<section>&tab=…`).
Rules worth knowing:

- Profile + Preferences write through the store (`update_profile`, personalization
  fields) and are read back next render — nothing is pretend-saved.
- AI-assistance depth preference applies to the shared local workspace on save.
- Notifications toggles persist per account (`beta_updates` among them).
- Security = real password set/change, the sessions list, sign-out-others.
- Account = tour replay + the type-your-email deletion gate.

## Beta feedback

Two surfaces, one pipeline:

- the **floating Feedback button** on every signed-in page — a `<dialog>` that
  submits via `fetch` to `POST /api/feedback`, toasts, and *never navigates away*
  (that is its whole point);
- **`/feedback`** — the same form full-page, plus the account's own submission
  history.

`add_feedback` stores kind (`feature` | `bug` | `general`), title, body, and the
page the user chose to include (client hides the toggle when it can't know one).

## Landing page

`app/templates/landing.html` + `app/static/public.js`. It must:

- say **open beta** plainly and list, in the promise band, what beta actually means
  (rough edges exist, feedback shapes the build);
- describe only what exists today — the chat, projects, six specialists, the
  browser voice, the demo boundaries — never roadmap-as-feature;
- offer three sign-in identities on equal footing (*"Providers appear as visibly
  disabled"* is the copy for what the demo can't wire).

## Testing the layer

```sh
python -m unittest tests.test_onboarding -v    # 46 tests: store + routes
python -m unittest discover -s tests           # the full offline suite
python - <<'PY'                                # Dom behaviors (needs node + jsdom)
PY
```

`tests/test_onboarding.py` splits in two: pure store tests (codes, lockout, expiry,
sessions, OAuth honesty, feedback persistence, deletion) and route tests through
`create_app(provider=FakeProvider(), accounts=store)` against the live templates.
It also pins the negative space: *an app with no store has no wall*, and
`/auth/login` then says accounts are off instead of pretending otherwise.

DOM behaviors (signup validation, code input, wizard, feedback dialog, tour
advance/skip) are verified under jsdom; remember jsdom has **no `showModal`** and
**zero-sized rects**, so the widget has an `open`-attribute fallback and tour test
rects must be stubbed.

## When you change anything here

- Honesty copy is test-pinned. If you rewrite the OAuth sentences or the beta
  promise, update `tests/test_onboarding.py` in the same commit or the suite fails.
- Never invent syncs: a saved tab must be re-rendered from the store, a counting
  state must read from the store, a disabled control must be disabled because
  there is genuinely nothing to do — never for looks.
- `runtime/` content is user data; keep it out of git and out of tests.
