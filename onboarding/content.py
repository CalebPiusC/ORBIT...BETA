"""Every word the onboarding surfaces say, in one place.

Copy lives here rather than in templates for the same reason model ids live in
``config.py``: a claim about what ORBIT does has to be reviewable in one diff.
The rule that governs this file is HANDOFF rule 5 — nothing is described that
the product cannot do today. Where a capability is not built, the entry exists
with ``available=False`` and a plain-language reason, because "not connected"
and "not yet built" are different sentences and the person deserves the right one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

BETA_LABEL = "Private beta"

# --------------------------------------------------------------------------- #
# Marketing: identity, promise, calls to action
# --------------------------------------------------------------------------- #

HERO = {
    "eyebrow": BETA_LABEL,
    "title": "Your ideas, in orbit.",
    "subtitle": (
        "ORBIT is a workspace where you plan, build, and ship with an AI that "
        "shows its reasoning, names the files it touches, and asks before it "
        "changes anything."
    ),
    "primary_cta": "Create your account",
    "secondary_cta": "Explore the demo workspace",
    "assurances": [
        "Free while in beta",
        "No card, no trial countdown",
        "Nothing runs without your approval",
    ],
}

# The product preview on the landing page is the real workspace's own markup and
# palette, rendered from the same design tokens. It is labeled as a preview so
# nobody mistakes a screenshot for a live session.
PREVIEW_LABEL = "A preview of the real workspace"

CAPABILITIES: tuple[dict[str, str], ...] = (
    {
        "tag": "Two ways to work",
        "title": "Chat mode and Agent mode",
        "body": (
            "Chat answers questions and can propose a tool call. Agent mode plans a "
            "checklist from the wording of your request, names the files it works "
            "on, and shows the operation it is running right now."
        ),
    },
    {
        "tag": "Safety you can point at",
        "title": "An approval gate, not a promise",
        "body": (
            "Every tool call and every proposed change is shown to you first. "
            "Nothing runs until you click Approve — and applying changes without "
            "that approval is refused by the data layer, not just hidden by a button."
        ),
    },
    {
        "tag": "Depth on demand",
        "title": "Quick when you want speed, Deep when you want the answer",
        "body": (
            "Quick keeps replies short. Deep spends far more thinking and routes "
            "across a chain of models, naming which one answered — and which one "
            "didn't — as it goes."
        ),
    },
    {
        "tag": "No silent spinners",
        "title": "It says what it is doing",
        "body": (
            "“Thinking…”, “Still thinking…”, “Writing your answer…”. A long wait "
            "says so instead of looking frozen, and a failed turn explains itself "
            "rather than leaving the caret blinking."
        ),
    },
    {
        "tag": "Talk instead of type",
        "title": "Push-to-talk with spoken replies",
        "body": (
            "Hold the mic, speak, release. Audio is transcribed locally on the "
            "machine running ORBIT; voice turns get a spoken reply. Typed turns "
            "stay text."
        ),
    },
    {
        "tag": "You own the record",
        "title": "Analytics read from your own audit log",
        "body": (
            "Messages per day, which model answered, reminders fired, ratings "
            "given — drawn from the append-only log ORBIT already writes. No new "
            "tracking was added to produce it."
        ),
    },
)

HOW_IT_WORKS: tuple[dict[str, str], ...] = (
    {
        "step": "01",
        "title": "Create your account",
        "body": (
            "One click with Google or GitHub, or an email and a password. Email "
            "accounts confirm the address before the workspace opens; Google and "
            "GitHub accounts go straight in."
        ),
    },
    {
        "step": "02",
        "title": "Choose your starting point",
        "body": (
            "Start something new, import work from a repo, a folder, a ZIP or a "
            "file, connect a Git provider, or just explore. Every one of those is "
            "skippable — the workspace is open either way."
        ),
    },
    {
        "step": "03",
        "title": "Work in Chat or Agent mode",
        "body": (
            "Ask, plan, or hand over a task. You approve what runs, you see what "
            "changed, and reminders keep the thread going when you come back."
        ),
    },
)

BETA_SECTION = {
    "title": "What “beta” means here",
    "intro": (
        "ORBIT is in beta because it is changing quickly, not because it is "
        "unfinished. Here is the honest split."
    ),
    "columns": (
        {
            "label": "Settled",
            "tone": "good",
            "points": [
                "The approval gate — nothing executes without your explicit yes",
                "Chat and Agent modes, with live status instead of a dead spinner",
                "Quick and Deep reply depth, and the model chain behind Deep",
                "Reminders with quiet hours, and the audit log every screen reads",
            ],
        },
        {
            "label": "Moving",
            "tone": "note",
            "points": [
                "Model names rotate as providers retire and replace them",
                "The Agent panel's layout is still being tuned against real tasks",
                "Copy and structure of this very site",
            ],
        },
        {
            "label": "Not yet — and it says so",
            "tone": "later",
            "points": [
                "GitLab and Bitbucket import (GitHub is the live integration)",
                "Sub-agents and parallel task runs",
                "Two-factor authentication",
            ],
        },
    ),
    "postscript": (
        "When something is not connected, ORBIT shows it as not connected. "
        "You will never see an integration lit up that does not work."
    ),
    "cta": "Tell us what to build next",
}

FAQ: tuple[dict[str, str], ...] = (
    {
        "q": "Does signing in with GitHub give ORBIT access to my repositories?",
        "a": (
            "No. GitHub sign-in reads your public profile and verified email "
            "address, and that is all it asks for — signing in does not grant "
            "access to your repositories. Repository access is a separate, "
            "optional grant you make from Settings → Integrations, and you can "
            "revoke it without touching your ORBIT account."
        ),
    },
    {
        "q": "Can ORBIT change my code without asking?",
        "a": (
            "No. A tool call or a change is proposed, shown to you in plain words, "
            "and waits. An apply request that arrives without your approval is "
            "refused with a 403 by the store itself."
        ),
    },
    {
        "q": "What happens to what I write here?",
        "a": (
            "Conversations are namespaced to your account and stored in the ORBIT "
            "runtime, not in a third-party analytics product. The audit log records "
            "what ORBIT did — event names, ids, timings — and never the text of "
            "your messages or replies."
        ),
    },
    {
        "q": "Do I need my own model API key?",
        "a": (
            "No. The deployment you are using is configured with its own model "
            "access, and keys stay in the server's environment — there is no field "
            "in ORBIT that asks you to paste one."
        ),
    },
    {
        "q": "What does it cost while it is in beta?",
        "a": (
            "Nothing. There is no card field and no trial timer. When that changes "
            "you will hear about it by email long before a paywall appears."
        ),
    },
    {
        "q": "Can I leave and take my data with me?",
        "a": (
            "Yes. Settings → Privacy exports everything ORBIT holds about you as a "
            "single JSON file, and deleting the account removes it. Both are one "
            "screen and one confirmation away."
        ),
    },
)

FOOTER_COLUMNS: tuple[dict[str, object], ...] = (
    {
        "label": "Product",
        "links": [
            {"label": "What ORBIT does", "href": "#product"},
            {"label": "How it works", "href": "#how"},
            {"label": "Beta notes", "href": "#beta"},
            {"label": "Create an account", "href": "/signup"},
        ],
    },
    {
        "label": "Trust",
        "links": [
            {"label": "Approval gate", "href": "#product"},
            {"label": "Privacy & data", "href": "#faq"},
            {"label": "Questions", "href": "#faq"},
        ],
    },
    {
        "label": "Beta",
        "links": [
            {"label": "Send feedback", "href": "/feedback"},
            {"label": "Sign in", "href": "/login"},
            {"label": "Demo workspace", "href": "/demo"},
        ],
    },
)

# --------------------------------------------------------------------------- #
# Integrations — the honest availability table
# --------------------------------------------------------------------------- #

INTEGRATIONS: tuple[dict[str, str], ...] = (
    {
        "slug": "github",
        "name": "GitHub",
        "state": "available",
        "summary": (
            "Sign in works today. Connecting GitHub for repository access is a "
            "separate, read-only grant you make from Settings → Integrations."
        ),
    },
    {
        "slug": "gitlab",
        "name": "GitLab",
        "state": "planned",
        "summary": (
            "Not available in this beta build. Import a ZIP or a local folder "
            "instead and the project still lives in your workspace."
        ),
    },
    {
        "slug": "bitbucket",
        "name": "Bitbucket",
        "state": "planned",
        "summary": (
            "Not available in this beta build — same route as GitLab: bring the "
            "work in as a ZIP, a folder, or individual files."
        ),
    },
)

# --------------------------------------------------------------------------- #
# Personalization — every question is optional, and says why it is asked
# --------------------------------------------------------------------------- #

BACKGROUNDS: tuple[dict[str, str], ...] = (
    {"value": "student", "label": "Student or learning to code"},
    {"value": "early", "label": "Early career developer"},
    {"value": "professional", "label": "Working developer"},
    {"value": "lead", "label": "Tech lead or manager"},
    {"value": "founder", "label": "Founder or solo builder"},
    {"value": "hobbyist", "label": "Building for fun"},
    {"value": "other", "label": "Something else"},
)

EXPERIENCE_LEVELS: tuple[dict[str, str], ...] = (
    {"value": "new", "label": "New to this", "hint": "Explain the why, not just the fix."},
    {"value": "some", "label": "Getting the hang of it", "hint": "Show the reasoning, skip the basics."},
    {"value": "confident", "label": "Confident", "hint": "Answer directly, assume the fundamentals."},
    {"value": "expert", "label": "Expert", "hint": "Be terse. Argue the trade-offs."},
)

INTERESTS: tuple[dict[str, str], ...] = (
    {"value": "web-apps", "label": "Web apps"},
    {"value": "apis", "label": "APIs & backends"},
    {"value": "mobile", "label": "Mobile apps"},
    {"value": "data", "label": "Data & analytics"},
    {"value": "ai", "label": "AI & agents"},
    {"value": "automation", "label": "Automation & tooling"},
    {"value": "games", "label": "Games"},
    {"value": "infra", "label": "Infra & DevOps"},
    {"value": "design", "label": "Design & frontend"},
    {"value": "learning", "label": "Learning to build"},
)

LANGUAGES: tuple[dict[str, str], ...] = (
    {"value": "python", "label": "Python", "group": "Language"},
    {"value": "javascript", "label": "JavaScript", "group": "Language"},
    {"value": "typescript", "label": "TypeScript", "group": "Language"},
    {"value": "dart", "label": "Dart", "group": "Language"},
    {"value": "go", "label": "Go", "group": "Language"},
    {"value": "rust", "label": "Rust", "group": "Language"},
    {"value": "java", "label": "Java", "group": "Language"},
    {"value": "kotlin", "label": "Kotlin", "group": "Language"},
    {"value": "swift", "label": "Swift", "group": "Language"},
    {"value": "csharp", "label": "C#", "group": "Language"},
    {"value": "php", "label": "PHP", "group": "Language"},
    {"value": "ruby", "label": "Ruby", "group": "Language"},
    {"value": "sql", "label": "SQL", "group": "Language"},
    {"value": "html-css", "label": "HTML & CSS", "group": "Language"},
    {"value": "react", "label": "React", "group": "Framework"},
    {"value": "vue", "label": "Vue", "group": "Framework"},
    {"value": "svelte", "label": "Svelte", "group": "Framework"},
    {"value": "nextjs", "label": "Next.js", "group": "Framework"},
    {"value": "node", "label": "Node.js", "group": "Framework"},
    {"value": "django", "label": "Django", "group": "Framework"},
    {"value": "flask", "label": "Flask", "group": "Framework"},
    {"value": "fastapi", "label": "FastAPI", "group": "Framework"},
    {"value": "flutter", "label": "Flutter", "group": "Framework"},
    {"value": "android", "label": "Android", "group": "Platform"},
    {"value": "ios", "label": "iOS", "group": "Platform"},
    {"value": "docker", "label": "Docker", "group": "Tooling"},
    {"value": "git", "label": "Git & GitHub", "group": "Tooling"},
    {"value": "postgres", "label": "Postgres", "group": "Tooling"},
    {"value": "firebase", "label": "Firebase", "group": "Tooling"},
    {"value": "aws", "label": "AWS", "group": "Tooling"},
    {"value": "testing", "label": "Testing", "group": "Tooling"},
)

PERSONALIZATION_HEADLINE = "Make ORBIT fit how you work"
PERSONALIZATION_PROMISE = (
    "Three short questions, all of them optional. Your answers only order the "
    "suggestions and starter prompts you see — they never gate anything, and you "
    "can change them in Settings whenever you like."
)

# --------------------------------------------------------------------------- #
# First-use guidance: one short tour, plus contextual hints that never nag
# --------------------------------------------------------------------------- #

TOUR_STEPS: tuple[dict[str, str], ...] = (
    {
        "id": "modes",
        "anchor": "modeToggle",
        "title": "Chat and Agent are two ways to work",
        "body": (
            "Chat answers questions, remembers facts, and can propose a tool call "
            "for you to approve. Agent mode takes a task, plans a checklist from "
            "your request, and shows what it is working on."
        ),
    },
    {
        "id": "depth",
        "anchor": "depthToggle",
        "title": "Quick for speed, Deep for the real answer",
        "body": (
            "Depth applies to your next reply. Deep spends far more thinking and "
            "will fall back across a chain of models if one is busy — and it tells "
            "you which model answered."
        ),
    },
    {
        "id": "composer",
        "anchor": "composerDock",
        "title": "Type, speak, or attach",
        "body": (
            "Hold the mic to talk instead of type. The ＋ button lists files for "
            "you locally — ORBIT does not send them to the model yet, and it will "
            "not pretend to have read them."
        ),
    },
    {
        "id": "gate",
        "anchor": "connectedTools",
        "title": "Everything waits for your yes",
        "body": (
            "When ORBIT wants to use a tool or change something, it shows the "
            "exact action and waits. Approve, decline, or ignore it — nothing "
            "happens until you decide."
        ),
    },
    {
        "id": "sidebar",
        "anchor": "sidebar",
        "title": "Your history, reminders, and settings live here",
        "body": (
            "Past conversations, the Analytics view built from ORBIT's own audit "
            "log, the reminders switch, and your account. Collapse the rail with « "
            "when you want the space back."
        ),
    },
)

# Contextual hints: shown once, dismissed for good, never stacked two at a time.
HINTS: tuple[dict[str, str], ...] = (
    {
        "id": "composer-first-message",
        "where": "home",
        "title": "Start with a sentence, not a spec",
        "body": (
            "“Help me add search to my notes API” is enough. ORBIT will ask for "
            "what it is missing rather than guessing."
        ),
        "action_label": "",
        "action_href": "",
    },
    {
        "id": "connect-git",
        "where": "home",
        "title": "Bring your existing work in",
        "body": (
            "GitHub is the live integration today. GitLab and Bitbucket work "
            "arrive later — a ZIP or a folder gets you moving now."
        ),
        "action_label": "Connect or import",
        "action_href": "/projects/new",
    },
    {
        "id": "agent-first-task",
        "where": "task",
        "title": "A task is a request, not a script",
        "body": (
            "Describe the outcome and the constraint — “don't push anything, I "
            "want to review the diff” — and the panel plans from your words."
        ),
        "action_label": "",
        "action_href": "",
    },
)

# --------------------------------------------------------------------------- #
# Project entry points — every route in, with honest availability
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class EntryPoint:
    slug: str
    title: str
    body: str
    href: str
    icon: str
    available: bool = True
    note: str = ""
    group: str = "start"
    badge: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "slug": self.slug,
            "title": self.title,
            "body": self.body,
            "href": self.href,
            "icon": self.icon,
            "available": self.available,
            "note": self.note,
            "group": self.group,
            "badge": self.badge,
        }


PROJECT_ENTRY_POINTS: tuple[EntryPoint, ...] = (
    EntryPoint(
        slug="idea",
        title="Describe an idea",
        body="Tell ORBIT what you want to build. It asks the questions that sharpen it, then proposes a plan.",
        href="/projects/idea",
        icon="✦",
        group="start",
        badge="AI",
    ),
    EntryPoint(
        slug="guided",
        title="Guided setup",
        body="Four questions and a file plan: a project skeleton you can start filling in.",
        href="/projects/guided",
        icon="◎",
        group="start",
    ),
    EntryPoint(
        slug="blank",
        title="Blank project in ORBIT",
        body="Nothing but a name and a place to think. Add files later, or never.",
        href="/projects/blank",
        icon="＋",
        group="start",
    ),
    EntryPoint(
        slug="github",
        title="GitHub",
        body="Connect with a read-only token or OAuth, then pick a repository to work on.",
        href="/projects/import/github",
        icon="◉",
        group="import",
    ),
    EntryPoint(
        slug="gitlab",
        title="GitLab",
        body="Import the archive instead — live GitLab access is not built yet.",
        href="/projects/import/gitlab",
        icon="◇",
        group="import",
        available=False,
        note="Not in this beta",
    ),
    EntryPoint(
        slug="bitbucket",
        title="Bitbucket",
        body="Same story as GitLab: a downloaded archive works today, the integration comes later.",
        href="/projects/import/bitbucket",
        icon="◇",
        group="import",
        available=False,
        note="Not in this beta",
    ),
    EntryPoint(
        slug="folder",
        title="Local folder",
        body="Point at a folder on your machine. Your browser lists the files; nothing is uploaded until you ask.",
        href="/projects/import/folder",
        icon="▤",
        group="import",
    ),
    EntryPoint(
        slug="zip",
        title="ZIP archive",
        body="Upload an archive and ORBIT reads the listing — file names and sizes, not the contents.",
        href="/projects/import/zip",
        icon="❐",
        group="import",
    ),
    EntryPoint(
        slug="files",
        title="Individual files",
        body="A handful of files you want to work on together, listed as a project.",
        href="/projects/import/files",
        icon="❏",
        group="import",
    ),
)

ENTRY_POINTS_BY_SLUG = {point.slug: point for point in PROJECT_ENTRY_POINTS}

PROJECT_KIND_LABELS = {
    "orbit": ("In ORBIT", "orbit"),
    "github": ("GitHub", "github"),
    "gitlab": ("GitLab", "gitlab"),
    "bitbucket": ("Bitbucket", "bitbucket"),
    "folder": ("Local folder", "folder"),
    "zip": ("ZIP archive", "zip"),
    "files": ("Files", "files"),
    "idea": ("From an idea", "idea"),
}

# Guided setup choices.
PROJECT_TEMPLATES: tuple[dict[str, str], ...] = (
    {"value": "web", "label": "Web app", "hint": "A browser interface with a backend."},
    {"value": "api", "label": "API or service", "hint": "Endpoints, jobs, integrations."},
    {"value": "cli", "label": "Command-line tool", "hint": "Something you run in a terminal."},
    {"value": "library", "label": "Library or package", "hint": "Code other projects import."},
    {"value": "data", "label": "Data or analysis", "hint": "Scripts, notebooks, pipelines."},
    {"value": "other", "label": "Something else", "hint": "Describe it and ORBIT adapts."},
)

GUIDED_OPTIONS: tuple[dict[str, str], ...] = (
    {"value": "readme", "label": "A README that explains the project"},
    {"value": "tests", "label": "A test folder and a first failing test"},
    {"value": "env", "label": "An .env.example with the settings it needs"},
    {"value": "ci", "label": "A CI workflow that runs the tests"},
    {"value": "gitignore", "label": "A .gitignore for the language"},
)


# --------------------------------------------------------------------------- #
# Feedback
# --------------------------------------------------------------------------- #

FEEDBACK_TYPES: tuple[dict[str, str], ...] = (
    {
        "value": "suggestion",
        "label": "Feature suggestion",
        "blurb": "Something ORBIT should do that it doesn't.",
        "prompt": "What should it do, and what are you doing today instead?",
    },
    {
        "value": "bug",
        "label": "Bug report",
        "blurb": "Something broke, or behaved in a way that surprised you.",
        "prompt": "What did you expect, and what happened instead?",
    },
    {
        "value": "general",
        "label": "General feedback",
        "blurb": "Anything else — confusion, praise, or a rough edge.",
        "prompt": "What is on your mind?",
    },
)

FEEDBACK_PROMISE = (
    "Feedback is read by the people building ORBIT. Nothing in your workspace "
    "changes when you send it, and the context attached below is exactly what "
    "you can see here — remove anything you would rather not share."
)


# --------------------------------------------------------------------------- #
# Welcome screen — the four starting options
# --------------------------------------------------------------------------- #

WELCOME_OPTIONS: tuple[dict[str, str], ...] = (
    {
        "slug": "create",
        "title": "Create a new project",
        "body": "Start from a blank page, a guided setup, or a description of the idea in your head.",
        "href": "/projects/new",
        "icon": "✦",
        "primary": "1",
    },
    {
        "slug": "import",
        "title": "Import an existing project",
        "body": "From GitHub, a ZIP archive, a folder on your machine, or a few loose files.",
        "href": "/projects/new?group=import",
        "icon": "↧",
        "primary": "0",
    },
    {
        "slug": "git",
        "title": "Connect a Git provider",
        "body": "Optional. GitHub signs you in today; repository access is a separate grant you control.",
        "href": "/settings/integrations",
        "icon": "◉",
        "primary": "0",
    },
    {
        "slug": "explore",
        "title": "Explore ORBIT",
        "body": "Open the workspace, take the 60-second tour, and ask your first question. No setup.",
        "href": "/?tour=1",
        "icon": "◎",
        "primary": "0",
    },
)

GETTING_STARTED_STEPS: tuple[dict[str, str], ...] = (
    {"id": "account", "label": "Create your account", "href": "/settings/profile"},
    {"id": "verify", "label": "Confirm your email address", "href": "/verify"},
    {"id": "workspace", "label": "Meet your workspace", "href": "/?tour=1"},
    {"id": "project", "label": "Start or import a project", "href": "/projects/new"},
    {"id": "first-turn", "label": "Send your first message", "href": "/"},
)

DASHBOARD_RECOMMENDATIONS = {
    "python": "Python is your first language here — the FastAPI or Flask starter reads best.",
    "javascript": "You work in JavaScript — the Node and React starters will feel familiar.",
    "typescript": "TypeScript first — the Next.js starter keeps types visible end to end.",
    "dart": "Dart and Flutter — the guided setup has a mobile-shaped plan.",
    "go": "Go — a service skeleton with a health endpoint is the fastest first win.",
    "rust": "Rust — start with the CLI skeleton and a failing test.",
    "web-apps": "Web apps: the guided setup plans routes, a data layer, and a first screen.",
    "apis": "APIs: describe an endpoint and the Agent panel will plan validation and errors too.",
    "data": "Data work: bring a CSV in and ask ORBIT to describe what it sees.",
    "ai": "AI features: start in Chat mode and ask for the smallest useful version.",
    "automation": "Automation: describe the tedious thing you do weekly and let Agent mode plan it.",
    "learning": "Learning: ask for an explanation at the depth you set in your preferences.",
}

DEFAULT_RECOMMENDATION = (
    "New here? The 60-second tour on the workspace shows where everything is."
)
