"""Change detection of the course file watcher: snapshots of a course's file
listing and the diff against the cached one. Pure functions, no I/O."""

from unibot.studip_api import (
    _parse_file_date_v2,
    file_date_timestamp,
)
import re
from urllib.parse import parse_qs, urlsplit


def _file_identity(file: dict) -> str:
    """Return a stable per-file cache key, even when names are duplicated."""
    file_id = file.get("id")
    if file_id:
        return f"id:{file_id}"

    url = file.get("url")
    if url:
        try:
            parts = urlsplit(url)
            match = re.search(r"/(?:file-refs|file/download)/([^/]+)(?:/content)?$", parts.path)
            query = parse_qs(parts.query)
            resource = match.group(1) if match else None
            if parts.path.endswith("/sendfile.php"):
                resource = next((query[key][0] for key in ("file_id", "id") if query.get(key)), None)
            if resource:
                return f"id:{resource}"
            if parts.path:
                # Stud.IP file links identify the resource in the path. Keep
                # the query too for legacy download routes that put the ID there.
                return f"url:{parts.netloc.lower()}{parts.path}?{parts.query}"
        except (TypeError, ValueError):
            pass

    # Some non-downloadable files have neither an ID nor a URL. A name is the
    # only identity available for those entries.
    return f"name:{file.get('name', '')}"


def _snapshot_course_files(files: list) -> dict:
    """Files keyed by stable resource identity, with a modified_ts added."""
    current = {}
    for f in files:
        if f.get("type") != "file" or not f.get("name"):
            continue
        dt = _parse_file_date_v2(f.get("modified"), f.get("modified_iso"))
        timestamp = file_date_timestamp(dt)
        f["modified_ts"] = None if timestamp is None else int(timestamp)
        current[_file_identity(f)] = f
    return current


def _file_was_updated(old: dict, cur: dict) -> bool:
    """Whether a file seen before really changed.

    Exact ISO timestamps detect same-day updates. Day-only legacy listings
    retain their tolerance when switching between listing formats.
    """
    old_size = str(old.get("size", "-")).strip()
    cur_size = str(cur.get("size", "-")).strip()
    if old_size != cur_size:
        return True
    old_ts, cur_ts = old.get("modified_ts"), cur.get("modified_ts")
    if old_ts is None or cur_ts is None:
        return False
    if old.get("modified_iso") and cur.get("modified_iso"):
        return cur_ts > old_ts
    return abs(cur_ts - old_ts) > 86400


def _diff_course_files(current: dict, old: dict) -> tuple[list, list]:
    """(new files, updated files) of one course."""
    new_files = []
    changed = []
    unmatched_old = set(old)
    for identity, file in current.items():
        old_identity = identity if identity in unmatched_old else None
        if old_identity is None:
            aliases = {_file_identity({"url": file.get("url")})}
            if file.get("id"):
                aliases.add(f"id:{file['id']}")
            matches = [key for key in unmatched_old if isinstance(old.get(key), dict)
                       and not (file.get("id") and old[key].get("id") and file["id"] != old[key]["id"])
                       and (aliases & {_file_identity(old[key]), _file_identity({"url": old[key].get("url")})})
                       and file.get("url") and old[key].get("url")]
            if len(matches) == 1:
                old_identity = matches[0]
        if old_identity is None:
            # Migrate cache entries written by older releases, which used the
            # filename as the key. This also avoids announcing every existing
            # file as new on the first scan after upgrading.
            name = file.get("name")
            old_identity = next((key for key in unmatched_old
                                 if isinstance(old.get(key), dict)
                                 and not key.startswith(("id:", "url:", "name:"))
                                 and old[key].get("name", key) == name), None)
        if old_identity is None:
            new_files.append(file)
            continue
        unmatched_old.remove(old_identity)
        previous = old.get(old_identity)
        if not isinstance(previous, dict) or _file_was_updated(previous, file):
            changed.append(file)
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
