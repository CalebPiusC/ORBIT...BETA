#!/bin/sh
# One-command clone setup. The pre-commit secret guard only runs if git is told
# where to find it, which a fresh clone does not know on its own — so this step
# is not optional and it is not something to remember by hand.
set -eu

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

git config core.hooksPath .githooks
echo "pre-commit hook -> .githooks (check_secrets.py runs on every commit)"

if [ ! -f .venv/pyvenv.cfg ]; then
    python3 -m venv .venv
fi
# NB: '.venv/bin/python', not '. .venv/bin/python'. Sourcing the interpreter
# binary as a shell script fails quietly enough that deps never install and the
# suite never runs, which reads as "the tests are broken" rather than
# "setup is broken".
.venv/bin/python -m pip install -q -r requirements.txt
echo "dependencies installed in .venv (activate with: . .venv/bin/activate)"

if [ ! -f .env ]; then
    cp .env.example .env
    echo "created .env from .env.example — add your Gemini key before running chat.py"
fi

.venv/bin/python -m unittest discover -s tests
echo "offline tests passed"
