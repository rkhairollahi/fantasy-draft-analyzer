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

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return templates.TemplateResponse(request, "dashboard.html", {"session": session})

    @app.get("/api/state")
    def api_state(top: int = 30):
        data = session.dashboard(top_n=top)
        _kick_news_refresh(app, session, top)
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


def _kick_news_refresh(app: FastAPI, session: DraftSession, top: int) -> None:
    """Refresh news for the current top-N off the request thread.

    The dashboard polls every couple of seconds; news must never block it.
    Results land in the session and appear on the following poll.
    """
    thread = app.state.news_thread
    if thread and thread.is_alive():
        return

    # Reuse the ids the dashboard just ranked rather than re-ranking here.
    ids = list(session.last_rec_ids[:top])
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
