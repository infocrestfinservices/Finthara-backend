#!/usr/bin/env bash
#
# Deploy, and every deploy after that. Safe to run repeatedly.
#
#   su - reportcraft
#   bash /opt/reportcraft/backend/deploy/deploy.sh
#
# Pulls both repos, installs what changed, builds the frontend and restarts the API. It does
# NOT touch .env — that is the one file the server owns and git never sees.

set -euo pipefail

APP_DIR=${APP_DIR:-/opt/reportcraft}
BACKEND="$APP_DIR/backend"
FRONTEND="$APP_DIR/frontend"

echo "==> Pulling"
git -C "$BACKEND" pull --ff-only
git -C "$FRONTEND" pull --ff-only

echo "==> Backend dependencies"
if [ ! -d "$BACKEND/venv" ]; then
  python3 -m venv "$BACKEND/venv"
fi
"$BACKEND/venv/bin/pip" install --quiet --upgrade pip
"$BACKEND/venv/bin/pip" install --quiet -r "$BACKEND/requirements.txt"

echo "==> Checking the environment before anything restarts"
# Refuses to go further if a required value is missing. A server that starts with a wildcard
# CORS or a development ENV is worse than one that does not start: the failure is invisible.
cd "$BACKEND"
"$BACKEND/venv/bin/python" - <<'PY'
import sys
sys.path.insert(0, ".")
from config import settings

problems = []
if settings.ENV.strip().lower() != "production":
    problems.append("ENV must be 'production'")
if settings.cors_origins == ["*"]:
    problems.append("CORS_ORIGINS must list the real frontend origin")
if not settings.SECRET_KEY or settings.SECRET_KEY.startswith("reportcraft-"):
    problems.append("SECRET_KEY is still the development phrase — generate a random one")
if not settings.DATABASE_URL:
    problems.append("DATABASE_URL is missing")
if "localhost" in settings.FRONTEND_URL or "127.0.0.1" in settings.FRONTEND_URL:
    problems.append("FRONTEND_URL still points at localhost — password reset links would too")
if not settings.LIBREOFFICE_PATH:
    print("  note: LIBREOFFICE_PATH is unset; the app will look on PATH")

if problems:
    print("\n.env is not ready:")
    for p in problems:
        print("   -", p)
    sys.exit(1)
print("  environment looks right")
PY

echo "==> Building the frontend"
# VITE_ variables are baked in AT BUILD TIME. Six services fall back to 127.0.0.1:8000 when
# VITE_BACKEND_URL is absent, and a build without it ships a frontend that silently talks to
# nothing — so it is read here and the build stops if it is missing.
cd "$FRONTEND"
if [ ! -f .env.production ]; then
  echo "ERROR: $FRONTEND/.env.production is missing (VITE_BACKEND_URL)"
  exit 1
fi
grep -q "VITE_BACKEND_URL=https\?://" .env.production || {
  echo "ERROR: VITE_BACKEND_URL is not set to a URL in .env.production"; exit 1; }
npm ci --silent
npm run build

echo "==> Restarting the API"
sudo systemctl restart reportcraft-api
sleep 3
sudo systemctl --no-pager status reportcraft-api | head -12

echo "==> Smoke test"
curl -fsS -o /dev/null -w "  /payments/config -> %{http_code}\n" http://127.0.0.1:8000/payments/config
echo "Deployed."
