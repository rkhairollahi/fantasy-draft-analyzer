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
