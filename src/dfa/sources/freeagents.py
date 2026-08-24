"""Free agents and rostered players for a specific ESPN league.

The draft board works off a league-agnostic player pool; in-season you need to
know who is actually available in *your* league, who owns everyone else, and
which of your own players are hurt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx

from ..models import POSITION_BY_ID, PRO_TEAM_BY_ID, Player
from .espn_players import BASE, RANK_TYPE, USER_AGENT, _positive, _season_stat

# ESPN availability buckets we treat as "you can add him".
AVAILABLE = ("FREEAGENT", "WAIVERS")


@dataclass
class LeaguePools:
    free_agents: list[Player] = field(default_factory=list)
    # espn player id -> fantasy team id that owns him
    owned_by: dict[int, int] = field(default_factory=dict)
    # espn player id -> ownership percent change over the last week
    trend: dict[int, float] = field(default_factory=dict)
    my_roster: list[Player] = field(default_factory=list)
    team_names: dict[int, str] = field(default_factory=dict)
    # Replacement levels for *this* league's shape, filled in by the caller.
    replacement: dict[str, float] = field(default_factory=dict)
    settings: object | None = None

    def is_free(self, player_id: int) -> bool:
        return player_id not in self.owned_by


def fetch_pools(
    league_id: str,
    season: int,
    scoring: str = "PPR",
    espn_s2: str | None = None,
    swid: str | None = None,
    my_team_id: int | None = None,
    limit: int = 300,
) -> LeaguePools:
    """Free agents plus a map of who owns everyone else."""
    cookies = {"espn_s2": espn_s2, "SWID": swid} if espn_s2 and swid else {}
    url = f"{BASE}/{season}/segments/0/leagues/{league_id}"
    pools = LeaguePools()

    fantasy_filter = {
        "players": {
            "filterStatus": {"value": list(AVAILABLE)},
            "limit": limit,
            "sortPercOwned": {"sortPriority": 1, "sortAsc": False},
        }
    }
    resp = httpx.get(
        url,
        params={"view": "kona_player_info"},
        headers={"User-Agent": USER_AGENT, "x-fantasy-filter": json.dumps(fantasy_filter)},
        cookies=cookies,
        timeout=60.0,
    )
    resp.raise_for_status()
    for entry in resp.json().get("players", []):
        player = _to_player(entry, season, scoring)
        if player:
            pools.free_agents.append(player)
            pools.trend[player.espn_id] = (
                (entry.get("player", {}).get("ownership") or {}).get("percentChange") or 0.0
            )

    # Rosters tell us who is taken, and which of those are ours.
    roster = httpx.get(
        url,
        params=[("view", "mRoster"), ("view", "mTeam")],
        headers={"User-Agent": USER_AGENT},
        cookies=cookies,
        timeout=60.0,
    )
    roster.raise_for_status()
    data = roster.json()
    for team in data.get("teams", []):
        team_id = team.get("id")
        name = team.get("name") or " ".join(
            filter(None, [team.get("location"), team.get("nickname")])
        )
        pools.team_names[team_id] = (name or f"Team {team_id}").strip()
        for slot in (team.get("roster") or {}).get("entries") or []:
            player_id = slot.get("playerId")
            if not player_id:
                continue
            pools.owned_by[player_id] = team_id
            if my_team_id and team_id == my_team_id:
                player = _to_player(slot.get("playerPoolEntry") or {}, season, scoring)
                if player:
                    pools.my_roster.append(player)
    return pools


def _to_player(entry: dict, season: int, scoring: str) -> Player | None:
    raw = entry.get("player") or {}
    pos = POSITION_BY_ID.get(raw.get("defaultPositionId"))
    if not pos or not raw.get("id"):
        return None
    ownership = raw.get("ownership") or {}
    ranks = (raw.get("draftRanksByRankType") or {}).get(RANK_TYPE.get(scoring, "PPR"), {})
    return Player(
        espn_id=raw["id"],
        name=(raw.get("fullName") or "").strip(),
        pos=pos,
        pro_team=PRO_TEAM_BY_ID.get(raw.get("proTeamId"), "FA"),
        proj=_season_stat(raw, season, 1) or 0.0,
        prev_season_points=_season_stat(raw, season - 1, 0),
        adp=_positive(ownership.get("averageDraftPosition")),
        espn_rank=ranks.get("rank"),
        percent_owned=ownership.get("percentOwned") or 0.0,
        injury=raw.get("injuryStatus") or "ACTIVE",
        outlook=(raw.get("seasonOutlook") or "").strip(),
    )
