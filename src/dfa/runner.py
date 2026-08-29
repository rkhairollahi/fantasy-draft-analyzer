"""Owns whichever mode is currently running, so one server can host them all.

The CLI used to pick a mode at launch: `dfa watch` or `dfa mock`. The launcher
needs to start and stop modes while the process keeps running, so mode setup
lives here instead.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field

from .config import Config
from .models import LeagueSettings
from .session import DraftSession
from .simulate import simulate_pick
from .watch.espn_league import EspnLeagueWatcher, apply_snapshot, position_lookup

# How long you get on the clock in a practice draft before it picks for you.
DEFAULT_PICK_SECONDS = 45
# Pause between bot picks, so the board is readable rather than instant.
BOT_PICK_DELAY = 1.1


@dataclass
class ModeState:
    mode: str = "idle"          # idle | practice | live | waiting
    detail: str = ""
    league_id: str | None = None
    league_name: str = ""
    # Practice only: when the current pick expires (epoch seconds).
    clock_expires: float | None = None
    pick_seconds: int = DEFAULT_PICK_SECONDS
    autopicked: list[str] = field(default_factory=list)

    def payload(self) -> dict:
        remaining = None
        if self.clock_expires:
            remaining = max(0, round(self.clock_expires - time.time()))
        return {
            "mode": self.mode,
            "detail": self.detail,
            "league_id": self.league_id,
            "league_name": self.league_name,
            "seconds_left": remaining,
            "pick_seconds": self.pick_seconds,
            "autopicked": self.autopicked[-3:],
        }


class ModeRunner:
    """Starts, stops and supervises the active mode."""

    def __init__(self, session: DraftSession, config: Config):
        self.session = session
        self.config = config
        self.state = ModeState()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # Set by the API when the user makes a practice pick.
        self._manual_pick: int | None = None
        self._lock = threading.RLock()

    # -- lifecycle ---------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=6)
        self._thread = None
        self.state = ModeState()
        self.session.status = "idle"
        self.session.status_detail = ""

    def _launch(self, target, *args) -> None:
        """Start a mode thread. Callers set self.state first, so this must not
        call stop() - that would reset the state they just built."""
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._guard, args=(target, *args), daemon=True
        )
        self._thread.start()

    def _guard(self, target, *args) -> None:
        """Run a mode and surface anything it throws.

        A mode thread dying silently looks identical to a stalled draft - it
        cost real debugging time once already.
        """
        try:
            target(*args)
        except Exception as exc:
            self.state.mode = "idle"
            self.state.detail = f"{type(exc).__name__}: {exc}"
            self.session.status = "error"
            self.session.status_detail = f"Mode failed: {type(exc).__name__}: {exc}"

    # -- practice ----------------------------------------------------------

    def start_practice(
        self,
        settings: LeagueSettings,
        slot: int,
        league_name: str = "",
        pick_seconds: int = DEFAULT_PICK_SECONDS,
    ) -> None:
        """A self-contained draft: bots pick, you pick, clock runs.

        Deliberately not ESPN's practice room. Running the draft locally means
        the clock, the slot and the autopick are ours to control, and there is
        no browser to fall over mid-session.
        """
        self.stop()   # tear down any previous mode before adopting new state
        settings = _copy_settings(settings)
        settings.my_draft_slot = slot
        # In a local draft, slot and team id can simply be the same number.
        settings.my_team_id = slot

        self.session.state = None
        self.session.apply_settings(settings)
        self.session.team_names = {
            i: ("You" if i == slot else f"Team {i}")
            for i in range(1, settings.teams + 1)
        }
        self.session.slot_to_team = {i: i for i in range(1, settings.teams + 1)}
        self.session.live_on_clock_team = None
        self.session.status = "watching"
        self.session.status_detail = "Practice draft"

        self.state = ModeState(
            mode="practice", league_name=league_name,
            pick_seconds=pick_seconds,
            detail=f"Practice draft from slot {slot}",
        )
        self._manual_pick = None
        self._launch(self._run_practice, slot, pick_seconds)

    def submit_pick(self, player_id: int) -> bool:
        """Register a manual practice pick; the draft thread applies it."""
        if self.state.mode != "practice":
            return False
        state = self.session.state
        if not state or not self._is_my_turn(state):
            return False
        with self._lock:
            self._manual_pick = player_id
        return True

    def _is_my_turn(self, state) -> bool:
        slot = state.settings.my_draft_slot
        return slot is not None and state.slot_on_the_clock() == slot

    def _run_practice(self, slot: int, pick_seconds: int) -> None:
        rng = random.Random()
        state = self.session.state
        total = state.settings.teams * state.settings.rounds

        while not self._stop.is_set() and len(state.picks) < total:
            if self._is_my_turn(state):
                self._await_my_pick(pick_seconds)
            else:
                self.state.clock_expires = None
                if self._stop.wait(BOT_PICK_DELAY):
                    return
                player_id = simulate_pick(self.session, rng)
                if player_id is None:
                    break
                self.session.record_pick(
                    player_id, team_id=state.slot_on_the_clock()
                )

        self.state.clock_expires = None
        if len(state.picks) >= total:
            state.complete = True
            self.session.status = "complete"
            self.session.status_detail = "Practice draft complete."
            self.state.detail = "Practice draft complete."

    def _await_my_pick(self, pick_seconds: int) -> None:
        """Give the user the clock; pick for them if it runs out."""
        state = self.session.state
        deadline = time.time() + pick_seconds
        self.state.clock_expires = deadline

        while time.time() < deadline and not self._stop.is_set():
            with self._lock:
                chosen = self._manual_pick
                self._manual_pick = None
            if chosen is not None:
                if self.session.record_pick(chosen, team_id=state.settings.my_team_id):
                    self.state.clock_expires = None
                    return
            time.sleep(0.2)

        if self._stop.is_set():
            return

        # Clock expired - take our own top recommendation.
        picks = self.session.recommendations(limit=1)
        if picks:
            player = picks[0].player
            self.session.record_pick(
                player.espn_id, team_id=state.settings.my_team_id
            )
            self.state.autopicked.append(player.name)
            self.state.detail = f"Auto-picked {player.name} - clock expired"
        self.state.clock_expires = None

    # -- live --------------------------------------------------------------

    def start_live(self, league_id: str, league_name: str = "") -> None:
        self.stop()
        self.state = ModeState(
            mode="waiting", league_id=league_id, league_name=league_name,
            detail="Waiting for the draft to open…",
        )
        self._launch(self._run_live, league_id)

    def _run_live(self, league_id: str) -> None:
        """Wait for the draft to open, then follow it over the websocket."""
        watcher = EspnLeagueWatcher(
            league_id, self.config.season,
            self.config.espn_s2, self.config.swid,
        )
        self.session.status = "watching"

        while not self._stop.is_set():
            try:
                snapshot = watcher.fetch(position_lookup(self.session.players))
            except Exception as exc:
                self.session.status_detail = f"ESPN poll failed: {type(exc).__name__}"
                if self._stop.wait(5):
                    return
                continue

            if self.session.state is None:
                self.session.apply_settings(snapshot.settings)
            self.session.team_names = dict(snapshot.team_names)
            self.session.slot_to_team = dict(snapshot.slot_to_team)

            if snapshot.complete:
                apply_snapshot(self.session.state, snapshot)
                self.session.status = "complete"
                self.session.status_detail = "Draft complete."
                self.state.mode = "live"
                self.state.detail = "Draft complete."
                return

            if snapshot.in_progress:
                self.state.mode = "live"
                self.state.detail = "Draft in progress - following the draft room."
                self._follow_draft_room(league_id)
                return

            self.state.mode = "waiting"
            self.state.detail = "Draft hasn't opened yet. Waiting…"
            self.session.status_detail = "Waiting for the draft to open."
            if self._stop.wait(15):
                return

    def _follow_draft_room(self, league_id: str) -> None:
        """Attach to the draft room websocket for the duration of the draft.

        ESPN's league API reports nothing while a draft is in progress - every
        pick reads playerId -1 until it finishes - so the room's own socket is
        the only live source.
        """
        from .watch.espn_mock import MockDraftWatcher

        session = self.session

        def on_pick(observed):
            pick = session.record_pick(observed.player_id, team_id=observed.team_id)
            if pick and pick.round == 1 and pick.team_id:
                session.slot_to_team[pick.pick_in_round] = pick.team_id
                if pick.team_id == session.state.settings.my_team_id:
                    session.state.settings.my_draft_slot = pick.pick_in_round

        watcher = MockDraftWatcher(
            players=session.players,
            on_pick=on_pick,
            headless=True,
            cookies=self.config.browser_cookies or [],
            capture_dir=self.config.cache_dir / "live-capture",
        )
        try:
            watcher.start(url=None)
            href = _find_draft_link(watcher, league_id, self.config.season)
            if not href:
                self.state.detail = (
                    "Could not find the draft room link - is the draft open?"
                )
                return
            watcher.page.goto(href, wait_until="domcontentloaded")
            watcher.page.wait_for_timeout(6000)
            self.state.detail = "Following your draft room."

            strikes = 0
            while not self._stop.is_set():
                if watcher.my_team_id is not None and session.state:
                    session.state.settings.my_team_id = watcher.my_team_id
                session.live_on_clock_team = watcher.on_clock_team
                try:
                    watcher.page.wait_for_timeout(1000)
                    strikes = 0
                except Exception:
                    try:
                        if watcher.page.is_closed():
                            return
                    except Exception:
                        return
                    strikes += 1
                    if strikes >= 15:
                        return
                    time.sleep(1.0)
        finally:
            try:
                watcher.stop()
            except Exception:
                pass


def _find_draft_link(watcher, league_id: str, season: int) -> str | None:
    """ESPN's draft URL needs a memberId, so follow their own link."""
    url = f"https://fantasy.espn.com/football/league?leagueId={league_id}&seasonId={season}"
    watcher.page.goto(url, wait_until="domcontentloaded")
    watcher.page.wait_for_timeout(8000)
    for anchor in watcher.page.query_selector_all("a"):
        href = anchor.get_attribute("href") or ""
        if "/football/draft?" in href:
            return href if href.startswith("http") else "https://fantasy.espn.com" + href
    return None


def _copy_settings(settings: LeagueSettings) -> LeagueSettings:
    return LeagueSettings(
        teams=settings.teams,
        rounds=settings.rounds,
        scoring=settings.scoring,
        starters=dict(settings.starters),
        snake=settings.snake,
    )
