"""unibot: the Uni Oldenburg study assistant Telegram bot.

The package replaces the former single 16,000-line studip_bot.py. Modules are
layered; a module only imports from modules listed *before* it at the top of
the file. The few references to a later module are imported inside the
function that needs them, which keeps the import graph free of cycles
(tests/test_package.py enforces both rules).

Mutable runtime state that several modules rebind (the Stud.IP session, the
login server, watcher flags, ...) lives in unibot.state and is always accessed
as ``state.<name>``, never imported by name, so every module sees the
current value.

Entry point: ``python studip_bot.py`` (calls unibot.app.run()).
"""

# Dependency order, lowest layer first.
MODULE_ORDER = (
    "config",       # environment, constants, logging (side effects: chdir, .env)
    "db",           # SQLite document store (caches and settings)
    "state",        # mutable runtime state
    "diagnostics",  # read-only connection report for /healthcheck
    "storage",      # JSON caches, stored settings, shared HTTP session, job locks
    "formatting",   # text/HTML/date helpers
    "telegram_ui",  # Telegram message helpers
    "auth",         # access control, Stud.IP login, /login Mini App
    "studip_api",   # Stud.IP JSON:API and HTML access
    "ai",           # LLM providers, speech-to-text, text-to-speech
    "documents",    # text extraction from course files
    "weather",
    "mensa",
    "wardrobe",
    "schedule",     # calendar, rooms, study windows
    "whatsapp",
    "monitoring",   # announcement/file/forum/message watchers
    "exams",
    "study_plan",
    "tasks",
    "enrollment",
    "morning",      # morning summary and spoken plans
    "voice",        # voice notes, intents, Q&A
    "system",       # health checks, status, restart
    "menus",        # menus, callback router, wizards
    "watcher",      # background watcher loop
    "app",          # startup and handler registration
)

# config must run before anything else: it anchors the working directory and
# loads .env, which other modules (and studip_oauth) read at import time.
from unibot import config  # noqa: E402

__all__ = ["MODULE_ORDER", "config"]
