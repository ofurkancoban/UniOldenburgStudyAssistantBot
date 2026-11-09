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
