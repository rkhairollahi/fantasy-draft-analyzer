"""Core domain objects shared by the sources, analysis and watch layers."""

from __future__ import annotations

from dataclasses import dataclass, field

# ESPN's numeric position ids, and the order we like to show them in.
POSITION_BY_ID = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DST"}
POSITION_ORDER = ["QB", "RB", "WR", "TE", "K", "DST"]

# ESPN proTeamId -> abbreviation. 0 is the free-agent bucket.
PRO_TEAM_BY_ID = {
    0: "FA", 1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL",
    7: "DEN", 8: "DET", 9: "GB", 10: "TEN", 11: "IND", 12: "KC", 13: "LV",
    14: "LAR", 15: "MIA", 16: "MIN", 17: "NE", 18: "NO", 19: "NYG", 20: "NYJ",
    21: "PHI", 22: "ARI", 23: "PIT", 24: "LAC", 25: "SF", 26: "SEA", 27: "TB",
    28: "WSH", 29: "CAR", 30: "JAX", 33: "BAL", 34: "HOU",
}

# Which positions may legally fill a FLEX slot.
FLEX_POSITIONS = ("RB", "WR", "TE")


@dataclass
class Player:
    """A draftable player with everything the board needs to rank them."""

    espn_id: int
    name: str
    pos: str
    pro_team: str
    proj: float = 0.0
    adp: float | None = None
    espn_rank: int | None = None
    auction_value: float = 0.0
    percent_owned: float = 0.0
    injury: str = "ACTIVE"
    outlook: str = ""
    prev_season_points: float | None = None
    bye: int | None = None

    @property
    def is_injury_risk(self) -> bool:
        return self.injury not in ("ACTIVE", "NORMAL", "")

    @property
    def short_name(self) -> str:
        """`Jahmyr Gibbs` -> `J. Gibbs`, for tight table columns."""
        parts = self.name.split()
        if len(parts) < 2:
            return self.name
        return f"{parts[0][0]}. {' '.join(parts[1:])}"


@dataclass
class LeagueSettings:
    """Roster shape and scoring, either auto-detected from ESPN or configured."""

    teams: int = 10
    rounds: int = 16
    scoring: str = "PPR"  # PPR | HALF | STANDARD
    # Starting lineup requirements. FLEX is RB/WR/TE.
    starters: dict[str, int] = field(
        default_factory=lambda: {
            "QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "DST": 1,
        }
    )
    snake: bool = True
    my_team_id: int | None = None
    my_draft_slot: int | None = None  # 1-indexed position in round 1

    @property
    def bench_size(self) -> int:
        return max(0, self.rounds - sum(self.starters.values()))

    def starters_at(self, pos: str) -> int:
        return self.starters.get(pos, 0)


@dataclass
class Pick:
    """A single completed selection."""

    overall: int
    round: int
    pick_in_round: int
    team_id: int
    player_id: int
    player_name: str = ""
    pos: str = ""
    keeper: bool = False
    auto: bool = False


@dataclass
class DraftState:
    """Mutable record of everything that has happened in the draft so far."""

    settings: LeagueSettings
    picks: list[Pick] = field(default_factory=list)
    # Set once the source tells us the draft is finished.
    complete: bool = False

    @property
    def drafted_ids(self) -> set[int]:
        return {p.player_id for p in self.picks}

    @property
    def next_overall(self) -> int:
        return len(self.picks) + 1

    def roster(self, team_id: int) -> list[Pick]:
        return [p for p in self.picks if p.team_id == team_id]

    def my_roster(self) -> list[Pick]:
        if self.settings.my_team_id is None:
            return []
        return self.roster(self.settings.my_team_id)

    def slot_on_the_clock(self, overall: int | None = None) -> int:
        """Snake-order draft slot (1-indexed) that owns a given overall pick."""
        overall = self.next_overall if overall is None else overall
        teams = self.settings.teams
        idx = (overall - 1) % teams
        rnd = (overall - 1) // teams
        if self.settings.snake and rnd % 2 == 1:
            idx = teams - 1 - idx
        return idx + 1

    def round_and_pick(self, overall: int | None = None) -> tuple[int, int]:
        overall = self.next_overall if overall is None else overall
        teams = self.settings.teams
        return (overall - 1) // teams + 1, (overall - 1) % teams + 1

    def my_next_picks(self, count: int = 3) -> list[int]:
        """Overall pick numbers I own from here forward."""
        slot = self.settings.my_draft_slot
        if not slot:
            return []
        total = self.settings.teams * self.settings.rounds
        out = []
        for overall in range(self.next_overall, total + 1):
            if self.slot_on_the_clock(overall) == slot:
                out.append(overall)
                if len(out) >= count:
                    break
        return out

    def is_my_pick(self) -> bool:
        slot = self.settings.my_draft_slot
        return slot is not None and self.slot_on_the_clock() == slot
