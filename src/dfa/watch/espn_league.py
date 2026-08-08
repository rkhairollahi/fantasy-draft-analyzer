"""Live draft watcher for a real ESPN league, by polling the league API.

Works for public leagues unauthenticated; private leagues need the `espn_s2`
and `SWID` cookies from a logged-in browser session.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from ..models import POSITION_BY_ID, DraftState, LeagueSettings, Pick
from ..sources.espn_players import BASE, USER_AGENT

# ESPN lineupSlotId -> our slot names, for reading rosterSettings.
LINEUP_SLOT = {
    0: "QB", 2: "RB", 4: "WR", 6: "TE", 16: "DST", 17: "K",
    23: "FLEX", 20: "BENCH", 21: "IR",
}
# Slots that are really a flex by another name.
_FLEX_LIKE = {3, 5, 7, 23}
BENCH_SLOT, IR_SLOT = 20, 21

RECEPTION_STAT_ID = 53


@dataclass
class LeagueSnapshot:
    settings: LeagueSettings
    picks: list[Pick]
    in_progress: bool
    complete: bool
    team_names: dict[int, str]
    # Draft slot (1-indexed) -> ESPN team id. These are NOT the same numbers,
    # so the clock needs this to name the team that is picking.
    slot_to_team: dict[int, int]


class EspnLeagueWatcher:
    """Polls one league and reports draft picks as they land."""

    def __init__(
        self,
        league_id: str,
        season: int,
        espn_s2: str | None = None,
        swid: str | None = None,
        timeout: float = 20.0,
    ):
        self.league_id = league_id
        self.season = season
        self.cookies = {}
        if espn_s2 and swid:
            self.cookies = {"espn_s2": espn_s2, "SWID": swid}
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            cookies=self.cookies,
            timeout=timeout,
            follow_redirects=True,
        )

    @property
    def url(self) -> str:
        return f"{BASE}/{self.season}/segments/0/leagues/{self.league_id}"

    def fetch(self, player_pos: dict[int, str] | None = None) -> LeagueSnapshot:
        """One poll: settings, teams and every pick made so far."""
        resp = self._client.get(
            self.url,
            params=[
                ("view", "mDraftDetail"),
                ("view", "mTeam"),
                ("view", "mSettings"),
            ],
        )
        if resp.status_code == 401:
            raise PermissionError(
                "ESPN returned 401 - this league is private. Set espn_s2 and swid "
                "in config.toml (see README for how to copy them out of your browser)."
            )
        resp.raise_for_status()
        data = resp.json()

        settings = self._parse_settings(data)
        team_names = self._parse_team_names(data)
        detail = data.get("draftDetail") or {}
        picks = self._parse_picks(detail, player_pos or {})

        # Identify my team from the SWID on the owners list.
        swid = self.cookies.get("SWID")
        if swid:
            for team in data.get("teams", []):
                if swid in (team.get("owners") or []):
                    settings.my_team_id = team.get("id")
                    break

        settings.my_draft_slot = self._draft_slot(data, settings.my_team_id)

        return LeagueSnapshot(
            settings=settings,
            picks=picks,
            in_progress=bool(detail.get("inProgress")),
            complete=bool(detail.get("drafted")) and not detail.get("inProgress"),
            team_names=team_names,
            slot_to_team=self._slot_to_team(data),
        )

    @staticmethod
    def _slot_to_team(data: dict) -> dict[int, int]:
        order = ((data.get("settings") or {}).get("draftSettings") or {}).get("pickOrder")
        if order:
            return {slot: team_id for slot, team_id in enumerate(order, start=1)}
        # Fall back to round 1 of the picks already made.
        mapping = {}
        for pick in (data.get("draftDetail") or {}).get("picks") or []:
            if pick.get("roundId") == 1 and pick.get("roundPickNumber"):
                mapping[pick["roundPickNumber"]] = pick.get("teamId")
        return mapping

    def _parse_settings(self, data: dict) -> LeagueSettings:
        raw = data.get("settings") or {}
        roster = (raw.get("rosterSettings") or {}).get("lineupSlotCounts") or {}
        draft = raw.get("draftSettings") or {}

        starters: dict[str, int] = {}
        flex_total = 0
        for slot_id_str, count in roster.items():
            slot_id = int(slot_id_str)
            count = int(count or 0)
            if not count or slot_id in (BENCH_SLOT, IR_SLOT):  # not starters
                continue
            if slot_id in _FLEX_LIKE:
                flex_total += count
                continue
            name = LINEUP_SLOT.get(slot_id)
            if name:
                starters[name] = starters.get(name, 0) + count
        if flex_total:
            starters["FLEX"] = flex_total

        teams = int(raw.get("size") or data.get("status", {}).get("teamsJoined") or 10)
        # IR slots are never drafted into, so they don't add draft rounds.
        total_slots = sum(
            int(count or 0)
            for slot_id, count in roster.items()
            if int(slot_id) != IR_SLOT
        )

        settings = LeagueSettings(
            teams=teams,
            rounds=total_slots or 16,
            scoring=self._detect_scoring(raw),
            snake=str(draft.get("type", "SNAKE")).upper() != "AUCTION",
        )
        if starters:
            settings.starters = starters
        return settings

    @staticmethod
    def _detect_scoring(raw: dict) -> str:
        """Read points-per-reception straight out of the league's scoring rules."""
        items = (raw.get("scoringSettings") or {}).get("scoringItems") or []
        for item in items:
            if item.get("statId") == RECEPTION_STAT_ID:
                pts = item.get("points")
                if pts is None:
                    overrides = item.get("pointsOverrides") or {}
                    pts = next(iter(overrides.values()), 0)
                pts = float(pts or 0)
                if pts >= 0.75:
                    return "PPR"
                if pts >= 0.25:
                    return "HALF"
                return "STANDARD"
        return "PPR"

    @staticmethod
    def _parse_team_names(data: dict) -> dict[int, str]:
        names = {}
        for team in data.get("teams", []):
            name = team.get("name") or " ".join(
                filter(None, [team.get("location"), team.get("nickname")])
            )
            names[team.get("id")] = (name or f"Team {team.get('id')}").strip()
        return names

    @staticmethod
    def _parse_picks(detail: dict, player_pos: dict[int, str]) -> list[Pick]:
        picks = []
        for raw in detail.get("picks") or []:
            player_id = raw.get("playerId")
            # ESPN pre-seeds the board with one empty slot per pick, carrying
            # playerId -1. Those are not selections; ingesting them would show
            # a full draft before anyone has picked.
            if not isinstance(player_id, int) or player_id <= 0:
                continue
            picks.append(
                Pick(
                    overall=raw.get("overallPickNumber") or 0,
                    round=raw.get("roundId") or 0,
                    pick_in_round=raw.get("roundPickNumber") or 0,
                    team_id=raw.get("teamId") or 0,
                    player_id=player_id,
                    pos=player_pos.get(player_id, ""),
                    keeper=bool(raw.get("keeper")),
                    auto=bool(raw.get("autoDraftTypeId")),
                )
            )
        picks.sort(key=lambda p: p.overall)
        return picks

    @staticmethod
    def _draft_slot(data: dict, my_team_id: int | None) -> int | None:
        if my_team_id is None:
            return None
        order = ((data.get("settings") or {}).get("draftSettings") or {}).get("pickOrder")
        if order and my_team_id in order:
            return order.index(my_team_id) + 1
        # Fall back to where the team appears in round 1 of the real picks.
        for pick in (data.get("draftDetail") or {}).get("picks") or []:
            if (
                pick.get("roundId") == 1
                and pick.get("teamId") == my_team_id
                and (pick.get("playerId") or 0) > 0
            ):
                return pick.get("roundPickNumber")
        return None

    def close(self) -> None:
        self._client.close()


def apply_snapshot(state: DraftState, snapshot: LeagueSnapshot) -> list[Pick]:
    """Merge a poll into the running state, returning only the new picks."""
    known = {p.overall for p in state.picks}
    fresh = [p for p in snapshot.picks if p.overall not in known]
    if fresh:
        state.picks.extend(fresh)
        state.picks.sort(key=lambda p: p.overall)
    state.complete = snapshot.complete
    return fresh


def position_lookup(players) -> dict[int, str]:
    """espn player id -> position, so picks can be labelled as they arrive."""
    return {p.espn_id: p.pos for p in players}
