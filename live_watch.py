"""Watch a LIVE ESPN league draft over the draft-room websocket.

ESPN's league API (`mDraftDetail`) does not reflect picks while a draft is in
progress - it returns one placeholder per slot with playerId -1 and only fills
in once the draft finishes. Rosters stay empty too. The only live source is the
draft room's own websocket, which is the same protocol the practice-draft
watcher already parses.

Usage:  .venv/bin/python live_watch.py <leagueId>
"""

from __future__ import annotations

import sys
import threading
import time

import uvicorn

from dfa.config import load_config
from dfa.server.app import create_app
from dfa.models import is_real_pick
from dfa.session import DraftSession
from dfa.sources.firefox_cookies import load_espn_cookies
from dfa.watch.espn_league import EspnLeagueWatcher
from dfa.watch.espn_mock import MockDraftWatcher

DRAFT_ROOM = "https://fantasy.espn.com/football/draft?leagueId={league}&seasonId={season}&teamId={team}"


def main(league_id: str) -> int:
    config = load_config()

    print("Reading league settings…", flush=True)
    league_watcher = EspnLeagueWatcher(
        league_id, config.season, config.espn_s2, config.swid
    )
    snapshot = league_watcher.fetch()
    settings = snapshot.settings
    print(f"  {settings.teams}-team {settings.scoring}, {settings.rounds} rounds, "
          f"slot {settings.my_draft_slot}, team {settings.my_team_id}", flush=True)

    session = DraftSession(config=config)
    session.load_players()
    session.apply_settings(settings)
    session.team_names = dict(snapshot.team_names)
    session.slot_to_team = dict(snapshot.slot_to_team)
    session.status = "watching"
    session.status_detail = f"Draft-room websocket, league {league_id}"

    def on_pick(observed):
        pick = session.record_pick(observed.player_id, team_id=observed.team_id)
        if pick:
            # Learn the real draft order from what the room actually does.
            # ESPN's published pickOrder disagreed with the live order here,
            # which put the wrong team in our slot.
            if pick.round == 1 and pick.team_id:
                session.slot_to_team[pick.pick_in_round] = pick.team_id
                if pick.team_id == session.state.settings.my_team_id:
                    session.state.settings.my_draft_slot = pick.pick_in_round
                    print(f"  >>> your real draft slot is {pick.pick_in_round}",
                          flush=True)
            who = session.team_names.get(pick.team_id, f"team {pick.team_id}")
            mine = "  <<< YOU" if pick.team_id == session.state.settings.my_team_id else ""
            print(f"  {pick.round}.{pick.pick_in_round:02d} #{pick.overall} "
                  f"{pick.player_name} ({pick.pos}) - {who}{mine}", flush=True)

    app = create_app(session)
    threading.Thread(
        target=uvicorn.Server(
            uvicorn.Config(app, host=config.host, port=config.port, log_level="error")
        ).run,
        daemon=True,
    ).start()
    print(f"\n  Dashboard: http://{config.host}:{config.port}\n", flush=True)

    watcher = MockDraftWatcher(
        players=session.players,
        on_pick=on_pick,
        headless=True,          # observe only; don't steal focus from your draft
        cookies=load_espn_cookies(),
        capture_dir=config.cache_dir / "live-capture",
    )
    watcher.start(url=None)
    # The draft room URL needs a memberId (the SWID) as well as league/team;
    # constructing it by hand 404s. Follow ESPN's own "Join Your Draft!" link
    # off the league page instead.
    league_url = f"https://fantasy.espn.com/football/league?leagueId={league_id}&seasonId={config.season}"
    print(f"Opening league page: {league_url}", flush=True)
    watcher.page.goto(league_url, wait_until="domcontentloaded")
    watcher.page.wait_for_timeout(9000)

    href = None
    for anchor in watcher.page.query_selector_all("a"):
        link = anchor.get_attribute("href") or ""
        if "/football/draft?" in link:
            href = link
            break
    if not href:
        print("Could not find the draft-room link. Is the draft actually open?",
              file=sys.stderr, flush=True)
        return 2
    if href.startswith("/"):
        href = "https://fantasy.espn.com" + href

    print(f"Joining draft room…", flush=True)
    watcher.page.goto(href, wait_until="domcontentloaded")
    watcher.page.wait_for_timeout(8000)
    print(f"In room: {watcher.page.url[:80]}", flush=True)
    print("Connected. Watching for picks…\n", flush=True)

    replay_capture(session, config.cache_dir / "live-capture" / "capture.log")

    strikes = 0
    while True:
        if watcher.my_team_id is not None and session.state:
            session.state.settings.my_team_id = watcher.my_team_id
        # SELECTING off the wire beats snake arithmetic for "am I up?"
        session.live_on_clock_team = watcher.on_clock_team
        try:
            watcher.page.wait_for_timeout(1000)
            strikes = 0
        except Exception as exc:
            try:
                closed = watcher.page.is_closed()
            except Exception:
                closed = True
            if closed:
                print("\nDraft tab closed - watcher stopped.", flush=True)
                return 0
            strikes += 1
            if strikes >= 15:
                print(f"Watcher gave up: {exc}", file=sys.stderr, flush=True)
                return 1
            time.sleep(1.0)


def replay_capture(session: DraftSession, path) -> None:
    """Re-apply picks already seen, so a restart doesn't lose the draft.

    Joining a room mid-draft does not replay earlier picks as SELECTED frames
    (they arrive inside an opaque INIT blob), so the capture log is the only
    record we have of anything seen before a restart.
    """
    import re

    if not path.exists():
        return
    replayed = 0
    for line in path.read_text(errors="ignore").splitlines():
        match = re.match(r"^SELECTED (-?\d+) (-?\d+)", line)
        if not match:
            continue
        team_id, player_id = int(match.group(1)), int(match.group(2))
        if not is_real_pick(player_id):
            continue
        pick = session.record_pick(player_id, team_id=team_id)
        if pick:
            replayed += 1
            if pick.round == 1:
                session.slot_to_team[pick.pick_in_round] = team_id
                if team_id == session.state.settings.my_team_id:
                    session.state.settings.my_draft_slot = pick.pick_in_round
    if replayed:
        print(f"Replayed {replayed} pick(s) from the capture log.", flush=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else ""))
