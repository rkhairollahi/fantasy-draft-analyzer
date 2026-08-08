"""Draft flow: what survives to your next pick, and when a position is running.

This is the part that has to be recomputed every single pick.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..models import POSITION_ORDER, DraftState
from .board import Board, RankedPlayer

# Minimum spread on a player's draft position, in picks. ADP uncertainty grows
# as you go later, so sigma scales with ADP but never collapses below this.
_SIGMA_FLOOR = 4.0
_SIGMA_SCALE = 0.20


@dataclass
class PositionRun:
    pos: str
    count: int
    window: int

    @property
    def label(self) -> str:
        return f"{self.count} of last {self.window} picks were {self.pos}"


def survival_probability(adp: float | None, target_overall: int) -> float:
    """P(player is still on the board when pick `target_overall` comes up)."""
    if adp is None:
        return 0.9  # unranked players rarely get sniped
    sigma = max(_SIGMA_FLOOR, _SIGMA_SCALE * adp)
    # P(draft position > target) under a normal centred on ADP.
    z = (target_overall - adp) / (sigma * math.sqrt(2))
    return max(0.0, min(1.0, 0.5 * math.erfc(z)))


def detect_runs(state: DraftState, window: int = 7, threshold: int = 4) -> list[PositionRun]:
    """Flag positions being taken unusually fast in the recent window."""
    recent = state.picks[-window:]
    if len(recent) < window:
        return []
    counts: dict[str, int] = {}
    for pick in recent:
        if pick.pos:
            counts[pick.pos] = counts.get(pick.pos, 0) + 1
    runs = [
        PositionRun(pos=pos, count=count, window=len(recent))
        for pos, count in counts.items()
        if count >= threshold and pos in ("QB", "RB", "WR", "TE")
    ]
    runs.sort(key=lambda r: r.count, reverse=True)
    return runs


def vona_by_position(
    board: Board, state: DraftState, next_pick: int | None
) -> dict[str, float]:
    """Value Over Next Available: what waiting one round costs at each position.

    Compares the best player available now against the best one likely to still
    be there at your following pick. A large number means the cliff is real.
    """
    drafted = state.drafted_ids
    result: dict[str, float] = {}

    for pos in POSITION_ORDER:
        # Waiting on a kicker or defense costs nothing worth modelling.
        if pos in ("K", "DST"):
            result[pos] = 0.0
            continue
        pool = board.available_at(pos, drafted)
        if not pool:
            result[pos] = 0.0
            continue
        best_now = max(pool[:20], key=lambda rp: rp.player.proj, default=None)
        if best_now is None:
            result[pos] = 0.0
            continue
        if next_pick is None:
            result[pos] = 0.0
            continue

        expected_later = _expected_best_at(pool, next_pick)
        result[pos] = round(max(0.0, best_now.player.proj - expected_later), 1)
    return result


def _expected_best_at(pool: list[RankedPlayer], target_overall: int) -> float:
    """Projected points of the best player at this position likely to survive.

    Walks the position pool in board order and takes an expectation weighted by
    each player's survival probability, which is smoother than picking the first
    player over a 50% threshold.
    """
    remaining_mass = 1.0
    expected = 0.0
    for rp in pool[:40]:
        survives = survival_probability(rp.player.adp, target_overall)
        take = remaining_mass * survives
        expected += take * rp.player.proj
        remaining_mass -= take
        if remaining_mass <= 0.01:
            break
    if remaining_mass > 0.01 and pool:
        expected += remaining_mass * pool[-1].player.proj
    return expected


def adp_value(adp: float | None, current_overall: int) -> float:
    """How far a player has fallen past his ADP. Positive means a bargain."""
    if adp is None:
        return 0.0
    return round(current_overall - adp, 1)
