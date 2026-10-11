"""The Agent panel's plan: steps derived from the actual request.

Two honesty rules shape this module, and both come from docs/HANDOFF.md:

* **Nothing here invents progress.** The checklist is *planned* from the wording
  of the request, so two different requests get two different checklists. A step
  only leaves ``pending`` when Orbit's own narration gives evidence for it, and
  the sentence that moved it is kept as the step's detail line — so every tick
  on the checklist can be read back against something Orbit actually said.
* **Nothing here invents files.** A file appears only because the request named
  it or because Orbit mentioned it, and each entry records which of the two. A
  task where neither happened shows no files at all.

There is no file-system access and no build log behind this. The panel is a
plan plus the evidence the stream produced — never a progress bar dressed up as
one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Step states. A step is ``pending`` until narration moves it, so a run that
# never mentioned a step leaves it visibly untouched.
PENDING = "pending"
ACTIVE = "active"
DONE = "done"

# Run states.
RUNNING = "running"
IDLE = "idle"
FAILED = "failed"

SOURCE_REQUEST = "request"
SOURCE_ORBIT = "orbit"

_KEYWORDS: dict[str, tuple[str, ...]] = {
    "find": (
        "find", "found", "locate", "located", "read", "reading", "scan", "scanned",
        "search", "look", "looking", "open", "opened", "inspect", "existing",
        "structure", "file", "files", "codebase", "repository", "repo", "context",
    ),
    "plan": (
        "plan", "planning", "approach", "design", "decide", "decided", "option",
        "options", "trade-off", "tradeoff", "sketch", "outline", "smallest", "first",
    ),
    "change": (
        "add", "added", "adding", "implement", "implemented", "write", "wrote",
        "writing", "edit", "edited", "create", "created", "change", "changed",
        "apply", "applied", "patch", "wire", "wired", "expose", "exposed", "update",
        "updated", "insert", "replace", "replaced", "define", "hook",
    ),
    "reproduce": (
        "reproduce", "reproducing", "reproduction", "fails", "failing", "failed",
        "error", "errors", "crash", "crashes", "bug", "symptom", "trigger", "broken",
    ),
    "fix": (
        "fix", "fixed", "fixing", "cause", "root", "correct", "corrected", "resolve",
        "resolved", "repair", "workaround", "guard", "handle",
    ),
    "refactor": (
        "refactor", "refactored", "rename", "renamed", "move", "moved", "extract",
        "extracted", "split", "simplify", "simplified", "restructure", "clean",
        "consolidate", "merge",
    ),
    "review": (
        "review", "reviewing", "read", "reading", "inspect", "inspected", "check",
        "checked", "audit", "diff", "compare", "compared", "walk", "trace",
    ),
    "report": (
        "report", "reporting", "finding", "findings", "summary", "summarise",
        "summarize", "list", "conclusion", "verdict", "recommend", "recommendation",
        "note", "notes", "overall",
    ),
    "test": (
        "test", "tests", "testing", "tested", "pytest", "spec", "specs", "assert",
        "coverage", "suite", "unit", "fixture",
    ),
    "verify": (
        "verify", "verified", "verification", "check", "checked", "run", "running",
        "ran", "try", "tried", "confirm", "confirmed", "sanity", "validate",
        "validated", "make sure", "ensure",
    ),
    "document": (
        "document", "documentation", "docs", "readme", "comment", "comments",
        "docstring", "explain", "write-up", "writeup",
    ),
    "summarize": (
        "summary", "summarise", "summarize", "in short", "overall", "ready",
        "next", "next step", "hand back", "review", "final", "wrap", "conclusion",
    ),
}

_IMPLEMENT_WORDS = frozenset(
    "add implement create build introduce wire expose support write make insert enable".split()
)
_UPDATE_WORDS = frozenset("update change modify bump upgrade migrate replace rename".split())
_FIX_WORDS = frozenset("fix bug broken fails fail failing error errors crash crashes regression issue".split())
_REFACTOR_WORDS = frozenset(
    "refactor clean cleanup simplify rename move extract split reorganise reorganize consolidate".split()
)
_REVIEW_WORDS = frozenset("review check audit inspect explain understand describe analyse analyze".split())
_TEST_WORDS = frozenset("test tests testing coverage spec specs".split())
_DOCUMENT_WORDS = frozenset("document documentation docs readme comment docstring".split())

# Words that end the leading clause of a request ("Add a status field *to* each
# order" -> verb "Add", object "a status field").
_CLAUSE_STOP = (
    r"(?:to|into|in|on|onto|for|from|so|that|and|or|with|which|without|but|when|"
    r"if|before|after|while|using|by|as|at|of|about|then|than|also|plus|via)\b"
)
_CLAUSE_RE = re.compile(
    r"^\W*([A-Za-z][\w-]*)\s+(.{3,70}?)\s*(?=[,.!?;:]|\b" + _CLAUSE_STOP + r"|$)"
)

_FILE_RE = re.compile(
    r"(?<![\w./-])[A-Za-z0-9_][\w-]*(?:/[\w.-]+)*\."
    r"(?:py|js|jsx|mjs|cjs|ts|tsx|json|md|markdown|yml|yaml|toml|html|css|scss|"
    r"sh|bash|zsh|rb|go|rs|java|kt|swift|c|cc|cpp|h|hpp|cs|php|sql|ini|cfg|conf|"
    r"lock|toml|txt|csv|env)"
    r"(?![\w])"
)

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")

MAX_STEPS_MIDDLE = 3
MAX_EVIDENCE_CHARS = 150
MAX_FILES = 8


@dataclass
class PlanStep:
    """One checklist line, plus the sentence that moved it (if any)."""

    title: str
    keywords: tuple[str, ...] = ()
    status: str = PENDING
    evidence: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"title": self.title, "status": self.status, "evidence": self.evidence}


@dataclass
class PlanFile:
    """A file the panel can point at, with where the name came from."""

    name: str
    source: str  # "request" | "orbit"

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "source": self.source}


@dataclass
class TaskRun:
    """The live state behind the Agent panel. ``None`` on a task means it has not run."""

    status: str = RUNNING
    operation: str = "Starting…"
    steps: list[PlanStep] = field(default_factory=list)
    files: list[PlanFile] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "operation": self.operation,
            "steps": [step.as_dict() for step in self.steps],
            "files": [item.as_dict() for item in self.files],
        }


def _title(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def leading_clause(request: str) -> tuple[str, str]:
    """``(verb, object)`` from the leading clause of a request.

    "Add a status field to each order" gives ``("Add", "a status field")``. Both
    strings are empty when the request has no clause to read, and callers fall
    back to a generic step title in that case.
    """
    match = _CLAUSE_RE.match(request or "")
    if not match:
        return "", ""
    verb = match.group(1).strip()
    obj = " ".join(match.group(2).split())
    if not verb.isalpha():
        return "", obj
    # "Look at orders.py" would otherwise read "the code for *at orders*". Only a
    # leading preposition is dropped — articles stay, because "Find the code for a
    # status field" reads better than "…for status field".
    obj = obj.strip(" .,;:!?")
    while True:
        trimmed = re.sub(
            r"^(?:at|to|in|on|into|onto|for|from|of|about)\s+",
            "",
            obj,
            flags=re.IGNORECASE,
        )
        if trimmed == obj:
            break
        obj = trimmed.strip(" .,;:!?")
    if len(obj) < 3:
        obj = ""
    return verb, obj


def _intents(text: str) -> set[str]:
    words = set(re.findall(r"[a-z][a-z-]+", (text or "").lower()))
    intents: set[str] = set()
    if words & _IMPLEMENT_WORDS:
        intents.add("implement")
    if words & _UPDATE_WORDS:
        intents.add("update")
    if words & _FIX_WORDS:
        intents.add("fix")
    if words & _REFACTOR_WORDS:
        intents.add("refactor")
    if words & _REVIEW_WORDS:
        intents.add("review")
    if words & _TEST_WORDS:
        intents.add("test")
    if words & _DOCUMENT_WORDS:
        intents.add("document")
    return intents


def plan_steps(request: str) -> list[PlanStep]:
    """A checklist derived from the wording of this request.

    The same four or five lines for every task is the thing this replaces: a bug
    report plans "reproduce" then "fix", a review asks for a report and no
    change, a docs request plans writing, and a request that mentions tests gets
    a test step.
    """
    text = " ".join((request or "").split())
    verb, obj = leading_clause(text)
    intents = _intents(text)

    steps: list[PlanStep] = [
        PlanStep(
            f"Find the code for {obj}" if obj else "Find the code this touches",
            _KEYWORDS["find"],
        )
    ]

    middle: list[PlanStep] = []
    if "fix" in intents:
        middle.append(PlanStep("Reproduce the failure", _KEYWORDS["reproduce"]))
        middle.append(PlanStep("Fix the root cause", _KEYWORDS["fix"]))
    elif "refactor" in intents:
        middle.append(PlanStep("Plan the restructuring", _KEYWORDS["plan"]))
        middle.append(PlanStep("Apply the refactor", _KEYWORDS["refactor"]))
    elif "review" in intents and not intents & {"implement", "update"}:
        middle.append(PlanStep("Read the code closely", _KEYWORDS["review"]))
        middle.append(PlanStep("Report what you find", _KEYWORDS["report"]))
    elif obj and verb:
        # The request's own words: "Plan how to add a status field" / "Add a status field".
        clause = f"{verb} {obj}"
        middle.append(PlanStep(_title(f"Plan how to {clause.lower()}"), _KEYWORDS["plan"]))
        middle.append(PlanStep(_title(clause), _KEYWORDS["change"]))
    else:
        middle.append(PlanStep("Plan the smallest change that works", _KEYWORDS["plan"]))
        middle.append(PlanStep("Make the change", _KEYWORDS["change"]))

    def already_planned(words: tuple[str, ...]) -> bool:
        """True when the request's own clause already produced that step.

        "Add tests for the order lookup" plans "Add tests" from the request, so a
        generic "Add or update the tests" beside it would just be the same line
        twice.
        """
        planned = " ".join(step.title.lower() for step in middle)
        return any(word in planned for word in words)

    if "test" in intents and len(middle) < MAX_STEPS_MIDDLE and not already_planned(("test", "tests")):
        middle.append(PlanStep("Add or update the tests", _KEYWORDS["test"]))
    if (
        "document" in intents
        and len(middle) < MAX_STEPS_MIDDLE
        and not already_planned(("documentation", "docs", "document", "readme", "comment"))
    ):
        middle.append(PlanStep("Write the documentation", _KEYWORDS["document"]))
    if (
        intents & {"implement", "update", "fix", "refactor"}
        and "test" not in intents
        and len(middle) < MAX_STEPS_MIDDLE
    ):
        middle.append(PlanStep("Verify the change", _KEYWORDS["verify"]))

    steps.extend(middle[:MAX_STEPS_MIDDLE])
    steps.append(PlanStep("Summarize it for your review", _KEYWORDS["summarize"]))
    return steps


def files_in(text: str, limit: int = MAX_FILES) -> list[str]:
    """Filenames and paths named in ``text``, in order, de-duplicated."""
    found: list[str] = []
    for match in _FILE_RE.finditer(text or ""):
        name = match.group(0).strip("./")
        if name and name not in found:
            found.append(name)
        if len(found) >= limit:
            break
    return found


def _sentences(text: str) -> list[str]:
    return [
        " ".join(part.split())
        for part in _SENTENCE_RE.split(text or "")
        if part and len(part.strip()) >= 12
    ]


def _trim(sentence: str) -> str:
    sentence = " ".join(sentence.split())
    if len(sentence) <= MAX_EVIDENCE_CHARS:
        return sentence
    return sentence[: MAX_EVIDENCE_CHARS - 1].rstrip() + "…"


def _mentions(step: PlanStep, sentence: str) -> bool:
    lowered = sentence.lower()
    for keyword in step.keywords:
        if re.search(r"(?<![\w])" + re.escape(keyword) + r"(?![\w])", lowered):
            return True
    return False


def apply_narration(steps: list[PlanStep], narration: str) -> bool:
    """Move steps on Orbit's own words. Returns True when anything changed.

    The first pending step a sentence mentions becomes ``active`` and keeps that
    sentence as its evidence; any step it moves past is marked ``done``. A step
    Orbit never mentioned stays ``pending`` — the checklist never claims work
    that has no line behind it.
    """
    changed = False
    for sentence in _sentences(narration):
        for index, step in enumerate(steps):
            if step.status != PENDING or not _mentions(step, sentence):
                continue
            for earlier in steps[:index]:
                if earlier.status == ACTIVE:
                    earlier.status = DONE
                    changed = True
            step.status = ACTIVE
            step.evidence = _trim(sentence)
            changed = True
            break
    return changed


def finish_run(steps: list[PlanStep]) -> None:
    """A turn that produced an answer finished the step it was narrating."""
    for step in steps:
        if step.status == ACTIVE:
            step.status = DONE


def add_files(run: TaskRun, names: list[str], source: str) -> bool:
    """Record newly named files. Returns True when the list changed."""
    changed = False
    for name in names:
        if len(run.files) >= MAX_FILES:
            break
        if any(item.name == name for item in run.files):
            continue
        run.files.append(PlanFile(name=name, source=source))
        changed = True
    return changed


def new_run(request: str) -> TaskRun:
    """The opening panel state for a task: a plan, and files the request named."""
    files = [PlanFile(name=name, source=SOURCE_REQUEST) for name in files_in(request)]
    return TaskRun(status=RUNNING, operation="Starting…", steps=plan_steps(request), files=files)


def summarize_request(request: str, limit: int = 70) -> str:
    """A task title from the request itself — no model call needed for this."""
    text = " ".join((request or "").split())
    if not text:
        return "Untitled task"
    head = re.split(r"(?<=[.!?])\s|\n", text, maxsplit=1)[0].strip()
    if len(head) > limit:
        head = head[:limit].rstrip() + "…"
    return head or "Untitled task"
