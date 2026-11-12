#!/bin/bash
set -e

echo "──────────────────────────────────────────────"
echo " 📦 Stud.IP Telegram Bot Setup Script (v3)"
echo "──────────────────────────────────────────────"

# ── 1️⃣ Python Check ────────────────────────────────────────────────
if ! command -v python3 &> /dev/null; then
  echo "❌ Python3 not found. Please install Python 3.10 or higher."
  exit 1
fi

PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
  echo "❌ Python version $PY_VER detected. Please use Python 3.10+."
  exit 1
fi

if ! command -v ffmpeg &> /dev/null; then
  echo "⚠️  ffmpeg not found - voice-message task input won't work without it."
  echo "   Install it with: apt install ffmpeg (Linux) or brew install ffmpeg (Mac)."
fi

# ── 2️⃣ Virtual Environment ──────────────────────────────────────────
if [ ! -d ".venv" ]; then
  echo "📁 Creating virtual environment (.venv)..."
  python3 -m venv .venv
fi

source .venv/bin/activate

# ── 3️⃣ Dependencies ────────────────────────────────────────────────
echo "⬆️  Upgrading pip..."
pip install --upgrade pip setuptools wheel >/dev/null

echo "📦 Installing Python dependencies..."
pip install -r requirements.txt

# Baileys and its current transitive packages require Node.js 20.9 or newer.
# Use the committed lockfile so a fresh setup matches the WhatsApp service
# version used by the bot and by scripts/deploy.sh.
if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  echo "❌ Node.js 20.9+ and npm are required for the WhatsApp service. Install Node.js, then rerun setup.sh."
  exit 1
fi
if ! node -e 'const [major, minor] = process.versions.node.split(".").map(Number); process.exit(major > 20 || (major === 20 && minor >= 9) ? 0 : 1)'; then
  echo "❌ Node.js $(node --version) detected. Please install Node.js 20.9 or higher."
  exit 1
fi
echo "📦 Installing WhatsApp service dependencies from its lockfile..."
(cd whatsapp_service && npm ci --omit=dev --no-audit --no-fund)

# ── 4️⃣ .env Check ──────────────────────────────────────────────────
if [ ! -f ".env" ]; then
  echo "⚠️  .env file not found!"
  if [ -f ".env.example" ]; then
      echo "📄 Creating .env from .env.example..."
      cp .env.example .env
      echo "✅ .env created. Please edit it with your credentials:"
      echo "   nano .env"
      exit 1
  else
      echo "❌ .env.example not found!"
      exit 1
  fi
fi

# ── 5️⃣ Launch ─────────────────────────────────────────────────────
echo "🚀 Starting Stud.IP Telegram Bot..."
python studip_bot.py
