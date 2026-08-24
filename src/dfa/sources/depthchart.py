"""NFL team depth charts (QB/RB/WR/TE) from ESPN's core API.

Each depth chart entry references an athlete by `$ref` URL; rather than
resolving hundreds of refs we extract the athlete id from the URL and join
names/positions from the Sleeper player DB (6,700+ players carry an espn_id),
falling back to our own top-400 pool.

Sleeper's native depth charts were evaluated and rejected: only 183 players
across the league, missing most starters.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ..models import PRO_TEAM_BY_ID
from .espn_players import USER_AGENT

DEPTH_URL = (
    "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/"
    "seasons/{season}/teams/{team_id}/depthcharts"
)
HEADSHOT_URL = "https://a.espncdn.com/i/headshots/nfl/players/full/{espn_id}.png"

POSITIONS = ("QB", "RB", "WR", "TE")
# Show at most this many per position per team; deeper than this is camp fodder.
MAX_DEPTH = {"QB": 3, "RB": 4, "WR": 6, "TE": 3}

_ATHLETE_ID = re.compile(r"/athletes/(\d+)")


@dataclass
class DepthEntry:
    espn_id: int
    name: str
    pos: str
    rank: int


@dataclass
class TeamDepthChart:
    team_id: int
    abbrev: str
    positions: dict[str, list[DepthEntry]] = field(default_factory=dict)


def fetch_depth_charts(
    season: int,
    name_lookup: dict[int, str],
    cache_dir: Path | None = None,
    ttl: int = 12 * 3600,
    force: bool = False,
) -> list[TeamDepthChart]:
    """All 32 team depth charts, offense skill positions only.

    `name_lookup` maps espn athlete id -> display name; entries with no known
    name are dropped (they are practice-squad depth we can't label).
    """
    cache_file = cache_dir / f"depthcharts_{season}.json" if cache_dir else None
    raw: dict[str, dict] | None = None
    if cache_file and cache_file.exists() and not force:
        if time.time() - cache_file.stat().st_mtime < ttl:
            try:
                raw = json.loads(cache_file.read_text())
            except Exception:
                raw = None

    if raw is None:
        raw = {}
        with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30.0) as client:
            for team_id in sorted(PRO_TEAM_BY_ID):
                if team_id == 0:
                    continue  # free-agent bucket, not a team
                try:
                    resp = client.get(
                        DEPTH_URL.format(season=season, team_id=team_id)
                    )
                    resp.raise_for_status()
                    raw[str(team_id)] = resp.json()
                except Exception:
                    continue  # one missing team must not sink the page
        if cache_file and raw:
            cache_file.write_text(json.dumps(raw))

    charts = []
    for team_key, payload in raw.items():
        team_id = int(team_key)
        chart = _parse_team(team_id, payload, name_lookup)
        if chart.positions:
            charts.append(chart)
    charts.sort(key=lambda c: c.abbrev)
    return charts


def _parse_team(
    team_id: int, payload: dict, name_lookup: dict[int, str]
) -> TeamDepthChart:
    chart = TeamDepthChart(
        team_id=team_id, abbrev=PRO_TEAM_BY_ID.get(team_id, str(team_id))
    )
    # The offensive group is the one whose positions include a quarterback.
    for group in payload.get("items", []):
        positions = group.get("positions") or {}
        if "qb" not in positions:
            continue
        for key in ("qb", "rb", "wr", "te"):
            entry = positions.get(key)
            if not entry:
                continue
            pos = key.upper()
            athletes = entry.get("athletes") or []
            athletes.sort(key=lambda a: a.get("rank") or 99)
            rows: list[DepthEntry] = []
            for athlete in athletes:
                ref = (athlete.get("athlete") or {}).get("$ref", "")
                match = _ATHLETE_ID.search(ref)
                if not match:
                    continue
                espn_id = int(match.group(1))
                name = name_lookup.get(espn_id)
                if not name:
                    continue  # unlabelable practice-squad depth
                rows.append(
                    DepthEntry(
                        espn_id=espn_id,
                        name=name,
                        pos=pos,
                        rank=athlete.get("rank") or len(rows) + 1,
                    )
                )
                if len(rows) >= MAX_DEPTH[pos]:
                    break
            if rows:
                # Merge multi-slot groups (e.g. 3WR sets list wr twice).
                existing = chart.positions.setdefault(pos, [])
                seen = {r.espn_id for r in existing}
                existing.extend(r for r in rows if r.espn_id not in seen)
                existing.sort(key=lambda r: r.rank)
                del existing[MAX_DEPTH[pos]:]
    return chart


def headshot_url(espn_id: int) -> str:
    return HEADSHOT_URL.format(espn_id=espn_id)


def build_name_lookup(players, sleeper_by_espn: dict[int, dict]) -> dict[int, str]:
    """espn id -> name, from our pool first then the wider Sleeper DB."""
    lookup: dict[int, str] = {}
    for espn_id, sleeper in sleeper_by_espn.items():
        name = sleeper.get("full_name")
        if name and sleeper.get("position") in POSITIONS:
            lookup[espn_id] = name
    for p in players:  # our pool wins on conflicts - names are cleaner
        lookup[p.espn_id] = p.name
    return lookup
