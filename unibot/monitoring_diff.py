"""Change detection of the course file watcher: snapshots of a course's file
listing and the diff against the cached one. Pure functions, no I/O."""

from datetime import datetime

from unibot.studip_api import (
    _parse_file_date_v2,
)


def _snapshot_course_files(files: list) -> dict:
    """Files of one course listing keyed by name, with a modified_ts added."""
    current = {}
    for f in files:
        if f.get("type") != "file" or not f.get("name"):
            continue
        dt = _parse_file_date_v2(f.get("modified"), f.get("modified_iso"))
        f["modified_ts"] = None if dt == datetime.min else int(dt.timestamp())
        current[f["name"]] = f
    return current


def _file_was_updated(old: dict, cur: dict) -> bool:
    """Whether a file seen before really changed.

    A size change always counts. With the same size, only a timestamp jump of
    more than 24 hours counts: smaller differences are the day-level date
    resolution of some listings or a re-upload of the same file, and a
    timestamp appearing or disappearing is just a format change.
    """
    old_size = str(old.get("size", "-")).strip()
    cur_size = str(cur.get("size", "-")).strip()
    if old_size != cur_size:
        return True
    old_ts, cur_ts = old.get("modified_ts"), cur.get("modified_ts")
    if old_ts is None or cur_ts is None:
        return False
    return abs(cur_ts - old_ts) > 86400


def _diff_course_files(current: dict, old: dict) -> tuple[list, list]:
    """(new files, updated files) of one course."""
    new_files = [f for name, f in current.items() if name not in old]
    changed = [f for name, f in current.items() if name in old and _file_was_updated(old.get(name, {}), f)]
    return new_files, changed


def _file_events(course_name: str, cid: str, new_files: list, changed_files: list) -> list[dict]:
    events = []
    for kind, flist in (("new", new_files), ("changed", changed_files)):
        for f in flist:
            events.append({
                "type": kind,
                "cid": cid,
                "course": course_name,
                "name": f.get("name"),
                "size": f.get("size", "-"),
                "modified": f.get("modified", "-"),
                "modified_iso": f.get("modified_iso"),
                "dt": _parse_file_date_v2(f.get("modified"), f.get("modified_iso")),
                "url": f.get("url"),
            })
    return events
