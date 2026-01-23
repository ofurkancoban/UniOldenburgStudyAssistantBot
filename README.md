# 🎓 Uni Oldenburg Study Assistant Bot

Example Telegram bot for **University of Oldenburg** students. Automatically checks **Stud.IP** for new announcements, files, messages, and calendar events, sending instant notifications to your Telegram.

## 🚀 Features

### 📢 Real-time Announcements
Never miss an important update from your lecturers. The bot continuously monitors all your courses and instantly forwards new announcements directly to your chat, ensuring you are always informed about cancellations, exam dates, or general course news.

### 📁 Smart File Manager
Get instant access to new course materials. The bot detects new uploads and updates with a **smart deduplication system** that prevents unnecessary notifications for minor metadata changes.
- **Instant Downloads:** Download files directly through Telegram without logging into the portal.
- **Intelligent Tracking:** Distinguishes between actual file updates and simple system timestamp changes.

### 📅 Personal Scheduling Assistant
Your weekly schedule, always at hand.
- **Proactive Reminders:** Receives automatic notifications 15 minutes before your classes start, including room information.
- **Weekly & Daily Views:** Interactive calendar commands allow you to check your schedule on the go.

### 📨 Unified Messaging
Stay connected with your peers and instructors. Checks your Stud.IP inbox and delivers new messages right to your phone, bridging the gap between the university platform and your daily messaging app.

### 🔄 Robust Session Handling
Built for reliability. The bot handles login sessions, 2FA/TOTP authentication, and auto-reconnection seamlessly, ensuring continuous service without manual intervention.

## 🛠️ Prerequisites

- **Python 3.10+**
- **Google Chrome** (installed on the system for Playwright)
- **Telegram Bot Token** (from [@BotFather](https://t.me/BotFather))

## 📦 Installation

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/yourusername/UniOldenburgCourseManager.git
    cd UniOldenburgCourseManager
    ```

2.  **Create a virtual environment (recommended):**
    ```bash
    python3 -m venv .venv
    source .venv/bin/activate  # Mac/Linux
    # .venv\Scripts\activate   # Windows
    ```

3.  **Install dependencies:**
    ```bash
    pip install -r requirements.txt
    playwright install chromium
    ```

## ⚙️ Configuration

1.  Copy `.env.example` to `.env`:
    ```bash
    cp .env.example .env
    ```

2.  Edit `.env` and fill in your details:
    ```ini
    USERNAME=your_studip_username
    PASSWORD=your_studip_password
    TELEGRAM_TOKEN=your_telegram_bot_token
    ALLOWED_USER_IDS=123456789,987654321  # Your Telegram User ID (comma separated)
    TOTP_SECRET=YOUR_SECRET_KEY           # Optional: For 2FA
    HEADLESS=true                         # Set to false to see the browser
    ```

## ▶️ Usage

Run the bot:
```bash
python studip_bot.py
```

### 🤖 Telegram Commands

| Command | Description |
| :--- | :--- |
| `/start` | Initializes the bot and logs into Stud.IP. |
| `/menu` | Opens the main interactive menu. |
| `/check` | Manually triggers a check for files, announcements, and messages. |
| `/watch` | Starts the automatic background monitoring loop. |
| `/status` | Shows the current status of the bot (last check time, running tasks). |

## 📝 Troubleshooting

- **Browser Errors:** If the bot fails to click buttons, try setting `HEADLESS=false` in `.env` to debug.
- **Login Issues:** Ensure `TOTP_SECRET` is correct if you have 2FA enabled.
- **Dependencies:** Run `playwright install` if you see driver errors.

## ⚠️ Disclaimer

This is an **unofficial** tool and is not affiliated with the University of Oldenburg. Use responsibly and ensure you comply with the university's IT usage policies.
