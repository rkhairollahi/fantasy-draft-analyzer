"""Player universe: projections, ADP, ranks, injury status and analyst outlooks.

Everything here comes from ESPN's `kona_player_info` view on the *league
defaults* endpoint, which needs no authentication and no league of your own.
Endpoint shapes were verified live against the 2026 season.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

from ..models import POSITION_BY_ID, PRO_TEAM_BY_ID, Player

BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons"

# leaguedefaults/<id> picks the scoring preset the ranks/ADP are drawn from.
SCORING_PRESET = {"STANDARD": 1, "HALF": 2, "PPR": 3}
# The rankType key inside draftRanksByRankType for each scoring flavour.
RANK_TYPE = {"STANDARD": "STANDARD", "HALF": "PPR", "PPR": "PPR"}

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# statSourceId 1 = projection, 0 = actual. statSplitTypeId 0 = full season.
_PROJECTED, _ACTUAL, _SEASON_SPLIT = 1, 0, 0


def fetch_players(
    season: int,
    scoring: str = "PPR",
    limit: int = 400,
    cache_dir: Path | None = None,
    ttl: int = 6 * 3600,
    force: bool = False,
) -> list[Player]:
    """Return the top `limit` draftable players, best-ranked first."""
    scoring = scoring.upper()
    payload = _fetch_raw(season, scoring, limit, cache_dir, ttl, force)
    byes = fetch_bye_weeks(season, cache_dir, ttl, force)

    players: list[Player] = []
    for entry in payload.get("players", []):
        raw = entry.get("player") or {}
        pos = POSITION_BY_ID.get(raw.get("defaultPositionId"))
        if not pos:
            continue  # skip anything we don't model (e.g. team defenses variants)
        pro_team = PRO_TEAM_BY_ID.get(raw.get("proTeamId"), "FA")
        ownership = raw.get("ownership") or {}
        ranks = (raw.get("draftRanksByRankType") or {}).get(RANK_TYPE[scoring], {})

        players.append(
            Player(
                espn_id=raw.get("id"),
                name=raw.get("fullName", "").strip(),
                pos=pos,
                pro_team=pro_team,
                proj=_season_stat(raw, season, _PROJECTED) or 0.0,
                prev_season_points=_season_stat(raw, season - 1, _ACTUAL),
                adp=_positive(ownership.get("averageDraftPosition")),
                espn_rank=ranks.get("rank"),
                auction_value=ranks.get("auctionValue") or 0.0,
                percent_owned=ownership.get("percentOwned") or 0.0,
                injury=raw.get("injuryStatus") or "ACTIVE",
                outlook=(raw.get("seasonOutlook") or "").strip(),
                bye=byes.get(pro_team),
            )
        )

    players.sort(key=lambda p: (p.espn_rank is None, p.espn_rank or 10**6))
    return players


def _fetch_raw(
    season: int, scoring: str, limit: int, cache_dir: Path | None, ttl: int, force: bool
) -> dict:
    cache_file = None
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"players_{season}_{scoring}_{limit}.json"
        if not force and _is_fresh(cache_file, ttl):
            return json.loads(cache_file.read_text())

    preset = SCORING_PRESET.get(scoring, 3)
    # NOTE: do not add filterStatsForTopScoringPeriodIds here - ESPN rejects
    # a value of 0 with "Filter: Value is invalid, must be > 0".
    fantasy_filter = {
        "players": {
            "limit": limit,
            "sortDraftRanks": {
                "sortPriority": 100,
                "sortAsc": True,
                "value": RANK_TYPE[scoring],
            },
        }
    }
    url = f"{BASE}/{season}/segments/0/leaguedefaults/{preset}"
    resp = httpx.get(
        url,
        params={"view": "kona_player_info"},
        headers={
            "User-Agent": USER_AGENT,
            "x-fantasy-filter": json.dumps(fantasy_filter),
        },
        timeout=60.0,
    )
    resp.raise_for_status()
    data = resp.json()
    if cache_file:
        cache_file.write_text(json.dumps(data))
    return data


def fetch_bye_weeks(
    season: int, cache_dir: Path | None = None, ttl: int = 6 * 3600, force: bool = False
) -> dict[str, int]:
    """Map team abbreviation -> bye week."""
    cache_file = None
    if cache_dir:
        cache_file = cache_dir / f"byes_{season}.json"
        if not force and _is_fresh(cache_file, ttl):
            return json.loads(cache_file.read_text())
    try:
        resp = httpx.get(
            f"{BASE}/{season}",
            params={"view": "proTeamSchedules_wl"},
            headers={"User-Agent": USER_AGENT},
            timeout=30.0,
        )
        resp.raise_for_status()
        settings = resp.json().get("settings", {}).get("proTeams", [])
        byes = {
            PRO_TEAM_BY_ID.get(t.get("id"), t.get("abbrev", "")): t.get("byeWeek")
            for t in settings
            if t.get("byeWeek")
        }
    except Exception:
        # A missing bye week is cosmetic; never let it break the board.
        byes = {}
    if cache_file and byes:
        cache_file.write_text(json.dumps(byes))
    return byes


def _season_stat(raw: dict, season: int, source_id: int) -> float | None:
    for stat in raw.get("stats") or []:
        if (
            stat.get("seasonId") == season
            and stat.get("statSourceId") == source_id
            and stat.get("statSplitTypeId") == _SEASON_SPLIT
        ):
            total = stat.get("appliedTotal")
            return round(total, 2) if total else None
    return None


def _positive(value):
    """ESPN uses 0 for 'undrafted' in ADP; treat that as unknown."""
    return value if value and value > 0 else None


def _is_fresh(path: Path | None, ttl: int) -> bool:
    return bool(path and path.exists() and (time.time() - path.stat().st_mtime) < ttl)
