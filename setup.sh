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
if (( $(echo "$PY_VER < 3.10" | bc -l) )); then
  echo "❌ Python version $PY_VER detected. Please use Python 3.10+."
  exit 1
fi

# ── 2️⃣ Menu Selection ───────────────────────────────────────────────
echo "Which version would you like to set up?"
echo "1) Standard (Browser-less, Recommended) - Fast, low RAM"
echo "2) Legacy (Playwright) - Original version with browser"
read -p "Select [1-2]: " VERSION_CHOICE

# ── 3️⃣ Virtual Environment ──────────────────────────────────────────
if [ ! -d ".venv" ]; then
  echo "📁 Creating virtual environment (.venv)..."
  python3 -m venv .venv
fi

source .venv/bin/activate

# ── 4️⃣ Dependencies ────────────────────────────────────────────────
echo "⬆️  Upgrading pip..."
pip install --upgrade pip setuptools wheel >/dev/null

echo "📦 Installing Python dependencies..."
pip install -r requirements.txt

if [ "$VERSION_CHOICE" == "2" ]; then
  echo "🌐 Installing Playwright & Chromium for Legacy version..."
  pip install playwright
  playwright install chromium
fi

# ── 5️⃣ .env Check ──────────────────────────────────────────────────
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

# ── 6️⃣ Launch ─────────────────────────────────────────────────────
if [ "$VERSION_CHOICE" == "1" ]; then
  echo "🚀 Starting Stud.IP Telegram Bot (Standard)..."
  python studip_bot.py
else
  echo "🚀 Starting Stud.IP Telegram Bot (Legacy)..."
  python studip_bot_playwright.py
fi
