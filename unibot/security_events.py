"""Privacy-conscious structured security event logging.

Never pass message text, credentials, course identifiers, URLs or provider
responses to this module. User identifiers are only correlated when the
operator configures SECURITY_AUDIT_KEY; the key itself is never logged.
"""

import hashlib
import hmac
import logging
import re
from typing import Optional

from unibot.config import SECURITY_AUDIT_KEY

_EVENT_NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_logger = logging.getLogger("security.audit")


def _subject_tag(user_id: Optional[int], key: str) -> Optional[str]:
    if user_id is None or isinstance(user_id, bool):
        return None
    try:
        raw = str(int(user_id)).encode("ascii")
    except (TypeError, ValueError, OverflowError):
        return None
    return hmac.new(key.encode("utf-8"), raw, hashlib.sha256).hexdigest()[:16]


def log_security_event(
    event: str,
    *,
    actor_id: Optional[int] = None,
    subject_id: Optional[int] = None,
    outcome: str = "success",
) -> None:
    """Log a bounded event label and keyed pseudonyms, never raw user data."""
    if not isinstance(event, str) or not _EVENT_NAME.fullmatch(event):
        raise ValueError("Invalid security event name")
    if outcome not in {"success", "denied", "failed", "revoked"}:
        raise ValueError("Invalid security event outcome")

    fields = [f"event={event}", f"outcome={outcome}"]
    key = SECURITY_AUDIT_KEY
    if len(key.encode("utf-8")) >= 32:
        actor = _subject_tag(actor_id, key)
        subject = _subject_tag(subject_id, key)
        if actor:
            fields.append(f"actor={actor}")
        if subject:
            fields.append(f"subject={subject}")
    _logger.info("%s", " ".join(fields))
