"""FastAPI app serving the live draft dashboard."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from ..session import DraftSession
from ..watch.espn_league import EspnLeagueWatcher

# How many players to keep news and risk warm for. The board shows 30, but
# runs and reaches mean you often click well past that, and fetches are
# cached and concurrent so the extra depth costs little after the first pass.
NEWS_DEPTH = 120

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


def create_app(session: DraftSession) -> FastAPI:
    app = FastAPI(title="Fantasy Draft Analyzer", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.state.session = session
    app.state.news_thread = None
    app.state.depth_charts = None
    if not hasattr(app.state, "store"):
        from ..auth import SessionStore
        from ..config import PROJECT_ROOT

        app.state.store = SessionStore(
            PROJECT_ROOT / "espn-session.json",
            PROJECT_ROOT / "cache" / "browser-profile",
        )
    from ..runner import ModeRunner

    app.state.runner = ModeRunner(session, session.config)
    _adopt_session(app, session, app.state.store)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return templates.TemplateResponse(request, "launcher.html", {"session": session})

    @app.get("/board", response_class=HTMLResponse)
    def board(request: Request):
        return templates.TemplateResponse(request, "dashboard.html", {"session": session})

    # -- sign-in -----------------------------------------------------------

    @app.get("/api/auth")
    def api_auth():
        store = app.state.store
        data = store.state.payload()
        data["selected_league"] = store.selected_league
        return JSONResponse(data)

    @app.post("/api/auth/login")
    def api_login():
        """Open a browser for the user to sign in to ESPN."""
        store = app.state.store
        if store.state.status in ("opening", "waiting"):
            return {"ok": True, "already": True}

        def worker():
            from ..auth import sign_in

            if sign_in(store):
                _adopt_session(app, session, store)

        threading.Thread(target=worker, daemon=True).start()
        return {"ok": True}

    @app.post("/api/auth/logout")
    def api_logout():
        app.state.store.clear()
        _LEAGUE_CACHE_RESET()
        return {"ok": True}

    # -- mode control ------------------------------------------------------

    @app.get("/api/modes")
    def api_modes(league: str | None = None):
        """What this league can currently do: practice, draft, free agency."""
        store = app.state.store
        league_id = league or store.selected_league
        if not league_id:
            raise HTTPException(status_code=400, detail="no league selected")
        try:
            snapshot = EspnLeagueWatcher(
                league_id, session.config.season,
                session.config.espn_s2, session.config.swid,
            ).fetch()
        except Exception as exc:
            raise HTTPException(
                status_code=404,
                detail=f"could not read league: {type(exc).__name__}",
            ) from exc

        s = snapshot.settings
        return JSONResponse({
            "league_id": league_id,
            "teams": s.teams,
            "rounds": s.rounds,
            "scoring": s.scoring,
            "my_team_id": s.my_team_id,
            "my_draft_slot": s.my_draft_slot,
            "draft_in_progress": snapshot.in_progress,
            "draft_complete": snapshot.complete,
            # Free agency only makes sense once there are rosters to analyse.
            "free_agency_ready": snapshot.complete,
            "runner": app.state.runner.state.payload(),
        })

    @app.post("/api/mode/practice")
    def api_start_practice(league: str, slot: int, pick_seconds: int = 45):
        snapshot = _league_snapshot(session, league)
        if slot < 1 or slot > snapshot.settings.teams:
            raise HTTPException(
                status_code=400,
                detail=f"slot must be between 1 and {snapshot.settings.teams}",
            )
        app.state.store.selected_league = league
        app.state.store.save()
        app.state.runner.start_practice(
            snapshot.settings, slot,
            league_name=_league_name(session, league),
            pick_seconds=pick_seconds,
        )
        return {"ok": True, "slot": slot}

    @app.post("/api/mode/live")
    def api_start_live(league: str):
        _league_snapshot(session, league)  # validates access
        app.state.store.selected_league = league
        app.state.store.save()
        app.state.runner.start_live(league, _league_name(session, league))
        return {"ok": True}

    @app.post("/api/mode/stop")
    def api_stop_mode():
        app.state.runner.stop()
        return {"ok": True}

    @app.post("/api/practice/pick/{player_id}")
    def api_practice_pick(player_id: int):
        if not app.state.runner.submit_pick(player_id):
            raise HTTPException(status_code=409, detail="not your pick right now")
        return {"ok": True}

    @app.post("/api/mode/backfill")
    def api_backfill(league: str | None = None):
        """Recover picks made before we joined the draft room."""
        league_id = league or app.state.store.selected_league
        if not league_id:
            raise HTTPException(status_code=400, detail="no league selected")
        recovered = app.state.runner.backfill_from_capture(league_id)
        return {"ok": True, "recovered": recovered,
                "picks": len(session.state.picks) if session.state else 0}

    @app.get("/api/mode")
    def api_mode():
        return JSONResponse(app.state.runner.state.payload())

    @app.get("/api/state")
    def api_state(top: int = 30):
        data = session.dashboard(top_n=top)
        data["runner"] = app.state.runner.state.payload()
        # News covers well beyond the visible board so a player is never
        # clicked into an empty panel.
        _kick_news_refresh(app, session, NEWS_DEPTH)
        return JSONResponse(data)

    @app.post("/api/pick/{player_id}")
    def api_pick(player_id: int):
        """Manually record a pick - the fallback when a watcher can't see one."""
        pick = session.record_pick(player_id)
        if pick is None:
            raise HTTPException(status_code=409, detail="player already drafted")
        return {"ok": True, "overall": pick.overall, "name": pick.player_name}

    @app.post("/api/undo")
    def api_undo():
        pick = session.undo_pick()
        return {"ok": bool(pick), "undone": pick.player_name if pick else None}

    @app.get("/api/search")
    def api_search(q: str, limit: int = 12):
        """Type-ahead over undrafted players, for manual entry."""
        if not session.board:
            return JSONResponse([])
        needle = q.lower().strip()
        if not needle:
            return JSONResponse([])
        drafted = session.state.drafted_ids if session.state else set()
        out = []
        for rp in session.board.ranked:
            if rp.id in drafted:
                continue
            if needle in rp.player.name.lower():
                out.append(
                    {
                        "id": rp.id,
                        "name": rp.player.name,
                        "pos": rp.player.pos,
                        "team": rp.player.pro_team,
                    }
                )
                if len(out) >= limit:
                    break
        return JSONResponse(out)

    # -- pre-draft prep ----------------------------------------------------

    @app.get("/prep", response_class=HTMLResponse)
    def prep(request: Request):
        return templates.TemplateResponse(request, "prep.html", {"session": session})

    @app.get("/api/prep/teams")
    def api_prep_teams():
        charts = _depth_charts(session)
        return JSONResponse([
            {"team_id": c.team_id, "abbrev": c.abbrev} for c in charts
        ])

    @app.get("/api/prep/team/{team_id}")
    def api_prep_team(team_id: int):
        from ..sources.depthchart import headshot_url
        from ..sources.history import fetch_histories
        from ..sources.risk import STATUS_SEVERITY

        chart = next(
            (c for c in _depth_charts(session) if c.team_id == team_id), None
        )
        if chart is None:
            raise HTTPException(status_code=404, detail="unknown team")

        ids = [e.espn_id for rows in chart.positions.values() for e in rows]
        histories = fetch_histories(
            ids,
            season=session.config.season - 1,
            scoring=session.config.league.scoring,
            cache_dir=session.config.cache_dir,
        )
        pool = {rp.id: rp for rp in (session.board.ranked if session.board else [])}

        def entry_payload(entry):
            hist = histories.get(entry.espn_id)
            ranked = pool.get(entry.espn_id)
            player = ranked.player if ranked else None
            return {
                "id": entry.espn_id,
                "name": entry.name,
                "rank": entry.rank,
                "headshot": headshot_url(entry.espn_id),
                "adp": player.adp if player else None,
                "proj": round(player.proj, 1) if player else None,
                "injury": player.injury if player else None,
                "injury_bad": bool(
                    player and STATUS_SEVERITY.get(player.injury)
                ),
                "tags": session.tags.tags_for(entry.espn_id),
                "note": session.tags.notes.get(entry.espn_id, ""),
                "history": None if hist is None else {
                    "season": hist.season,
                    "total": hist.total,
                    "games": hist.games,
                    "ppg": hist.ppg,
                    "best": hist.best_week,
                    "weekly": [
                        {"week": w, "pts": hist.weekly[w]}
                        for w in sorted(hist.weekly)
                    ],
                    "missed": hist.missed_weeks,
                },
            }

        return JSONResponse({
            "team_id": chart.team_id,
            "abbrev": chart.abbrev,
            "scoring": session.config.league.scoring,
            "positions": {
                pos: [entry_payload(e) for e in rows]
                for pos, rows in chart.positions.items()
            },
        })

    @app.get("/api/prep/injuries/{player_id}")
    def api_prep_injuries(player_id: int):
        """Injury & recovery history: current status, injury-classified news,
        and games missed inferred from last season's log."""
        from ..sources.history import fetch_histories
        from ..sources.risk import classify_about

        ranked = session.board.get(player_id) if session.board else None
        name = ranked.player.name if ranked else ""

        session.refresh_news_for([player_id])
        items = session.news.get(player_id, [])
        injury_news = []
        for item in items:
            level, phrase = classify_about(
                f"{item.headline} {item.story}", name or item.headline
            )
            if level in ("medium", "high"):
                injury_news.append({
                    "age": item.age_label,
                    "headline": item.headline[:200],
                    "level": level,
                })

        session.refresh_risk_for([player_id])
        risk = session.risk.get(player_id)

        hist = fetch_histories(
            [player_id],
            season=session.config.season - 1,
            scoring=session.config.league.scoring,
            cache_dir=session.config.cache_dir,
        ).get(player_id)

        return JSONResponse({
            "current_status": ranked.player.injury if ranked else None,
            "risk_level": risk.level if risk else "none",
            "risk_notes": risk.notes[:5] if risk else [],
            "injury_news": injury_news[:6],
            "missed_last_season": hist.missed_weeks if hist else [],
            "games_last_season": hist.games if hist else None,
        })

    @app.get("/api/tags")
    def api_tags():
        return JSONResponse({
            "vocabulary": session.tags.vocabulary,
            "player_tags": {
                str(k): v for k, v in session.tags.player_tags.items()
            },
            "tagged_count": session.tags.tagged_count,
        })

    @app.post("/api/tags/{player_id}/{tag_id}")
    def api_toggle_tag(player_id: int, tag_id: str):
        try:
            tags = session.tags.toggle(player_id, tag_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown tag")
        return {"ok": True, "tags": tags}

    @app.post("/api/tags/new")
    def api_new_tag(label: str, tone: str = "good"):
        try:
            tag = session.tags.add_tag_type(label, tone)
        except ValueError:
            raise HTTPException(status_code=400, detail="empty tag label")
        return {"ok": True, "tag": tag}

    @app.get("/api/leagues")
    def api_leagues():
        """Every fantasy football league this account belongs to."""
        return JSONResponse(_my_leagues(session))

    # -- in-season waiver wire ---------------------------------------------

    @app.get("/waivers", response_class=HTMLResponse)
    def waivers(request: Request):
        return templates.TemplateResponse(request, "waivers.html", {"session": session})

    @app.get("/api/waivers")
    def api_waivers(league: str | None = None, refresh: bool = False):
        from ..analysis.waiver import (
            SECTION_META, find_opportunities, group_opportunities,
            my_exposed_starters,
        )

        league_id = league or session.config.league_id
        if not league_id:
            raise HTTPException(status_code=400, detail="no league configured")

        try:
            pools = _league_pools(session, league_id, refresh)
        except Exception as exc:
            # An unknown or inaccessible league is a client mistake, not a
            # server fault - ESPN's 404 used to surface as a bare 500.
            raise HTTPException(
                status_code=404,
                detail=f"could not read league {league_id}: {type(exc).__name__}",
            ) from exc
        by_id = {p.espn_id: p for p in session.players}
        for p in pools.free_agents + pools.my_roster:
            by_id.setdefault(p.espn_id, p)

        opps = find_opportunities(
            pools.free_agents,
            session.depth_index(),
            by_id,
            pools.my_roster,
            pools.trend,
            replacement=getattr(pools, "replacement", None)
            or (session.board.replacement if session.board else {}),
            limit=200,
        )
        grouped = group_opportunities(opps)

        def payload(o):
            return {
                "id": o.player.espn_id,
                "name": o.player.name,
                "pos": o.player.pos,
                "team": o.player.pro_team,
                "owned": round(o.player.percent_owned, 1),
                "proj": round(o.player.proj, 1),
                "injury": o.player.injury,
                "score": round(o.score, 1),
                "headline": o.headline,
                "reasons": o.reasons,
                "blocked_by": o.blocked_by,
                "vor": round(o.vor, 1),
                "trend": round(o.trend, 1),
            }

        return JSONResponse({
            "league_id": league_id,
            "my_roster": [
                {"name": p.name, "pos": p.pos, "team": p.pro_team, "injury": p.injury}
                for p in pools.my_roster
            ],
            "exposed": [
                {"name": p.name, "pos": p.pos, "injury": p.injury}
                for p in my_exposed_starters(pools.my_roster)
            ],
            "free_agent_count": len(pools.free_agents),
            # True when nobody available beats a replacement starter - normal
            # in a shallow league, and worth saying rather than showing a
            # section that looks broken.
            "none_above_replacement": not any(
                o.vor > 0 for o in grouped.get("gem", [])
            ),
            # Nobody is moving much this week - worth saying, so a short
            # "being added" list doesn't read as a broken feed.
            "quiet_wire": max(
                (o.trend for o in grouped.get("rising", [])), default=0.0
            ) < 1.5,
            "sections": [
                {
                    "id": key,
                    "title": title,
                    "blurb": blurb,
                    "players": [payload(o) for o in grouped.get(key, [])[:12]],
                }
                for key, title, blurb in SECTION_META
            ],
        })

    @app.get("/api/history/{player_id}")
    def api_history(player_id: int):
        """Last season's week-by-week scoring, under this league's rules.

        Fetched on hover rather than folded into /api/state: the board polls
        every couple of seconds and carries 120 players, so shipping every
        game log on every poll would be wasteful. Per-player files are cached
        for a week, so a repeat hover is instant.
        """
        from ..sources.history import fetch_histories

        hist = fetch_histories(
            [player_id],
            season=session.config.season - 1,
            scoring=session.config.league.scoring,
            cache_dir=session.config.cache_dir,
        ).get(player_id)
        ranked = session.board.get(player_id) if session.board else None
        return JSONResponse({
            "id": player_id,
            "name": ranked.player.name if ranked else "",
            "scoring": session.config.league.scoring,
            "history": None if hist is None else {
                "season": hist.season,
                "total": hist.total,
                "games": hist.games,
                "ppg": hist.ppg,
                "best": hist.best_week,
                "weekly": [{"week": w, "pts": hist.weekly[w]} for w in sorted(hist.weekly)],
                "missed": hist.missed_weeks,
            },
        })

    @app.get("/api/player/{player_id}")
    def api_player(player_id: int):
        rp = session.board.get(player_id) if session.board else None
        if not rp:
            raise HTTPException(status_code=404, detail="unknown player")
        items = session.news.get(player_id, [])
        return {
            "name": rp.player.name,
            "pos": rp.player.pos,
            "team": rp.player.pro_team,
            "proj": rp.player.proj,
            "prev": rp.player.prev_season_points,
            "outlook": rp.player.outlook,
            "news": [
                {"headline": n.headline, "story": n.story, "age": n.age_label}
                for n in items
            ],
        }

    return app


def _adopt_session(app, session: DraftSession, store) -> None:
    """Push a signed-in ESPN session into the config the fetchers read."""
    if not store.state.signed_in:
        return
    session.config.espn_s2 = store.state.espn_s2
    session.config.swid = store.state.swid
    session.config.browser_cookies = store.state.cookies
    _LEAGUE_CACHE_RESET()


def _LEAGUE_CACHE_RESET() -> None:
    """Forget cached league/roster lookups after the session changes."""
    global _LEAGUE_CACHE
    _LEAGUE_CACHE = None
    _POOL_CACHE.clear()


def _league_snapshot(session: DraftSession, league_id: str):
    try:
        return EspnLeagueWatcher(
            league_id, session.config.season,
            session.config.espn_s2, session.config.swid,
        ).fetch()
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail=f"could not read league {league_id}: {type(exc).__name__}",
        ) from exc


def _league_name(session: DraftSession, league_id: str) -> str:
    for entry in _my_leagues(session):
        if str(entry.get("id")) == str(league_id):
            return entry.get("name") or ""
    return ""


def _depth_charts(session: DraftSession):
    """Depth charts for all teams, built once per process."""
    from ..sources.depthchart import build_name_lookup, fetch_depth_charts
    from ..sources.risk import RiskFeed

    if getattr(_depth_charts, "_cache", None) is not None:
        return _depth_charts._cache

    if session.risk_feed is None:
        session.risk_feed = RiskFeed(cache_dir=session.config.cache_dir)
    session.risk_feed._load_sleeper(force=False)

    lookup = build_name_lookup(session.players, session.risk_feed._sleeper)
    charts = fetch_depth_charts(
        season=session.config.season,
        name_lookup=lookup,
        cache_dir=session.config.cache_dir,
    )
    _depth_charts._cache = charts
    return charts


_LEAGUE_CACHE: list[dict] | None = None
_POOL_CACHE: dict[str, tuple[float, object]] = {}


def _my_leagues(session: DraftSession) -> list[dict]:
    """List the account's leagues via ESPN's fan API.

    The league endpoints need an id you already know; this is the only place
    that will tell you which leagues you are actually in.
    """
    global _LEAGUE_CACHE
    if _LEAGUE_CACHE is not None:
        return _LEAGUE_CACHE

    import httpx

    from ..sources.espn_players import USER_AGENT

    config = session.config
    leagues: list[dict] = []
    try:
        resp = httpx.get(
            f"https://fan.api.espn.com/apis/v2/fans/{config.swid}",
            params={"context": "fantasy", "lang": "en", "region": "us"},
            headers={"User-Agent": USER_AGENT},
            cookies={"espn_s2": config.espn_s2, "SWID": config.swid},
            timeout=30.0,
        )
        resp.raise_for_status()
        for pref in resp.json().get("preferences") or []:
            entry = (pref.get("metaData") or {}).get("entry") or {}
            groups = entry.get("groups") or []
            if not groups or entry.get("gameId") != 1:
                continue  # gameId 1 is football
            if entry.get("seasonId") and entry["seasonId"] != config.season:
                continue
            leagues.append({
                "id": str(groups[0].get("groupId")),
                "name": groups[0].get("groupName") or "League",
                "team": entry.get("entryMetadata", {}).get("teamName") or entry.get("name"),
            })
    except Exception:
        pass

    if not leagues and config.league_id:
        leagues = [{"id": config.league_id, "name": "My league", "team": None}]
    _LEAGUE_CACHE = leagues
    return leagues



POOL_TTL = 300  # seconds; the waiver wire does not move every second


def _league_pools(session: DraftSession, league_id: str, refresh: bool = False):
    """League free agents and rosters, cached briefly - the fetch is slow."""
    import time as _time

    from ..sources.freeagents import fetch_pools
    from ..watch.espn_league import EspnLeagueWatcher

    hit = _POOL_CACHE.get(league_id)
    if hit and not refresh and _time.time() - hit[0] < POOL_TTL:
        return hit[1]

    my_team_id = None
    settings = None
    try:
        snapshot = EspnLeagueWatcher(
            league_id, session.config.season,
            session.config.espn_s2, session.config.swid,
        ).fetch()
        my_team_id = snapshot.settings.my_team_id
        settings = snapshot.settings
    except Exception:
        pass

    pools = fetch_pools(
        league_id,
        session.config.season,
        session.config.league.scoring,
        session.config.espn_s2,
        session.config.swid,
        my_team_id=my_team_id,
    )
    # Replacement levels must reflect the league being *viewed*, not whichever
    # league the draft board happens to be configured for - an 8-team league
    # has a much shallower baseline than a 10-team one. Computed standalone so
    # viewing waivers never mutates a live draft session's board.
    if settings is not None:
        from ..analysis.board import replacement_levels

        pools.replacement = replacement_levels(session.players, settings)
        pools.settings = settings

    _POOL_CACHE[league_id] = (_time.time(), pools)
    return pools


def _kick_news_refresh(app: FastAPI, session: DraftSession, top: int) -> None:
    """Refresh news for the current top-N off the request thread.

    The dashboard polls every couple of seconds; news must never block it.
    Results land in the session and appear on the following poll.
    """
    thread = app.state.news_thread
    if thread and thread.is_alive():
        return

    # Reuse the ids the dashboard just ranked rather than re-ranking here.
    ids = list(session.last_rec_ids[:max(top, NEWS_DEPTH)])
    if not ids:
        return

    def worker():
        try:
            session.refresh_news_for(ids)
            # Risk builds on the news we just pulled, so it runs second.
            session.refresh_risk_for(ids)
        except Exception:
            pass

    app.state.news_thread = threading.Thread(target=worker, daemon=True)
    app.state.news_thread.start()
