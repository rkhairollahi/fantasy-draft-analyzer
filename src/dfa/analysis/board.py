"""The draft board: value over replacement and tier structure.

VOR is computed once against the full preseason pool so the numbers stay
stable as the draft runs. Anything that should move pick-to-pick (positional
runs, what will survive to your next pick) lives in `flow.py` instead.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from ..models import FLEX_POSITIONS, LeagueSettings, Player

# How a FLEX slot tends to be filled league-wide. Used to push each position's
# replacement level down by its realistic share of flex starts.
FLEX_SHARE = {"RB": 0.45, "WR": 0.45, "TE": 0.10}

# Positions where late-round replacement is so cheap that VOR overstates them.
STREAMING_POSITIONS = ("K", "DST")
STREAMING_VALUE_MULT = 0.15


@dataclass
class RankedPlayer:
    player: Player
    vor: float = 0.0
    tier: int = 1
    pos_rank: int = 0
    overall_rank: int = 0
    sort_value: float = 0.0

    @property
    def id(self) -> int:
        return self.player.espn_id


@dataclass
class Board:
    """Ranked, tiered view of the whole player pool."""

    settings: LeagueSettings
    ranked: list[RankedPlayer] = field(default_factory=list)
    replacement: dict[str, float] = field(default_factory=dict)
    _by_id: dict[int, RankedPlayer] = field(default_factory=dict, repr=False)

    @classmethod
    def build(cls, players: list[Player], settings: LeagueSettings) -> "Board":
        replacement = replacement_levels(players, settings)
        ranked = [
            RankedPlayer(player=p, vor=round(p.proj - replacement.get(p.pos, 0.0), 1))
            for p in players
        ]
        # Kicker and defense scarcity is an illusion - the 12th-best is freely
        # available in the last round - so discount their VOR for ordering
        # purposes while leaving the displayed number honest.
        for rp in ranked:
            mult = STREAMING_VALUE_MULT if rp.player.pos in STREAMING_POSITIONS else 1.0
            rp.sort_value = rp.vor * mult

        ranked.sort(key=lambda r: r.sort_value, reverse=True)
        for i, rp in enumerate(ranked, start=1):
            rp.overall_rank = i

        _assign_pos_ranks(ranked)
        _assign_tiers(ranked)

        board = cls(settings=settings, ranked=ranked, replacement=replacement)
        board._by_id = {rp.id: rp for rp in ranked}
        return board

    def get(self, player_id: int) -> RankedPlayer | None:
        return self._by_id.get(player_id)

    def available(self, drafted: set[int]) -> list[RankedPlayer]:
        return [rp for rp in self.ranked if rp.id not in drafted]

    def available_at(self, pos: str, drafted: set[int]) -> list[RankedPlayer]:
        return [rp for rp in self.ranked if rp.player.pos == pos and rp.id not in drafted]


# How deep into the draft to look when deriving baselines from ADP. Round 10
# is the conventional cutoff: by then every team has its starters and the rest
# of the position is genuinely replaceable off waivers.
BASELINE_ROUNDS = 10


def replacement_levels(players: list[Player], settings: LeagueSettings) -> dict[str, float]:
    """Projected points of the last startable player at each position.

    Prefers an empirical baseline: how many players at each position the market
    actually drafts inside the starter window, taken from live ADP. Assuming
    `teams x starting slots` badly misprices positions whose real roster demand
    differs from their slot count - in 2026 PPR the market drafts 35 WRs by
    pick 100 against 24 starting slots, while taking only 9 TEs against 10.

    Falls back to the slot-count method when ADP is unavailable.
    """
    by_pos: dict[str, list[float]] = {}
    for p in players:
        by_pos.setdefault(p.pos, []).append(p.proj)
    for projections in by_pos.values():
        projections.sort(reverse=True)

    empirical = _adp_baseline_counts(players, settings)

    levels: dict[str, float] = {}
    for pos, projections in by_pos.items():
        depth = empirical.get(pos) or _slot_baseline_count(pos, settings)
        if depth <= 0:
            levels[pos] = projections[0] if projections else 0.0
            continue
        # Index of the last startable player; clamp to the pool we have.
        idx = min(depth, len(projections)) - 1
        levels[pos] = projections[idx] if idx >= 0 else 0.0
    return levels


def _slot_baseline_count(pos: str, settings: LeagueSettings) -> int:
    """Baseline from starting slots, including a share of the flex."""
    starters = settings.starters_at(pos)
    if pos in FLEX_POSITIONS:
        starters += FLEX_SHARE.get(pos, 0.0) * settings.starters_at("FLEX")
    return int(round(settings.teams * starters))


def _adp_baseline_counts(
    players: list[Player], settings: LeagueSettings
) -> dict[str, int]:
    """Players per position drafted inside the starter window, per ADP."""
    cutoff = settings.teams * BASELINE_ROUNDS
    counts: dict[str, int] = {}
    for p in players:
        if p.adp and p.adp <= cutoff:
            counts[p.pos] = counts.get(p.pos, 0) + 1

    if sum(counts.values()) < cutoff * 0.5:
        return {}  # too little ADP coverage to trust

    # Never go shallower than the position's own dedicated starting slots -
    # those players are started by definition.
    for pos in list(counts):
        floor = settings.teams * settings.starters_at(pos)
        counts[pos] = max(counts[pos], floor)
    return counts


def _assign_pos_ranks(ranked: list[RankedPlayer]) -> None:
    counters: dict[str, int] = {}
    for rp in ranked:
        pos = rp.player.pos
        counters[pos] = counters.get(pos, 0) + 1
        rp.pos_rank = counters[pos]


def _assign_tiers(ranked: list[RankedPlayer], depth: int = 48) -> None:
    """Split each position into tiers of bounded projected-points spread.

    A tier stays open until someone falls more than `width` points below the
    player who opened it. Splitting purely on outlier gaps produces one-man
    tiers at the top of a position and ten-man tiers in the middle, because
    the gap distribution is heavily skewed; bounding the spread instead keeps
    tiers comparable in meaning all the way down the board.
    """
    by_pos: dict[str, list[RankedPlayer]] = {}
    for rp in ranked:
        by_pos.setdefault(rp.player.pos, []).append(rp)

    for group in by_pos.values():
        group.sort(key=lambda r: r.player.proj, reverse=True)
        width = _tier_width(group, depth)

        tier = 1
        leader = group[0].player.proj if group else 0.0
        for i, rp in enumerate(group):
            if i >= depth:
                # Past the analysed depth everyone shares one trailing tier.
                rp.tier = tier + 1
                continue
            if rp.player.proj < leader - width:
                tier += 1
                leader = rp.player.proj
            rp.tier = tier


def _tier_width(group: list[RankedPlayer], depth: int) -> float:
    """Points of spread allowed inside one tier, scaled to the position."""
    projections = [rp.player.proj for rp in group[: min(depth, 24)] if rp.player.proj > 0]
    if len(projections) < 4:
        return 12.0
    return max(8.0, 0.45 * statistics.pstdev(projections))
