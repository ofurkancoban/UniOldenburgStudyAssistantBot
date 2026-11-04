"""Entry point of the Uni Oldenburg study assistant bot.

The bot itself lives in the unibot package (see unibot/__init__.py for the
module layout). This file stays the process entry point so existing setups
keep working unchanged: `python studip_bot.py`, PM2 and the self-restart
(which re-executes sys.argv) all start here.
"""

from unibot.app import run

if __name__ == "__main__":
    run()
