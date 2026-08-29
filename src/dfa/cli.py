"""Command line entry points."""

from __future__ import annotations

import argparse
import random
import sys
import threading
import time
from pathlib import Path

from .config import load_config
from .models import LeagueSettings
from .session import DraftSession


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dfa", description="Fantasy draft analyzer")
    parser.add_argument("--config", type=Path, default=None, help="path to config.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="refresh the cached player pool")
    p_fetch.add_argument("--force", action="store_true")

    p_serve = sub.add_parser("serve", help="dashboard only, picks entered manually")

    p_watch = sub.add_parser("watch", help="dashboard + live ESPN league draft")
    p_watch.add_argument("--league", help="ESPN league id (overrides config)")
    p_watch.add_argument("--slot", type=int, help="your draft slot, 1-indexed")

    sub.add_parser("app", help="launcher: sign in, pick a league, choose a mode")

    p_mock = sub.add_parser("mock", help="dashboard + browser watcher for ESPN mocks")
    p_mock.add_argument("--slot", type=int, help="your draft slot, 1-indexed")
    p_mock.add_argument(
        "--random-slot",
        action="store_true",
        help="draw a random draft slot, so you practise from every position",
    )
    p_mock.add_argument("--teams", type=int, default=10)
    p_mock.add_argument("--cdp", help="attach to a running Chrome, e.g. http://localhost:9222")
    p_mock.add_argument("--headless", action="store_true")
    p_mock.add_argument("--capture", action="store_true", help="record traffic for debugging")
    p_mock.add_argument(
        "--no-dashboard-tab",
        action="store_true",
        help="don't auto-open the dashboard in a second browser tab",
    )
    p_mock.add_argument(
        "--practice",
        nargs="?",
        const="",
        metavar="LEAGUE_NAME",
        help="start a league-specific practice draft against ESPN auto teams "
             "(defaults to the league in config.toml)",
    )

    p_check = sub.add_parser("check", help="verify ESPN access and print detected settings")
    p_check.add_argument("--league", help="ESPN league id (overrides config)")

    sub.add_parser("simulate", help="run an offline simulated draft (no server)")

    args = parser.parse_args(argv)

    # Line-buffer stdout so progress and errors reach the log immediately.
    # Python block-buffers when stdout isn't a terminal, which meant a draft
    # that died in the background left an empty log and nothing to diagnose.
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    config = load_config(args.config)

    if args.command == "fetch":
        return cmd_fetch(config, force=args.force)
    if args.command == "serve":
        return cmd_serve(config)
    if args.command == "watch":
        return cmd_watch(config, args)
    if args.command == "app":
        return cmd_app(config, args)
    if args.command == "mock":
        return cmd_mock(config, args)
    if args.command == "check":
        return cmd_check(config, args)
    if args.command == "simulate":
        return cmd_simulate(config)
    return 1


def cmd_check(config, args) -> int:
    """Confirm we can read the league, and show what was auto-detected."""
    from .watch.espn_league import EspnLeagueWatcher

    league_id = args.league or config.league_id
    if not league_id:
        print("No league id. Pass --league or set espn.league_id in config.toml.",
              file=sys.stderr)
        return 2

    print(f"League {league_id}, season {config.season}")
    print(f"Cookies present: {'yes' if config.has_private_league_auth else 'no'}")

    watcher = EspnLeagueWatcher(
        league_id=league_id,
        season=config.season,
        espn_s2=config.espn_s2,
        swid=config.swid,
    )
    try:
        snapshot = watcher.fetch()
    except PermissionError as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"\nFAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    settings = snapshot.settings
    print("\nConnected. Detected settings:")
    print(f"  teams:      {settings.teams}")
    print(f"  scoring:    {settings.scoring}")
    print(f"  rounds:     {settings.rounds}")
    print(f"  draft type: {'snake' if settings.snake else 'auction/linear'}")
    print(f"  starters:   {settings.starters}")
    print(f"  bench:      {settings.bench_size}")
    print(f"\n  your team id:   {settings.my_team_id}")
    print(f"  your draft slot: {settings.my_draft_slot}")
    if settings.my_team_id is None:
        print("  (could not identify your team - check the SWID cookie)")
    if settings.my_draft_slot is None:
        print("  (draft order not set yet - set my_draft_slot in config.toml)")

    print(f"\n  teams in league ({len(snapshot.team_names)}):")
    for team_id, name in sorted(snapshot.team_names.items()):
        mine = "  <- you" if team_id == settings.my_team_id else ""
        print(f"    {team_id}: {name}{mine}")

    print(f"\n  draft in progress: {snapshot.in_progress}")
    print(f"  draft complete:    {snapshot.complete}")
    print(f"  picks recorded:    {len(snapshot.picks)}")
    return 0


def cmd_fetch(config, force: bool = False) -> int:
    session = DraftSession(config=config)
    players = session.load_players(force=force)
    print(f"Loaded {len(players)} players for {config.season} ({config.league.scoring}).")
    print("Replacement levels:")
    for pos, level in sorted(session.board.replacement.items()):
        print(f"  {pos:4s} {level:7.1f}")
    print("\nTop 10 by VOR:")
    for rp in session.board.ranked[:10]:
        print(f"  {rp.overall_rank:3d} {rp.player.name:24s} {rp.player.pos:4s} "
              f"T{rp.tier}  VOR {rp.vor:6.1f}")
    return 0


def cmd_serve(config) -> int:
    session = _prepare(config)
    session.status = "manual"
    session.status_detail = "Enter picks with the search box."
    _run_server(config, session)
    return 0


def cmd_watch(config, args) -> int:
    league_id = args.league or config.league_id
    if not league_id:
        print("No league id. Pass --league or set espn.league_id in config.toml.", file=sys.stderr)
        return 2
    if args.slot:
        config.league.my_draft_slot = args.slot

    session = _prepare(config)
    session.status = "watching"
    session.status_detail = f"Polling ESPN league {league_id}"

    thread = threading.Thread(
        target=_poll_league, args=(session, config, league_id), daemon=True
    )
    thread.start()
    _run_server(config, session)
    return 0


def _poll_league(session: DraftSession, config, league_id: str) -> None:
    from .watch.espn_league import EspnLeagueWatcher, apply_snapshot, position_lookup

    watcher = EspnLeagueWatcher(
        league_id=league_id,
        season=config.season,
        espn_s2=config.espn_s2,
        swid=config.swid,
    )
    positions = position_lookup(session.players)
    settings_applied = False

    while True:
        try:
            snapshot = watcher.fetch(player_pos=positions)
            if not settings_applied:
                session.apply_settings(snapshot.settings)
                session.team_names = dict(snapshot.team_names)
                session.slot_to_team = dict(snapshot.slot_to_team)
                settings_applied = True
                print(f"League: {snapshot.settings.teams} teams, "
                      f"{snapshot.settings.scoring}, "
                      f"slot {snapshot.settings.my_draft_slot}")
            state = session.ensure_state()
            new = apply_snapshot(state, snapshot)
            for pick in new:
                ranked = session.board.get(pick.player_id) if session.board else None
                if ranked:
                    pick.player_name = ranked.player.name
                    pick.pos = ranked.player.pos
                print(f"  {pick.round}.{pick.pick_in_round:02d} #{pick.overall} "
                      f"{pick.player_name} ({pick.pos})")
            if snapshot.complete:
                session.status = "complete"
                session.status_detail = "Draft complete."
                return
        except PermissionError as exc:
            session.status = "error"
            session.status_detail = str(exc)
            print(str(exc), file=sys.stderr)
            return
        except Exception as exc:
            session.status_detail = f"poll error: {exc}"
        time.sleep(config.poll_interval)


def cmd_app(config, args) -> int:
    """Serve the launcher. Modes are started from the UI, not the CLI."""
    session = _prepare(config)
    session.status = "idle"
    session.status_detail = "Choose a mode from the menu."
    url = f"http://{config.host}:{config.port}/"
    print(f"\n  Fantasy Draft Analyzer: {url}\n", flush=True)

    def open_browser():
        import webbrowser

        time.sleep(1.5)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=open_browser, daemon=True).start()
    _run_server(config, session)
    return 0


def cmd_mock(config, args) -> int:
    config.league.teams = args.teams
    if args.random_slot:
        # Drawn per run so you get reps from the turn, the wheel and the middle.
        config.league.my_draft_slot = random.randint(1, config.league.teams)
        print(f"Random draft slot: {config.league.my_draft_slot} "
              f"of {config.league.teams}")
    elif args.slot:
        config.league.my_draft_slot = args.slot
    elif config.league.my_draft_slot is None:
        # Without a slot we can't tell when it's your turn, so the on-the-clock
        # shortlist would never appear. Borrow it from the real league.
        detected = _detect_draft_slot(config)
        if detected:
            config.league.my_draft_slot = detected
            print(f"Draft slot {detected} detected from your league.")
        else:
            print("No draft slot known - pass --slot N so the on-the-clock "
                  "shortlist can tell when it's your turn.", file=sys.stderr)

    session = _prepare(config)
    session.status = "watching"
    session.status_detail = "Browser watcher: join a mock draft in the opened window."

    thread = threading.Thread(target=_run_mock, args=(session, config, args), daemon=True)
    thread.start()
    _run_server(config, session)
    return 0


def _run_mock(session: DraftSession, config, args) -> None:
    from .sources.firefox_cookies import load_espn_cookies
    from .watch.espn_mock import MockDraftWatcher

    def on_pick(observed):
        pick = session.record_pick(observed.player_id, team_id=observed.team_id)
        if pick:
            print(f"  [{observed.source}] {pick.round}.{pick.pick_in_round:02d} "
                  f"{pick.player_name} ({pick.pos})")

    cookies = load_espn_cookies()
    if cookies:
        print(f"Loaded {len(cookies)} ESPN cookies from Firefox.")
    else:
        print("No Firefox session found - you'll need to log in to ESPN in the "
              "browser window that opens.")

    watcher = MockDraftWatcher(
        players=session.players,
        on_pick=on_pick,
        headless=args.headless,
        cdp_url=args.cdp,
        user_data_dir=None if args.cdp else config.cache_dir / "browser-profile",
        capture_dir=config.cache_dir / "capture" if args.capture else None,
        cookies=cookies,
    )
    try:
        watcher.start(url=None if args.practice is not None else None)

        if args.practice is not None:
            league = args.practice or _league_display_name(config)
            print(f"Starting practice draft for {league!r} at slot "
                  f"{config.league.my_draft_slot}…")
            watcher.start_practice_draft(league, draft_slot=config.league.my_draft_slot)
            print("Practice draft open. Make your own picks in the draft tab.")
        else:
            watcher.page.goto("https://fantasy.espn.com/football/mockdraftlobby")
            print("Browser open. Join a mock draft; picks will stream to the dashboard.")

        draft_page = watcher.page
        if not args.no_dashboard_tab:
            _open_dashboard_tab(watcher, config)
            draft_page.bring_to_front()

        # Picks arrive on the draft websocket. Idle via the page rather than
        # time.sleep: Playwright's sync API only dispatches events while we
        # are inside a Playwright call, so a plain sleep would buffer every
        # frame until the next interaction and the board would appear frozen.
        strikes = 0
        while True:
            if watcher.my_team_id is not None and session.state:
                session.state.settings.my_team_id = watcher.my_team_id
            try:
                watcher.page.wait_for_timeout(1000)
                strikes = 0
            except Exception as exc:
                # Playwright's sync driver can raise from its own frame-detach
                # handler when an ad iframe disappears mid-draft
                # ("list.remove(x): x not in list"). That is transient and must
                # not take the watcher down in the middle of your draft.
                if _page_is_gone(watcher):
                    session.status = "complete"
                    session.status_detail = "Draft tab closed."
                    print("\nDraft tab closed - watcher stopped. "
                          "Dashboard stays up; Ctrl+C to exit.")
                    return
                strikes += 1
                if strikes >= 15:
                    session.status = "error"
                    session.status_detail = f"browser watcher gave up: {exc}"
                    print(f"Browser watcher gave up after repeated errors: {exc}",
                          file=sys.stderr)
                    return
                time.sleep(1.0)
    except Exception as exc:
        session.status = "error"
        session.status_detail = f"browser watcher failed: {exc}"
        print(f"Browser watcher failed: {exc}", file=sys.stderr)


def _page_is_gone(watcher) -> bool:
    """True when the draft tab is genuinely closed, not just erroring."""
    try:
        return watcher.page is None or watcher.page.is_closed()
    except Exception:
        # If we can't even ask, the driver is dead - treat it as gone.
        return True


def _open_dashboard_tab(watcher, config, wait_seconds: float = 20.0) -> None:
    """Open the analyzer in a second tab, once the server answers.

    Opening it blind races the server startup and lands on a connection error.
    """
    import urllib.error
    import urllib.request

    url = f"http://{config.host}:{config.port}/"
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url + "api/state?top=3", timeout=2).read()
            break
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    else:
        print(f"Dashboard not responding yet; open {url} manually.", file=sys.stderr)
        return

    try:
        tab = watcher.page.context.new_page()
        tab.goto(url, wait_until="domcontentloaded")
        print(f"Dashboard tab open at {url}")
    except Exception as exc:
        print(f"Could not open dashboard tab ({exc}); open {url} manually.",
              file=sys.stderr)


def _detect_draft_slot(config) -> int | None:
    """Read our draft slot from the real league, best effort."""
    if not config.league_id:
        return None
    from .watch.espn_league import EspnLeagueWatcher

    try:
        watcher = EspnLeagueWatcher(config.league_id, config.season,
                                    config.espn_s2, config.swid)
        return watcher.fetch().settings.my_draft_slot
    except Exception:
        return None


def _league_display_name(config) -> str:
    """Best-effort league name for the practice-draft lobby row."""
    from .watch.espn_league import EspnLeagueWatcher

    if not config.league_id:
        raise RuntimeError("no league configured; pass --practice 'League Name'")
    watcher = EspnLeagueWatcher(config.league_id, config.season,
                                config.espn_s2, config.swid)
    resp = watcher._client.get(watcher.url, params={"view": "mSettings"})
    resp.raise_for_status()
    return (resp.json().get("settings") or {}).get("name") or ""


def cmd_simulate(config) -> int:
    """Offline sanity check: draft by ADP and print the board at intervals."""
    from .simulate import run_simulation

    session = _prepare(config)
    run_simulation(session)
    return 0


def _prepare(config) -> DraftSession:
    session = DraftSession(config=config)
    print("Loading player pool…")
    session.load_players()
    session.ensure_state(config.league)
    print(f"  {len(session.players)} players, {config.league.scoring} scoring.")
    return session


def _run_server(config, session: DraftSession) -> None:
    import uvicorn

    from .server.app import create_app

    app = create_app(session)
    print(f"\n  Dashboard: http://{config.host}:{config.port}\n")
    uvicorn.run(app, host=config.host, port=config.port, log_level="warning")


if __name__ == "__main__":
    raise SystemExit(main())
