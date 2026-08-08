"""Per-player news, fetched concurrently for whoever is near the top of the board.

Note the host: `site.web.api.espn.com` serves this feed, while the otherwise
similar `site.api.espn.com` returns 403 for it.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .espn_players import USER_AGENT

NEWS_URL = "https://site.web.api.espn.com/apis/fantasy/v2/games/ffl/news/players"
TRENDING_URL = "https://api.sleeper.app/v1/players/nfl/trending/{kind}"


@dataclass
class NewsItem:
    player_id: int
    headline: str
    story: str
    published: str  # ISO 8601

    @property
    def age_days(self) -> float | None:
        try:
            when = datetime.fromisoformat(self.published.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None
        return (datetime.now(timezone.utc) - when).total_seconds() / 86400

    @property
    def age_label(self) -> str:
        days = self.age_days
        if days is None:
            return ""
        if days < 1:
            hours = max(1, int(days * 24))
            return f"{hours}h ago"
        if days < 14:
            return f"{int(days)}d ago"
        return f"{int(days / 7)}w ago"


def fetch_news(
    player_ids: list[int],
    limit_per_player: int = 3,
    cache_dir: Path | None = None,
    ttl: int = 1800,
    max_concurrent: int = 8,
) -> dict[int, list[NewsItem]]:
    """Fetch recent news for each player id, using a disk cache between runs."""
    results: dict[int, list[NewsItem]] = {}
    to_fetch: list[int] = []

    for pid in player_ids:
        cached = _read_cache(cache_dir, pid, ttl)
        if cached is not None:
            results[pid] = cached
        else:
            to_fetch.append(pid)

    if to_fetch:
        with httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=15.0,
            limits=httpx.Limits(max_connections=max_concurrent),
        ) as client:
            for pid in to_fetch:
                items = _fetch_one(client, pid, limit_per_player)
                results[pid] = items
                _write_cache(cache_dir, pid, items)
    return results


def _fetch_one(client: httpx.Client, player_id: int, limit: int) -> list[NewsItem]:
    try:
        resp = client.get(NEWS_URL, params={"playerId": player_id, "limit": limit})
        resp.raise_for_status()
        feed = resp.json().get("feed") or []
    except Exception:
        # News is a nice-to-have; a failure must never stall the draft board.
        return []

    items = []
    for entry in feed[:limit]:
        items.append(
            NewsItem(
                player_id=player_id,
                headline=(entry.get("headline") or "").strip(),
                story=_clean(entry.get("story") or ""),
                published=entry.get("published") or "",
            )
        )
    return items


def fetch_trending(kind: str = "add", lookback_hours: int = 48, limit: int = 50) -> dict[str, int]:
    """Sleeper's trending adds/drops, keyed by Sleeper player id.

    Used only as a soft 'buzz' signal, so failures degrade to an empty dict.
    """
    try:
        resp = httpx.get(
            TRENDING_URL.format(kind=kind),
            params={"lookback_hours": lookback_hours, "limit": limit},
            timeout=15.0,
        )
        resp.raise_for_status()
        return {row["player_id"]: row.get("count", 0) for row in resp.json()}
    except Exception:
        return {}


def _clean(story: str) -> str:
    """Strip the HTML ESPN embeds in story bodies."""
    out, depth = [], 0
    for ch in story:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return " ".join("".join(out).split())


def _cache_path(cache_dir: Path | None, player_id: int) -> Path | None:
    if not cache_dir:
        return None
    news_dir = cache_dir / "news"
    news_dir.mkdir(parents=True, exist_ok=True)
    return news_dir / f"{player_id}.json"


def _read_cache(cache_dir: Path | None, player_id: int, ttl: int) -> list[NewsItem] | None:
    path = _cache_path(cache_dir, player_id)
    if not path or not path.exists():
        return None
    if (time.time() - path.stat().st_mtime) > ttl:
        return None
    try:
        return [NewsItem(**row) for row in json.loads(path.read_text())]
    except Exception:
        return None


def _write_cache(cache_dir: Path | None, player_id: int, items: list[NewsItem]) -> None:
    path = _cache_path(cache_dir, player_id)
    if path:
        path.write_text(json.dumps([asdict(i) for i in items]))
