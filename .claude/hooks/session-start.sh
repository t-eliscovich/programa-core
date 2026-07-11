#!/bin/bash
# SessionStart hook for Programa Core.
#
# Installs the Python dependencies so tests, linters and the app itself work
# inside Claude Code on the web sessions. Runs only in the remote environment;
# local machines are assumed to already have their venv set up via `make setup`
# or `./launcher.sh`.
#
# Idempotent and non-interactive: safe to run on every session start.
set -euo pipefail

# Only run in Claude Code on the web (remote). No-op on local machines.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-.}"

echo "▶ Installing Python dependencies (requirements.txt)…"
python3 -m pip install --quiet -r requirements.txt

# The base image ships a Debian-packaged `cryptography` whose Rust bindings
# need `_cffi_backend` at import time. That backend is not present by default,
# which makes `import cryptography` (pulled in by Authlib for Google OAuth)
# crash with a PyO3 panic and takes down ~130 tests during app factory import.
# Installing cffi into the pip environment satisfies the binding.
echo "▶ Ensuring cffi is available (cryptography/_cffi_backend fix)…"
python3 -m pip install --quiet cffi

# Sanity check: the import that used to panic must now succeed.
python3 - <<'PY'
from cryptography.hazmat.bindings._rust import x509  # noqa: F401
print("✓ cryptography bindings OK")
PY

echo "✓ Programa Core session setup complete."
