"""Read an ESPN web session out of the local Firefox cookie store.

`espn_s2` + `SWID` are enough for ESPN's JSON API, but the draft room is a web
app behind ESPN's OneSite auth, which needs the full cookie set. Firefox keeps
cookies unencrypted, unlike Chrome on macOS which locks them behind Keychain.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path

FIREFOX_PROFILES = Path.home() / "Library/Application Support/Firefox/Profiles"
# Firefox sameSite ints -> the strings Playwright expects.
_SAME_SITE = {0: "None", 1: "Lax", 2: "Strict"}


def find_cookie_db(profiles_dir: Path | None = None) -> Path | None:
    """Newest profile that actually has a cookie store."""
    profiles_dir = profiles_dir or FIREFOX_PROFILES
    if not profiles_dir.exists():
        return None
    candidates = sorted(
        profiles_dir.glob("*/cookies.sqlite"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def load_espn_cookies(profiles_dir: Path | None = None) -> list[dict]:
    """Return ESPN/Disney cookies in Playwright's format.

    Returns an empty list rather than raising when Firefox isn't present, so
    callers can fall back to an interactive login.
    """
    db = find_cookie_db(profiles_dir)
    if not db:
        return []

    with tempfile.TemporaryDirectory() as tmp:
        # Firefox holds a lock while running; work on a copy, WAL included.
        copy = Path(tmp) / "cookies.sqlite"
        shutil.copy(db, copy)
        for suffix in ("-wal", "-shm"):
            side = db.with_name(db.name + suffix)
            if side.exists():
                shutil.copy(side, copy.with_name(copy.name + suffix))

        try:
            con = sqlite3.connect(str(copy))
            rows = con.execute(
                "SELECT name, value, host, path, isSecure, isHttpOnly, sameSite "
                "FROM moz_cookies WHERE host LIKE '%espn.com%' OR host LIKE '%go.com%'"
            ).fetchall()
            con.close()
        except sqlite3.Error:
            return []

    cookies = []
    for name, value, host, path, secure, http_only, same_site in rows:
        cookie = {
            "name": name,
            "value": value,
            "domain": host,
            "path": path or "/",
            "secure": bool(secure),
            "httpOnly": bool(http_only),
            "sameSite": _SAME_SITE.get(same_site, "Lax"),
        }
        # Playwright rejects SameSite=None on a non-secure cookie.
        if not cookie["secure"] and cookie["sameSite"] == "None":
            cookie["sameSite"] = "Lax"
        cookies.append(cookie)
    return cookies


def espn_api_cookies(profiles_dir: Path | None = None) -> dict[str, str]:
    """Just espn_s2 and SWID, for the plain JSON API."""
    wanted = {"espn_s2", "SWID"}
    return {
        c["name"]: c["value"]
        for c in load_espn_cookies(profiles_dir)
        if c["name"] in wanted
    }
