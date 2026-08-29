"""Watcher for ESPN mock drafts (and any ESPN draft room) via a real browser.

The mock draft lobby is a JavaScript app with no public API, so this drives a
browser instead. Three strategies run at once, most robust first:

  1. websocket frames  - the draft room streams pick events; parsed structurally
  2. XHR responses     - some flows deliver picks over plain HTTP
  3. DOM name matching - scan the pick-history region for known player names

Strategy 3 needs no selector knowledge: it matches against the 400-player
universe we already loaded, so it survives ESPN reskinning their markup.

Run `dfa capture-mock` to record a real session to disk if the parsers ever
need re-calibrating against a markup change.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from ..models import Player, is_real_pick

MOCK_LOBBY_URL = "https://fantasy.espn.com/football/mockdraftlobby"

# Keys that, seen together, strongly suggest a draft-pick payload.
_PICK_KEY_SETS = [
    {"playerId", "overallPickNumber"},
    {"playerId", "roundId"},
    {"playerId", "teamId", "roundPickNumber"},
]

# The draft room talks a plain space-delimited text protocol over
# wss://fantasydraft.espn.com/game-1/league-<id>/JOIN - not JSON. Verified
# against a live practice draft; see docs/draft-protocol.md.
DRAFT_WS_HOST = "fantasydraft.espn.com"


def parse_draft_message(line: str) -> dict | None:
    """Decode one draft-room protocol line.

    Known verbs:
      SELECTED <teamId> <playerId> <lineupSlotId>   a completed pick
      SELECTING <teamId> <millis>                   team is on the clock
      CLOCK <n> <millis>                            countdown tick
      STATE <n>                                     1 once the draft starts
      TOKEN 1:<leagueId>:<teamId>:<swid>:<n>        identifies our own team
      JOINED <teamId> <swid>                        someone joined
      AUTOSUGGEST <playerId>                        ESPN's suggested pick
      AUTODRAFT <teamId> <bool>                     autopick toggled
    """
    if not line:
        return None
    parts = line.strip().split()
    if not parts:
        return None
    verb = parts[0]

    try:
        if verb == "SELECTED" and len(parts) >= 3:
            return {
                "kind": "pick",
                "team_id": int(parts[1]),
                "player_id": int(parts[2]),
                "slot_id": int(parts[3]) if len(parts) > 3 else None,
            }
        if verb == "SELECTING" and len(parts) >= 2:
            return {"kind": "on_clock", "team_id": int(parts[1]),
                    "millis": int(parts[2]) if len(parts) > 2 else None}
        if verb == "CLOCK" and len(parts) >= 3:
            return {"kind": "clock", "millis": int(parts[2])}
        if verb == "STATE" and len(parts) >= 2:
            return {"kind": "state", "value": int(parts[1])}
        if verb == "TOKEN" and len(parts) >= 2:
            bits = parts[1].split(":")
            if len(bits) >= 3:
                return {"kind": "token", "league_id": bits[1], "team_id": int(bits[2])}
        if verb == "JOINED" and len(parts) >= 2:
            return {"kind": "joined", "team_id": int(parts[1])}
        if verb == "AUTOSUGGEST" and len(parts) >= 2:
            return {"kind": "suggest", "player_id": int(parts[1])}
    except (ValueError, IndexError):
        return None
    return None


@dataclass
class ObservedPick:
    player_id: int | None
    player_name: str
    overall: int | None = None
    round: int | None = None
    team_id: int | None = None
    source: str = "unknown"


@dataclass
class NameIndex:
    """Normalised player-name lookup used by the DOM strategy."""

    by_name: dict[str, Player] = field(default_factory=dict)

    @classmethod
    def build(cls, players: Iterable[Player]) -> "NameIndex":
        index: dict[str, Player] = {}
        for p in players:
            index[normalise(p.name)] = p
            # `J. Gibbs` style abbreviations appear in compact pick feeds.
            parts = p.name.split()
            if len(parts) >= 2:
                index.setdefault(normalise(f"{parts[0][0]} {' '.join(parts[1:])}"), p)
        return cls(by_name=index)

    def lookup(self, text: str) -> Player | None:
        return self.by_name.get(normalise(text))

    def find_all(self, blob: str) -> list[Player]:
        """Every known player mentioned in a chunk of page text, in order."""
        found, seen = [], set()
        normalised = normalise(blob)
        for name, player in self.by_name.items():
            if len(name) < 6:
                continue
            if name in normalised and player.espn_id not in seen:
                seen.add(player.espn_id)
                found.append((normalised.index(name), player))
        found.sort(key=lambda pair: pair[0])
        return [p for _, p in found]


def normalise(text: str) -> str:
    text = text.lower().replace(".", "").replace("'", "").replace("-", " ")
    text = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", text)
    return " ".join(text.split())


class MockDraftWatcher:
    """Drives a browser and emits picks it observes in an ESPN draft room."""

    def __init__(
        self,
        players: list[Player],
        on_pick: Callable[[ObservedPick], None],
        headless: bool = False,
        user_data_dir: Path | None = None,
        cdp_url: str | None = None,
        capture_dir: Path | None = None,
        cookies: list[dict] | None = None,
    ):
        self.index = NameIndex.build(players)
        self.by_id = {p.espn_id: p for p in players}
        self.on_pick = on_pick
        self.headless = headless
        self.user_data_dir = user_data_dir
        self.cdp_url = cdp_url
        self.capture_dir = capture_dir
        # Full ESPN session cookies. espn_s2/SWID alone authenticate the API
        # but not the web app, which uses ESPN's OneSite tokens.
        self.cookies = cookies or []
        self._seen: set[tuple] = set()
        self._dom_seen: list[int] = []
        self._pick_count = 0
        # Learned from the draft room itself.
        self.my_team_id: int | None = None
        self.on_clock_team: int | None = None
        self.draft_league_id: str | None = None
        self._playwright = None
        self._browser = None
        self._context = None
        self.page = None

    # -- lifecycle ---------------------------------------------------------

    def start(self, url: str = MOCK_LOBBY_URL):
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        if self.cdp_url:
            # Attach to a Chrome the user already has open and logged in.
            self._browser = self._playwright.chromium.connect_over_cdp(self.cdp_url)
            self._context = (
                self._browser.contexts[0]
                if self._browser.contexts
                else self._browser.new_context()
            )
            self.page = (
                self._context.pages[0]
                if self._context.pages
                else self._context.new_page()
            )
        elif self.user_data_dir:
            # Persistent profile keeps the ESPN login between runs.
            self._context = self._playwright.chromium.launch_persistent_context(
                str(self.user_data_dir), headless=self.headless
            )
            self.page = self._context.pages[0] if self._context.pages else self._context.new_page()
        else:
            self._browser = self._playwright.chromium.launch(headless=self.headless)
            self._context = self._browser.new_context()
            self.page = self._context.new_page()

        if self.cookies and self._context:
            try:
                self._context.add_cookies(self.cookies)
            except Exception:
                pass

        self._wire_listeners()
        if url and not self.page.url.startswith("https://fantasy.espn.com"):
            self.page.goto(url, wait_until="domcontentloaded")
        return self.page

    def start_practice_draft(
        self, league_name: str, draft_slot: int | None = None, timeout: int = 90000
    ):
        """Open a league-specific practice draft against ESPN's auto teams.

        Preferred over a public mock room: it uses your league's real settings
        and nobody else is in the room. Flow is lobby row -> config modal
        (pick draft slot) -> draft room, which may open in this tab or a new one.
        """
        self.page.goto(MOCK_LOBBY_URL, wait_until="domcontentloaded")
        self.page.wait_for_timeout(8000)

        row_button = None
        for row in self.page.query_selector_all("tr"):
            if league_name.lower() in (row.inner_text() or "").lower():
                row_button = row.query_selector("button")
                break
        if row_button is None:
            raise RuntimeError(f"no practice-draft row found for league {league_name!r}")

        row_button.click()
        self.page.wait_for_selector("[role=dialog]", timeout=timeout)
        self.page.wait_for_timeout(2500)

        if draft_slot:
            self._select_draft_slot(draft_slot)

        context = self.page.context
        before = {p for p in context.pages}
        start = self.page.query_selector("button:has-text('Start Practice Draft')")
        if start is None:
            raise RuntimeError("could not find the 'Start Practice Draft' button")
        start.click()

        # The room either replaces this tab or opens a new one.
        deadline = time.time() + timeout / 1000
        while time.time() < deadline:
            self.page.wait_for_timeout(1000)
            fresh = [p for p in context.pages if p not in before]
            if fresh:
                self._adopt_page(fresh[0])
                return self.page
            if "draft" in self.page.url.lower() and "lobby" not in self.page.url.lower():
                self._wire_listeners()
                return self.page
        raise RuntimeError(f"draft room never opened (still at {self.page.url})")

    def _select_draft_slot(self, slot: int) -> None:
        """Choose a draft position in the practice-draft config modal."""
        dialog = self.page.query_selector("[role=dialog]")
        if not dialog:
            return
        for el in dialog.query_selector_all("button, li, label, div, span"):
            try:
                if (el.inner_text() or "").strip() == str(slot):
                    el.click()
                    self.page.wait_for_timeout(600)
                    return
            except Exception:
                continue

    def _adopt_page(self, page):
        """Move listeners onto a draft room that opened in a new tab."""
        self.page = page
        self._wire_listeners()
        page.wait_for_load_state("domcontentloaded")

    def stop(self):
        for closer in (self._context, self._browser, self._playwright):
            try:
                if closer is self._playwright:
                    closer and closer.stop()
                else:
                    closer and closer.close()
            except Exception:
                pass

    # -- strategy 1 + 2: network -------------------------------------------

    def _wire_listeners(self):
        def on_websocket(ws):
            self._capture("websocket-open", ws.url)
            ws.on("framereceived", lambda payload: self._on_frame(payload, ws.url))

        def on_response(response):
            ctype = (response.headers or {}).get("content-type", "")
            if "json" not in ctype:
                return
            if not any(k in response.url for k in ("draft", "league", "pick")):
                return
            try:
                self._scan_payload(response.json(), source="xhr")
            except Exception:
                pass

        self.page.on("websocket", on_websocket)
        self.page.on("response", on_response)

    def _on_frame(self, payload, url: str):
        if isinstance(payload, bytes):
            try:
                payload = payload.decode("utf-8", "ignore")
            except Exception:
                return
        self._capture("websocket-frame", payload)

        # The draft room's own socket speaks the text protocol.
        message = parse_draft_message(payload)
        if message:
            self._handle_draft_message(message)
            return

        try:
            data = json.loads(payload)
        except (ValueError, TypeError):
            return
        self._scan_payload(data, source="websocket")

    def _handle_draft_message(self, message: dict) -> None:
        kind = message["kind"]
        if kind == "pick":
            self._pick_count += 1
            player = self.by_id.get(message["player_id"])
            self._emit(
                ObservedPick(
                    player_id=message["player_id"],
                    player_name=player.name if player else str(message["player_id"]),
                    overall=self._pick_count,
                    team_id=message["team_id"],
                    source="draft-ws",
                )
            )
        elif kind == "token":
            # The room tells us which team we are.
            self.my_team_id = message["team_id"]
            self.draft_league_id = message.get("league_id")
        elif kind == "on_clock":
            self.on_clock_team = message["team_id"]

    def _scan_payload(self, node, source: str, depth: int = 0):
        """Walk arbitrary JSON looking for anything shaped like a pick."""
        if depth > 8:
            return
        if isinstance(node, list):
            for item in node:
                self._scan_payload(item, source, depth + 1)
            return
        if not isinstance(node, dict):
            return

        keys = set(node.keys())
        if any(required <= keys for required in _PICK_KEY_SETS):
            player_id = node.get("playerId")
            if is_real_pick(player_id):
                player = self.by_id.get(player_id)
                self._emit(
                    ObservedPick(
                        player_id=player_id,
                        player_name=player.name if player else str(player_id),
                        overall=node.get("overallPickNumber"),
                        round=node.get("roundId") or node.get("round"),
                        team_id=node.get("teamId"),
                        source=source,
                    )
                )
        for value in node.values():
            self._scan_payload(value, source, depth + 1)

    # -- strategy 3: DOM name matching -------------------------------------

    def poll_dom(self, selector_hints: Iterable[str] = ()) -> None:
        """Opt-in DOM scan. Disabled unless you pass an explicit selector.

        Do not turn this on to track picks. The draft room renders a
        team-by-team *grid* of rosters, not a chronological feed, so scanning
        it yields players in draft-slot order rather than pick order, mixes in
        unpicked cells, and silently corrupts every downstream pick number.
        Two live practice drafts confirmed this. The websocket protocol in
        `parse_draft_message` is exact and complete; use that.

        Kept only for debugging a room whose socket we cannot read.
        """
        if not self.page or not selector_hints:
            return
        text = self._pick_region_text(selector_hints)
        if not text:
            return
        players = self.index.find_all(text)
        ids = [p.espn_id for p in players]
        if ids == self._dom_seen:
            return
        known = set(self._dom_seen)
        for player in players:
            if player.espn_id not in known:
                self._emit(
                    ObservedPick(
                        player_id=player.espn_id,
                        player_name=player.name,
                        source="dom",
                    )
                )
        self._dom_seen = ids

    def _pick_region_text(self, selector_hints: Iterable[str]) -> str:
        """Text of the pick-history container, or empty if we can't find it.

        There is deliberately no whole-page fallback. The draft room renders
        the entire *available* player pool, so scanning the body reports every
        undrafted player as a pick - verified against a live practice draft,
        where it produced 115 phantom picks. No data beats wrong data.
        """
        candidates = list(selector_hints) + [
            "[class*='draft-columns']",
            "[class*='pickHistory']",
            "[class*='draftHistory']",
            "[class*='PickList']",
            "[class*='draft-results']",
        ]
        for selector in candidates:
            try:
                node = self.page.query_selector(selector)
                if node:
                    text = node.inner_text()
                    if text and len(text) > 20:
                        return text
            except Exception:
                continue
        return ""

    # -- plumbing ----------------------------------------------------------

    def _emit(self, pick: ObservedPick):
        key = (pick.player_id, pick.overall)
        if key in self._seen:
            return
        # A pick seen first over the network shouldn't fire again from the DOM.
        if any(k[0] == pick.player_id for k in self._seen):
            return
        self._seen.add(key)
        self.on_pick(pick)

    def _capture(self, kind: str, payload: str):
        if not self.capture_dir:
            return
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        path = self.capture_dir / "capture.log"
        with path.open("a") as fh:
            fh.write(f"--- {kind} {time.time():.3f}\n{payload}\n")

    def dump_page(self, name: str = "page.html"):
        """Save current DOM, for calibrating selectors after a markup change."""
        if not (self.page and self.capture_dir):
            return
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        (self.capture_dir / name).write_text(self.page.content())
