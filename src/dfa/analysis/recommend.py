"""Composite pick recommendations.

Blends four independent signals so no single one can dominate:
  value   - VOR, how much better than a replacement starter
  need    - open starting slots on your roster
  urgency - VONA plus the odds he is gone by your next pick
  bargain - how far he has slipped past his ADP
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import FLEX_POSITIONS, DraftState, Player
from .board import Board
from .flow import adp_value, survival_probability, vona_by_position
from .roster import RosterState

# Injury status -> multiplier applied to the final score.
INJURY_PENALTY = {
    "ACTIVE": 1.0, "NORMAL": 1.0, "PROBABLE": 0.99, "QUESTIONABLE": 0.96,
    "DOUBTFUL": 0.85, "OUT": 0.70, "SUSPENSION": 0.70,
    "INJURY_RESERVE": 0.45, "IR": 0.45, "PUP": 0.55, "DAY_TO_DAY": 0.97,
}


@dataclass
class Recommendation:
    player: Player
    score: float
    vor: float
    tier: int
    pos_rank: int
    need: float
    vona: float
    survival: float          # P(available at my next pick)
    bargain: float           # picks fallen past ADP
    reasons: list[str] = field(default_factory=list)

    @property
    def is_value(self) -> bool:
        return self.bargain >= 8

    @property
    def will_likely_last(self) -> bool:
        return self.survival >= 0.65


def recommend(
    board: Board,
    state: DraftState,
    roster: RosterState,
    limit: int = 30,
) -> list[Recommendation]:
    """Rank the available pool for the team currently on the clock."""
    drafted = state.drafted_ids
    available = board.available(drafted)[: max(limit * 4, 120)]
    if not available:
        return []

    current_overall = state.next_overall
    my_picks = state.my_next_picks(count=2)
    # The pick after this one is what we might have to wait for.
    next_pick = my_picks[1] if len(my_picks) > 1 else None
    vona = vona_by_position(board, state, next_pick)

    # Normalise VOR across the players actually on the board rather than
    # clamping at zero. Late in a draft everyone is below replacement, and a
    # clamp would flatten the whole field to a single value.
    vors = [rp.vor for rp in available]
    lo_vor, hi_vor = min(vors), max(vors)
    vor_span = (hi_vor - lo_vor) or 1.0
    max_vona = max(vona.values(), default=1.0) or 1.0

    rounds_left = _rounds_left(state)

    out: list[Recommendation] = []
    for rp in available:
        player = rp.player
        pos = player.pos

        value_component = (rp.vor - lo_vor) / vor_span
        fit = roster_fit(rp, roster, rounds_left)
        need_component = roster.needs.get(pos, 0.3)
        urgency_component = vona.get(pos, 0.0) / max_vona

        survival = survival_probability(player.adp, next_pick) if next_pick else 0.0
        bargain = adp_value(player.adp, current_overall)
        bargain_component = max(-0.5, min(1.0, bargain / 24.0))

        score = (
            0.50 * value_component * fit
            + 0.22 * need_component
            + 0.18 * urgency_component * fit
            + 0.10 * bargain_component
        )
        # Waiting is cheap if he is very likely to still be there next time.
        if survival > 0.8:
            score *= 0.93
        score *= INJURY_PENALTY.get(player.injury, 0.9)

        # Two starters at one position sharing a bye means a hole you have to
        # patch off waivers. A real cost, but a small one - never a reason to
        # pass on a clearly better player.
        clash = roster.bye_clash(pos, player.bye)
        if clash and pos not in ("K", "DST"):
            score *= max(0.90, 1.0 - 0.05 * clash)

        out.append(
            Recommendation(
                player=player,
                score=round(score * 100, 1),
                vor=rp.vor,
                tier=rp.tier,
                pos_rank=rp.pos_rank,
                need=need_component,
                vona=vona.get(pos, 0.0),
                survival=round(survival, 2),
                bargain=bargain,
                reasons=_reasons(rp, roster, vona, survival, bargain, board, drafted),
            )
        )

    out.sort(key=lambda r: r.score, reverse=True)
    return out[:limit]


def roster_fit(rp, roster: RosterState, rounds_left: int) -> float:
    """How much this player would actually improve the lineup I can start.

    Raw VOR is roster-blind: it rates a third tight end exactly as highly as
    the first, even though only one of them can ever be in the lineup. This
    scales value by what the pick is really worth to *this* roster.
    """
    pos = rp.player.pos

    if pos in ("K", "DST"):
        # One each, and never before the end of the draft.
        if roster.is_capped(pos):
            return 0.0
        return 1.0 if rounds_left <= 2 else 0.05

    if roster.open_slots.get(pos, 0) > 0:
        return 1.0
    if pos in FLEX_POSITIONS and roster.open_slots.get("FLEX", 0) > 0:
        return 1.0

    # Every starting spot he could claim is taken - would he displace one?
    worst = roster.worst_starter(pos)
    if worst is not None and rp.player.proj > worst:
        return 0.75

    # Pure bench. Real value, but a fraction of a starter's.
    return 0.30


def _rounds_left(state) -> int:
    total = state.settings.teams * state.settings.rounds
    remaining = max(0, total - state.next_overall + 1)
    return remaining // max(1, state.settings.teams)


def _reasons(rp, roster, vona, survival, bargain, board, drafted) -> list[str]:
    """Short human-readable justifications shown next to each recommendation."""
    player = rp.player
    pos = player.pos
    reasons: list[str] = []

    if bargain >= 12:
        reasons.append(f"fallen {int(bargain)} picks past ADP")
    elif bargain >= 6:
        reasons.append(f"slight value ({int(bargain)} past ADP)")

    if roster.open_slots.get(pos):
        reasons.append(f"fills open {pos} slot")
    elif roster.needs.get(pos, 0) >= 0.6:
        reasons.append(f"{pos} still thin")

    # Last player in his tier is a genuine cliff warning.
    same_tier = [
        r for r in board.available_at(pos, drafted) if r.tier == rp.tier
    ]
    if len(same_tier) == 1:
        reasons.append(f"last of {pos} tier {rp.tier}")
    elif len(same_tier) <= 3:
        reasons.append(f"only {len(same_tier)} left in tier {rp.tier}")

    if vona.get(pos, 0) >= 25:
        reasons.append(f"steep {pos} drop-off after this")

    if survival and survival < 0.25:
        reasons.append("very unlikely to last")
    elif survival and survival > 0.8:
        reasons.append("should still be there next pick")

    if player.is_injury_risk:
        reasons.append(f"injury: {player.injury.replace('_', ' ').lower()}")

    if roster.bye_clash(pos, player.bye):
        reasons.append(f"bye {player.bye} clash at {pos}")

    return reasons[:4]
