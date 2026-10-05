#!/bin/bash
# AI Grader one-command startup script
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "Creating virtual environment…"
  python3 -m venv .venv
fi
source .venv/bin/activate
echo "Installing dependencies…"
pip install -q -r requirements.txt

if [ ! -f ".env" ]; then
  echo ""
  echo "⚠️  No .env file detected."
  echo "   Please enter your OpenAI API key on the Settings page after startup,"
  echo "   or copy .env.example to .env and fill it in:"
  echo "   cp .env.example .env"
  echo ""
fi

echo "Starting server: http://localhost:5000"
exec flask --app app run --host=0.0.0.0 --port=5000
