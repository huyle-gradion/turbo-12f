#!/bin/bash
# Smoke: happy path on a disposable repo. No network, no model.
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
T12="$ROOT/bin/t12"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

REPO="$TMP/product"
HOME_DIR="$TMP/state"
mkdir -p "$REPO/tests"
cd "$REPO"
git init -b main -q
git config user.email smoke@example.com
git config user.name Smoke
printf 'def greet(name="world"):\n    return "Hello!"\n' > app.py
touch tests/__init__.py
git add . && git commit -qm init

export PYTHONDONTWRITEBYTECODE=1
"$T12" init --repo "$REPO" --home "$HOME_DIR"
"$T12" prepare --home "$HOME_DIR" --task "$ROOT/examples/task.json"

WT="$HOME_DIR/worktrees/demo-1"
cat > "$WT/tests/test_greet.py" <<'EOF'
import unittest
from app import greet

class TestGreet(unittest.TestCase):
    def test_named(self):
        self.assertEqual(greet("Ana"), "Hello, Ana!")

    def test_default(self):
        self.assertEqual(greet(), "Hello, world!")
EOF

"$T12" red --home "$HOME_DIR" --task-id demo-1 \
  --test-path tests/test_greet.py \
  --command "python3 -m unittest tests.test_greet -q" \
  --marker FAILED --session sess-test-writer

printf 'def greet(name="world"):\n    return f"Hello, {name}!"\n' > "$WT/app.py"
git -C "$WT" add . && git -C "$WT" commit -qm "feat: implement greet"

"$T12" stage --home "$HOME_DIR" --task-id demo-1 --session sess-builder
"$T12" gate --home "$HOME_DIR" --task-id demo-1 --name unit --session sess-qa
"$T12" replay --home "$HOME_DIR" --task-id demo-1

cat > "$TMP/review.md" <<'EOF'
Verdict: approve

## Findings

None. Hypotheses checked against the diff; verification re-run.
EOF
"$T12" review --home "$HOME_DIR" --task-id demo-1 --role reviewer \
  --session sess-reviewer --verdict approve --report "$TMP/review.md"

"$T12" green --home "$HOME_DIR" --task-id demo-1
"$T12" manifest --home "$HOME_DIR" --task-id demo-1

PHASE="$("$T12" status --home "$HOME_DIR" --task-id demo-1 | python3 -c 'import json,sys; print(json.load(sys.stdin)["phase"])')"
test "$PHASE" = "local-verified"
echo "smoke OK: local-verified"
