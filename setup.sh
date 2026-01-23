#!/bin/bash
set -e

echo "──────────────────────────────────────────────"
echo " 📦 Stud.IP Telegram Bot Setup Script (v2)"
echo "──────────────────────────────────────────────"

# ── 1️⃣ Python kontrolü ───────────────────────────────────────────────
if ! command -v python3 &> /dev/null; then
  echo "❌ Python3 not found. Please install Python 3.10 or higher."
  exit 1
fi

PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
if (( $(echo "$PY_VER < 3.10" | bc -l) )); then
  echo "❌ Python version $PY_VER detected. Please use Python 3.10+."
  exit 1
fi

# ── 2️⃣ Sanal ortam kontrolü ───────────────────────────────────────────
if [ ! -d ".venv" ]; then
  echo "📁 Creating virtual environment (.venv)..."
  python3 -m venv .venv
fi

source .venv/bin/activate

# ── 3️⃣ Pip yükseltme ─────────────────────────────────────────────────
echo "⬆️  Upgrading pip..."
pip install --upgrade pip setuptools wheel >/dev/null

# ── 4️⃣ Gerekli dosyalar ──────────────────────────────────────────────
if [ ! -f "requirements.txt" ]; then
  echo "❌ requirements.txt not found!"
  deactivate
  exit 1
fi

if [ ! -f "studip_bot.py" ]; then
  echo "❌ studip_bot.py not found! Please run this script inside the project root."
  deactivate
  exit 1
fi

# ── 5️⃣ Bağımlılık kurulumu ───────────────────────────────────────────
echo "📦 Installing Python dependencies..."
pip install -r requirements.txt

# ── 6️⃣ Playwright Chromium kurulumu ──────────────────────────────────
echo "🌐 Installing Playwright Chromium..."
playwright install chromium >/dev/null

# ── 7️⃣ .env kontrolü ─────────────────────────────────────────────────
if [ ! -f ".env" ]; then
  echo "⚠️  .env file not found!"
  echo "Please create one with your Stud.IP and Telegram credentials before running."
  deactivate
  exit 1
fi

# ── 8️⃣ Başlatma ──────────────────────────────────────────────────────
echo "🚀 Starting Stud.IP Telegram Bot..."
echo "──────────────────────────────────────────────"
python studip_bot.py