"""One throwaway database directory per test run, removed when the run ends
(every test module used to leave its own directory behind in /tmp)."""

import atexit
import os
import shutil
import tempfile


def use_temp_db() -> None:
    if os.environ.get("BOT_DB_FILE"):
        return
    directory = tempfile.mkdtemp(prefix="unibot-tests-")
    os.environ["BOT_DB_FILE"] = os.path.join(directory, "bot_data.sqlite3")
    atexit.register(shutil.rmtree, directory, True)


_UNSET = object()


def run_handler(handler, update, context=_UNSET, *args, **kwargs):
    """Call a handler with the same user scope as Telegram's authorization gate.

    Authorization and lifecycle remain real or explicitly mocked by the test;
    this helper only supplies the request's identity and deletion generation.
    """
    import asyncio
    from unibot.auth import acting_as

    query = getattr(update, "callback_query", None)
    uid = getattr(getattr(query, "from_user", None), "id", None)
    if not isinstance(uid, int):
        uid = getattr(getattr(update, "effective_user", None), "id", None)
    if not isinstance(uid, int) or isinstance(uid, bool):
        raise ValueError("handler test must provide a real Telegram user id")
    with acting_as(uid):
        return asyncio.run(handler(update, *args, **kwargs) if context is _UNSET
                           else handler(update, context, *args, **kwargs))


def activate_student(uid, courses=None):
    """Explicit fixture for a registered student with active background jobs."""
    from unibot import db
    db.upsert_user(uid, is_active=1)
    db.set_lifecycle(uid, "active", "test fixture")
    if courses is not None:
        db.get_user_scope(uid).sync_courses(courses)


def run_as(uid, awaitable):
    """Run a personal operation in its owner's request/deletion context."""
    import asyncio
    from unibot.auth import acting_as
    with acting_as(uid):
        return asyncio.run(awaitable)
