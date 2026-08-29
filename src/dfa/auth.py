"""ESPN sign-in through a real browser, and the session it produces.

Reading cookies out of a Firefox profile worked but assumed a particular
browser was installed and already logged in. Signing in through a browser we
drive ourselves works for anyone, and the persistent profile means it is a
one-time step rather than something to repeat every run.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

# ESPN sets both of these once a fantasy session is established.
REQUIRED_COOKIES = ("espn_s2", "SWID")
LOGIN_URL = "https://www.espn.com/fantasy/football/"
# Where the browser lands once the user is actually signed in.
SIGNED_IN_HINT = "fantasy.espn.com"


@dataclass
class AuthState:
    """Where the sign-in flow currently is, for the launcher to render."""

    status: str = "signed_out"   # signed_out | opening | waiting | signed_in | error
    detail: str = ""
    swid: str | None = None
    espn_s2: str | None = None
    cookies: list[dict] = field(default_factory=list)

    @property
    def signed_in(self) -> bool:
        return bool(self.espn_s2 and self.swid)

    def payload(self) -> dict:
        return {
            "status": self.status,
            "detail": self.detail,
            "signed_in": self.signed_in,
        }


class SessionStore:
    """Persists the ESPN session so sign-in survives a restart."""

    def __init__(self, path: Path, profile_dir: Path):
        self.path = path
        self.profile_dir = profile_dir
        self.state = AuthState()
        self.selected_league: str | None = None
        self._lock = threading.Lock()
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
        except Exception:
            return
        self.state.espn_s2 = data.get("espn_s2")
        self.state.swid = data.get("swid")
        self.state.cookies = data.get("cookies") or []
        self.selected_league = data.get("selected_league")
        if self.state.signed_in:
            self.state.status = "signed_in"
            self.state.detail = "Restored from a previous session."

    def save(self) -> None:
        with self._lock:
            self.path.write_text(json.dumps({
                "espn_s2": self.state.espn_s2,
                "swid": self.state.swid,
                "cookies": self.state.cookies,
                "selected_league": self.selected_league,
            }))
            try:
                self.path.chmod(0o600)  # it holds a live session
            except OSError:
                pass

    def clear(self) -> None:
        self.state = AuthState()
        self.selected_league = None
        self.save()

    def adopt(self, cookies: list[dict]) -> bool:
        """Take a cookie jar from the browser; True if it authenticates us."""
        by_name = {c.get("name"): c.get("value") for c in cookies}
        s2, swid = by_name.get("espn_s2"), by_name.get("SWID")
        if not (s2 and swid):
            return False
        if swid and not swid.startswith("{"):
            swid = "{" + swid.strip("{}") + "}"
        self.state.espn_s2 = s2
        self.state.swid = swid
        self.state.cookies = cookies
        self.state.status = "signed_in"
        self.state.detail = ""
        self.save()
        return True


def sign_in(store: SessionStore, timeout: float = 300.0) -> bool:
    """Open a browser, wait for the user to sign in, capture the session.

    Runs on its own thread (Playwright's sync API needs one), driving a
    persistent profile so a returning user is usually signed in already.
    """
    from playwright.sync_api import sync_playwright

    store.state.status = "opening"
    store.state.detail = "Opening a browser window…"
    store.profile_dir.mkdir(parents=True, exist_ok=True)

    try:
        with sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(
                str(store.profile_dir),
                headless=False,
                viewport={"width": 1180, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)

            store.state.status = "waiting"
            store.state.detail = "Sign in to ESPN in the browser window."

            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    if store.adopt(context.cookies()):
                        store.state.detail = "Signed in."
                        page.wait_for_timeout(1200)
                        context.close()
                        return True
                    page.wait_for_timeout(1500)
                except Exception:
                    break  # window closed by the user
            context.close()
    except Exception as exc:
        store.state.status = "error"
        store.state.detail = f"Sign-in failed: {type(exc).__name__}"
        return False

    if not store.state.signed_in:
        store.state.status = "signed_out"
        store.state.detail = "Sign-in window closed before finishing."
    return store.state.signed_in
