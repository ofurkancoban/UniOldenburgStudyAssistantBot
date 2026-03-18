# 🎓 Uni Oldenburg Study Assistant Bot

<div align="center">

![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white)
![Telegram](https://img.shields.io/badge/Telegram-Bot-2CA5E0?style=for-the-badge&logo=telegram&logoColor=white)
![Automation](https://img.shields.io/badge/Automation-High--Performance-brightgreen?style=for-the-badge)

</div>

A premier Telegram assistant for **University of Oldenburg** students. This bot automates **Stud.IP** monitoring, delivering instant notifications for course updates, files, and messages directly to your pocket.

---

## 🌗 Choose Your Version

During the setup process via `setup.sh`, you can select between two specialized engines:

### 🚀 1. Browser-less (Standard) - `studip_bot.py`
**Recommended** for most users and production servers.
- **Ultra-Lightweight**: Uses direct JSON/HTTP requests. Runs smoothly even on 512MB RAM.
- **High Speed**: Immediate synchronization without the overhead of a headless browser.
- **Modern Features**: Built-in support for the latest Vue-based Stud.IP forum and message systems.

### 🌐 2. Playwright (Legacy) - `studip_bot_playwright.py`
**The original browser-mimicking engine.**
- **Reliability**: Navigates the Stud.IP portal exactly like a human user.
- **Resource Intensive**: Requires Playwright (Chromium) and significantly more CPU/RAM.

---

## ✨ Key Features

### 📅 Smart Scheduling & Reminders
- **Morning Summary (07:00 AM)**: Wake up to a perfect overview of your day. Includes your today's schedule and the current **Mensa Menu**.
- **Lecture Alerts**: Never be late again. Receive a notification with the course name and room location **30 minutes before** every class.
- **Interactive Calendar**: Check your "Today" or "Weekly" views directly within Telegram using simple commands.

### 📢 Institutional Intelligence
- **Real-time Announcements**: Instant forwarding of course updates, cancellations, and exam news.
- **Advanced File Manager**: Detects new uploads and updates. Features **one-click downloads** directly to your Telegram chat.
- **Unified Messaging**: Bridges your Stud.IP inbox to Telegram. Stay on top of direct communications without logging in.
- **Forum Observer**: Tracks new posts in course forums, providing full conversation context and clean text previews.

### 🍽️ Mensa Integration
- Dynamic daily menu updates for the university cafeteria, including pricing, allergen filters, and emoji-coded ingredients.

---

## 🛠️ Installation & Setup

**The easiest way to get started is using the interactive setup script:**

```bash
git clone https://github.com/ofurkancoban/UniOldenburgStudyAssistantBot.git
cd UniOldenburgStudyAssistantBot
chmod +x setup.sh
./setup.sh
```

The script will automatically:
1. Create a Python virtual environment.
2. Install all necessary dependencies (including ICS parsing and Telegram libraries).
3. Let you choose between **Standard** or **Legacy** versions.
4. Help you create your `.env` configuration file.

---

## ⚙️ Configuration (`.env`)

Fill in your credentials in the generated `.env` file:

```ini
USERNAME=your_studip_id
PASSWORD=your_password
TELEGRAM_TOKEN=your_bot_token
ALLOWED_USER_IDS=12345678,87654321
STUDIP_ICAL_URL=https://elearning.uni-oldenburg.de/dispatch.php/ical/index/...
TOTP_SECRET=YOUR_SECRET_KEY  # Required if 2FA is enabled
```
> [!TIP]
> You can find your **STUDIP_ICAL_URL** under **Planner > Export > iCalendar Copy Link** on the Stud.IP portal.

---

## 🤖 Telegram Commands

| Command | Action |
| :--- | :--- |
| `/start` | Initializes the session and opens the main menu. |
| `/menu` | Opens the interactive dashboard (Courses, Calendar, Mensa). |
| `/check` | Triggers a manual synchronization of all watchers. |
| `/status` | Displays system health, last check times, and active configuration. |

---

## 🔒 Security & Performance
- **Private Access**: Restricted via `ALLOWED_USER_IDS` to prevent unauthorized use.
- **Persistent State**: Uses a lightweight JSON cache system to ensure you never receive duplicate notifications.
- **Encryption**: Uses TLS for all communications between your server, Stud.IP, and Telegram.

## ⚠️ Disclaimer
This is an **unofficial** tool and not affiliated with the University of Oldenburg. Use responsibly and ensure compliance with the university's IT policies.

---
**Created for students who value their time.** 🎓✨
