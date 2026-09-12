#!/usr/bin/env bash
# Start GemmaQA local development stack (backend + frontend).
# Ports:
#   Backend API: http://127.0.0.1:8000
#   Frontend UI: http://127.0.0.1:5173
# Default test target: https://thinking-tester-contact-list.herokuapp.com/
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

echo "=== GemmaQA start-dev ==="
echo "Backend  -> http://127.0.0.1:8000"
echo "Frontend -> http://127.0.0.1:5173"
echo "Default target -> https://thinking-tester-contact-list.herokuapp.com/"
echo

if [[ ! -f backend/.env && -f backend/.env.example ]]; then
  cp backend/.env.example backend/.env
  echo "Created backend/.env from example."
fi
if [[ ! -f frontend/.env ]]; then
  cat > frontend/.env <<'EOF'
VITE_API_BASE_URL=
VITE_WS_BASE_URL=
VITE_DEMO_MODE=false
EOF
  echo "Created frontend/.env (Vite proxy mode)."
fi

if [[ ! -x backend/.venv/bin/python ]]; then
  echo "Creating backend virtualenv..."
  python3 -m venv backend/.venv
  backend/.venv/bin/pip install -r backend/requirements.txt
  backend/.venv/bin/playwright install chromium
fi

[[ -d frontend/node_modules ]] || (cd frontend && npm install)

cleanup() {
  echo
  echo "Stopping GemmaQA processes..."
  kill 0 2>/dev/null || true
}
trap cleanup EXIT INT TERM

(cd backend && .venv/bin/python run.py) &
(cd frontend && npm run dev) &

echo
echo "Stack starting. Open http://127.0.0.1:5173"
echo "Default New Run target: https://thinking-tester-contact-list.herokuapp.com/"
wait
