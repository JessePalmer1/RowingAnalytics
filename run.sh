#!/usr/bin/env bash
# Start the race replay UI on macOS or Linux. Windows: use run.cmd.
#
#   ./run.sh              local mode unless .env sets DATABASE_URL
#   PORT=8001 ./run.sh    another port (Concept2 must allow that redirect URI too)
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8000}"

step() { printf '\033[36m==> %s\033[0m\n' "$1"; }

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is not installed. Install it with:"
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 1
fi

if [ ! -f .env ]; then
  echo
  echo "First run: this app needs the Concept2 client ID and secret."
  echo "If someone shared this project with you, they can send you both."
  read -r -p "Concept2 client ID: " client_id
  read -r -p "Concept2 client secret: " client_secret
  if [ -z "$client_id" ] || [ -z "$client_secret" ]; then
    echo "Both values are needed. Run this again when you have them."
    exit 1
  fi
  printf 'C2_CLIENT_ID=%s\nC2_CLIENT_SECRET=%s\n' "$client_id" "$client_secret" > .env
  step "Saved to .env"
fi

if grep -Eq '^[[:space:]]*DATABASE_URL[[:space:]]*=[[:space:]]*[^[:space:]]' .env; then
  step "Starting Postgres (DATABASE_URL is set)"
  docker compose up -d
  until docker compose exec -T db pg_isready -U erg >/dev/null 2>&1; do sleep 2; done
  step "Syncing dependencies"
  uv sync --quiet
  step "Applying database migrations"
  uv run alembic upgrade head
else
  step "Local mode: embedded database, cleared when you close the app"
  step "Syncing dependencies (first run downloads Python 3.12 and packages)"
  uv sync --quiet
fi

url="http://localhost:${PORT}/replay"
( # open the browser once the server answers
  for _ in $(seq 1 90); do
    if curl -fs -o /dev/null "http://127.0.0.1:${PORT}/status"; then
      if command -v open >/dev/null 2>&1; then open "$url"; elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$url"; fi
      printf '\n    Race replay: %s\n    Press Ctrl+C to stop.\n\n' "$url"
      break
    fi
    sleep 1
  done
) &

step "Starting the app on port ${PORT}"
exec uv run uvicorn erg.api:app --host 127.0.0.1 --port "$PORT"
