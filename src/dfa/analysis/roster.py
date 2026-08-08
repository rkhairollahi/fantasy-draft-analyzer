"""Roster construction: which starting slots are filled, and what you still need."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import FLEX_POSITIONS, POSITION_ORDER, LeagueSettings, Pick

# You can only ever start one of these, and the waiver wire is full of them.
HARD_CAP = {"K": 1, "DST": 1}


@dataclass
class RosterState:
    filled: dict[str, int] = field(default_factory=dict)   # slot -> count filled
    counts: dict[str, int] = field(default_factory=dict)   # position -> players held
    open_slots: dict[str, int] = field(default_factory=dict)
    needs: dict[str, float] = field(default_factory=dict)  # position -> 0..1 urgency
    bench: int = 0
    # Projected points of the players currently occupying each starting slot,
    # so we can tell whether a new player would actually be an upgrade.
    starter_proj: dict[str, list[float]] = field(default_factory=dict)

    _required: dict[str, int] = field(default_factory=dict, repr=False)

    def slot_summary(self) -> list[tuple[str, int, int]]:
        """(slot, filled, required) in display order."""
        out = []
        for slot in ["QB", "RB", "WR", "TE", "FLEX", "K", "DST"]:
            required = self._required.get(slot, 0)
            if required:
                out.append((slot, self.filled.get(slot, 0), required))
        return out

    def is_capped(self, pos: str) -> bool:
        cap = HARD_CAP.get(pos)
        return cap is not None and self.counts.get(pos, 0) >= cap

    # Bye weeks of players already rostered, by position.
    byes_by_pos: dict[str, list[int]] = field(default_factory=dict)

    def bye_clash(self, pos: str, bye: int | None) -> int:
        """How many rostered players at this position share that bye week."""
        if not bye:
            return 0
        return sum(1 for b in self.byes_by_pos.get(pos, []) if b == bye)

    def worst_starter(self, pos: str) -> float | None:
        """Lowest projection among the slots a player at `pos` could claim."""
        pool = list(self.starter_proj.get(pos, []))
        if pos in FLEX_POSITIONS:
            pool += self.starter_proj.get("FLEX", [])
        return min(pool) if pool else None


def evaluate_roster(
    picks: list[Pick],
    settings: LeagueSettings,
    picks_left: int,
    proj_by_id: dict[int, float] | None = None,
    bye_by_id: dict[int, int] | None = None,
) -> RosterState:
    """Fill starting slots best-player-first, then score positional urgency."""
    proj_by_id = proj_by_id or {}
    bye_by_id = bye_by_id or {}

    byes_by_pos: dict[str, list[int]] = {}
    for pick in picks:
        bye = bye_by_id.get(pick.player_id)
        if pick.pos and bye:
            byes_by_pos.setdefault(pick.pos, []).append(bye)

    by_pos: dict[str, list[float]] = {}
    for pick in picks:
        if pick.pos:
            by_pos.setdefault(pick.pos, []).append(proj_by_id.get(pick.player_id, 0.0))
    for projections in by_pos.values():
        projections.sort(reverse=True)

    counts = {pos: len(v) for pos, v in by_pos.items()}
    remaining = {pos: list(v) for pos, v in by_pos.items()}

    filled: dict[str, int] = {}
    starter_proj: dict[str, list[float]] = {}

    # Dedicated slots take the best players at each position.
    for pos in POSITION_ORDER:
        required = settings.starters_at(pos)
        if not required:
            continue
        pool = remaining.get(pos, [])
        taken = pool[:required]
        filled[pos] = len(taken)
        starter_proj[pos] = taken
        remaining[pos] = pool[required:]

    # Whatever is left competes for FLEX, best first.
    flex_required = settings.starters_at("FLEX")
    flex_candidates: list[tuple[float, str]] = []
    for pos in FLEX_POSITIONS:
        flex_candidates += [(proj, pos) for proj in remaining.get(pos, [])]
    flex_candidates.sort(reverse=True)
    flex_taken = flex_candidates[:flex_required]
    filled["FLEX"] = len(flex_taken)
    starter_proj["FLEX"] = [proj for proj, _ in flex_taken]

    used: dict[str, int] = {}
    for _, pos in flex_taken:
        used[pos] = used.get(pos, 0) + 1
    for pos, count in used.items():
        remaining[pos] = remaining.get(pos, [])[count:]

    open_slots = {}
    for slot in ["QB", "RB", "WR", "TE", "FLEX", "K", "DST"]:
        required = settings.starters_at(slot)
        if required:
            open_slots[slot] = max(0, required - filled.get(slot, 0))

    state = RosterState(
        filled=filled,
        counts=counts,
        open_slots=open_slots,
        bench=sum(len(v) for v in remaining.values()),
        starter_proj=starter_proj,
        byes_by_pos=byes_by_pos,
    )
    state._required = {
        slot: settings.starters_at(slot)
        for slot in ["QB", "RB", "WR", "TE", "FLEX", "K", "DST"]
    }
    state.needs = _need_scores(state, settings, picks_left)
    return state


def _need_scores(
    state: RosterState, settings: LeagueSettings, picks_left: int
) -> dict[str, float]:
    """0..1 urgency per position, blending open starting slots with depth."""
    needs: dict[str, float] = {}
    flex_open = state.open_slots.get("FLEX", 0)

    for pos in POSITION_ORDER:
        required = settings.starters_at(pos)
        if not required and pos not in FLEX_POSITIONS:
            continue

        # K and DST are pure end-of-draft business, and only ever one deep.
        if pos in ("K", "DST"):
            open_direct = state.open_slots.get(pos, 0)
            needs[pos] = 0.9 if (open_direct and picks_left <= 2) else 0.01
            continue

        open_direct = state.open_slots.get(pos, 0)
        score = 0.0
        if open_direct:
            # An unfilled dedicated starting slot is the strongest signal.
            score += 0.6 + 0.15 * min(open_direct - 1, 2)
        if flex_open and pos in FLEX_POSITIONS:
            score += 0.25

        held = state.counts.get(pos, 0)
        target = _depth_target(pos, settings)
        if held >= target:
            score *= 0.35  # already deep here
        elif held == 0 and required:
            score += 0.1

        needs[pos] = round(min(1.0, score), 3)
    return needs


def _depth_target(pos: str, settings: LeagueSettings) -> int:
    """Roughly how many of a position a sane roster wants in total."""
    base = settings.starters_at(pos)
    if pos in ("RB", "WR"):
        return base + 3
    if pos in ("TE", "QB"):
        return base + 1
    return base
