#!/usr/bin/env bash
# Start the local work-only Codex Brain stack.

set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_CODEX=false

usage() {
  cat <<'EOF'
Usage: ./scripts/start.sh [--install-codex]

Starts the local Codex Brain Docker stack. On the first run, it creates the
Python environment and .env using the active Codex gateway configuration.

Options:
  --install-codex  Register the local MCP server and work skill in Codex.
  --help           Show this help message.
EOF
}

case "${1:-}" in
  "")
    ;;
  --install-codex)
    INSTALL_CODEX=true
    ;;
  --help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac

cd "$PROJECT_ROOT"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required to start Codex Brain." >&2
  exit 1
fi

if [[ ! -x .venv/bin/brainctl ]]; then
  echo "Creating the local Python environment..."
  python3 -m venv .venv
  .venv/bin/pip install -e '.[dev]'
fi

if [[ ! -f .env ]]; then
  echo "Creating .env from the active Codex gateway configuration..."
  .venv/bin/brainctl config init --from-codex
  .venv/bin/python - <<'PY'
import secrets
from pathlib import Path

env_path = Path(".env")
placeholder = "NEO4J_PASSWORD=replace-with-a-strong-local-password"
password = f"NEO4J_PASSWORD={secrets.token_urlsafe(32)}"
env_path.write_text(
    env_path.read_text(encoding="utf-8").replace(placeholder, password),
    encoding="utf-8",
)
PY
  echo "Created .env with a generated local Neo4j password."
fi

.venv/bin/python - <<'PY'
from pathlib import Path

env_path = Path(".env")
lines = env_path.read_text(encoding="utf-8").splitlines()
existing = {line.split("=", 1)[0] for line in lines if "=" in line}
lines = [
    "BRAIN_LLM_MODEL=gpt-5.6-luna"
    if line.startswith("BRAIN_LLM_MODEL=gpt-5.5")
    else line
    for line in lines
]
defaults = {
    "BRAIN_REFLECTION_MODEL": "gpt-5.6-luna",
    "BRAIN_REFLECTION_REASONING_EFFORT": "high",
    "BRAIN_CODEX_SESSIONS_HOST_DIR": str(Path.home() / ".codex" / "sessions"),
    "BRAIN_CODEX_SESSIONS_DIR": "/sources/codex",
    "BRAIN_SESSION_CLOSED_AFTER_SECONDS": "1800",
    "BRAIN_REFLECTION_HOUR": "3",
    "BRAIN_TIMEZONE": "America/Argentina/Buenos_Aires",
    "BRAIN_REFLECTION_MAX_NOTES": "50",
}
missing = [f"{key}={value}" for key, value in defaults.items() if key not in existing]
if missing:
    env_path.write_text(
        "\n".join(lines) + "\n" + "\n".join(missing) + "\n",
        encoding="utf-8",
    )
elif lines != env_path.read_text(encoding="utf-8").splitlines():
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required to start Codex Brain." >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  if command -v colima >/dev/null 2>&1; then
    echo "Starting Colima..."
    colima start
  else
    echo "Docker is not running. Start Docker Desktop and run this again." >&2
    exit 1
  fi
fi

echo "Starting Codex Brain..."
make up
make doctor

if [[ "$INSTALL_CODEX" == true ]]; then
  .venv/bin/brainctl config install-codex
  echo "Restart Codex to load the codex-work-brain skill and MCP server."
fi

echo "Codex Brain is ready at http://127.0.0.1:8765/mcp"
