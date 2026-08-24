"""Ranking free agents by opportunity, not by projection.

A waiver pickup is worth making for a reason, and the reason matters more than
the raw number: the man behind an injured starter is a different proposition
from a rising ownership spike, and both differ from a cheap high-projection
player nobody rostered. Each candidate carries the reason that surfaced it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import Player

# How much a starter's injury opens the door for the man behind him.
INJURY_OPENING = {
    "INJURY_RESERVE": 1.0, "IR": 1.0, "OUT": 0.95, "PUP": 0.9,
    "SUSPENSION": 0.9, "NON_FOOTBALL_INJURY": 0.85,
    "DOUBTFUL": 0.7, "QUESTIONABLE": 0.4, "DAY_TO_DAY": 0.35,
}
# Ownership swing (percentage points in a week) that counts as real momentum.
RISING_THRESHOLD = 1.5
# Above this ownership a player isn't really a find any more.
GEM_MAX_OWNED = 55.0
# Kickers and defences are streamed weekly; they are never a "find".
STREAMING_POSITIONS = ("K", "DST")


@dataclass
class Opportunity:
    player: Player
    score: float = 0.0
    kind: str = ""              # takeover | handcuff | rising | gem
    headline: str = ""
    reasons: list[str] = field(default_factory=list)
    vor: float = 0.0            # value over a replacement starter
    blocked_by: str = ""        # starter ahead of him, if any
    for_my_player: str = ""     # my injured player this covers
    trend: float = 0.0

    @property
    def is_urgent(self) -> bool:
        return self.kind in ("takeover", "handcuff")


def find_opportunities(
    free_agents: list[Player],
    depth_index: dict[int, dict],
    player_by_id: dict[int, Player],
    my_roster: list[Player],
    trend: dict[int, float],
    replacement: dict[str, float] | None = None,
    limit: int = 40,
) -> list[Opportunity]:
    """Rank the free agent pool by why you'd want them."""
    replacement = replacement or {}
    my_ids = {p.espn_id for p in my_roster}
    my_hurt = {
        p.espn_id: p for p in my_roster
        if INJURY_OPENING.get(p.injury, 0) >= 0.4
    }

    out: list[Opportunity] = []
    for fa in free_agents:
        room = depth_index.get(fa.espn_id)
        change = trend.get(fa.espn_id, 0.0)
        opp = Opportunity(player=fa, trend=change)

        starter = _starter_ahead(room, player_by_id) if room else None
        opening = INJURY_OPENING.get(starter.injury, 0.0) if starter else 0.0

        if starter and opening >= 0.4:
            # The man directly behind someone who is hurt.
            mine = starter.espn_id in my_hurt
            opp.kind = "handcuff" if mine else "takeover"
            opp.blocked_by = starter.name
            if mine:
                opp.for_my_player = starter.name
                opp.headline = f"Covers your {starter.name} ({starter.injury.title()})"
            else:
                opp.headline = f"Next up behind {starter.name} ({starter.injury.title()})"
            # Backing up a good player is worth far more than backing up a bad one.
            opp.score = 100 * opening * _value_weight(starter)
            opp.reasons.append(f"{starter.name} listed {starter.injury.replace('_',' ').lower()}")
            if mine:
                opp.score *= 1.6
                opp.reasons.append("on your roster - protect the pick")
        elif change >= RISING_THRESHOLD:
            opp.kind = "rising"
            opp.headline = f"Ownership up {change:.1f} pts this week"
            opp.score = 40 + change * 4
            opp.reasons.append("managers are adding him now")
        elif fa.proj > 0 and fa.pos not in STREAMING_POSITIONS:
            # Score on value over a replacement starter, not raw points. Raw
            # projection floats every backup QB to the top because
            # quarterbacks simply score more.
            #
            # VOR is deliberately not required to be positive: in a shallow
            # league every above-replacement player is already rostered, and a
            # section that renders empty tells you nothing. Ranking what is
            # actually there, with ownership as the "hidden" multiplier, at
            # least answers "who is the best of what's left".
            vor = fa.proj - replacement.get(fa.pos, 0.0)
            hidden = max(0.0, 1.0 - fa.percent_owned / 100.0)
            opp.kind = "gem"
            opp.vor = vor
            opp.headline = (f"{vor:+.0f} vs a replacement {fa.pos}"
                            f" · {fa.percent_owned:.0f}% rostered")
            # Value leads, obscurity is the tie-breaker. Weighted the other
            # way round, a -34 VOR tight end outranked a +8 quarterback purely
            # for being less rostered.
            opp.score = 60 + vor * 0.8 + hidden * 15
            if fa.percent_owned <= GEM_MAX_OWNED:
                opp.reasons.append("under the radar")
        else:
            continue

        if starter and opening < 0.4:
            opp.blocked_by = starter.name
            opp.reasons.append(f"behind {starter.name}")
            opp.score *= 0.55  # blocked by a healthy starter
        if room and room.get("rank") == 1 and not starter:
            opp.reasons.append(f"listed {room['pos']}1 on {room['team']}")
            opp.score *= 1.35
        if change >= RISING_THRESHOLD and opp.kind != "rising":
            opp.reasons.append(f"ownership +{change:.1f} pts")
            opp.score *= 1.15
        if fa.espn_id in my_ids:
            continue  # already yours

        out.append(opp)

    out.sort(key=lambda o: o.score, reverse=True)
    return out[:limit]


def _starter_ahead(room: dict, player_by_id: dict[int, Player]) -> Player | None:
    """The highest-ranked teammate ahead of him at his position."""
    ahead = [m for m in room.get("mates", []) if m.get("ahead")]
    if not ahead:
        return None
    ahead.sort(key=lambda m: m["rank"])
    for mate in ahead:
        found = next(
            (p for p in player_by_id.values() if p.name == mate["name"]), None
        )
        if found:
            return found
    return None


def _value_weight(starter: Player) -> float:
    """Backing up a stud is the whole point; backing up a scrub is not."""
    if starter.adp and starter.adp <= 40:
        return 1.6
    if starter.adp and starter.adp <= 90:
        return 1.25
    if starter.adp and starter.adp <= 160:
        return 1.0
    return 0.7


def my_exposed_starters(my_roster: list[Player]) -> list[Player]:
    """Your own players whose status should worry you."""
    hurt = [p for p in my_roster if INJURY_OPENING.get(p.injury, 0) >= 0.35]
    hurt.sort(key=lambda p: INJURY_OPENING.get(p.injury, 0), reverse=True)
    return hurt


def group_opportunities(opps: list[Opportunity]) -> dict[str, list[Opportunity]]:
    """Split into independently-ranked sections.

    A single merged list is dominated by whichever signal scores highest that
    week - in preseason, injury designations swamp everything - so each kind
    gets its own ranking and nothing is crowded out.
    """
    sections: dict[str, list[Opportunity]] = {
        "handcuff": [], "takeover": [], "rising": [], "gem": [],
    }
    for opp in opps:
        if opp.kind in sections:
            sections[opp.kind].append(opp)
    for rows in sections.values():
        rows.sort(key=lambda o: o.score, reverse=True)
    return sections


SECTION_META = [
    ("handcuff", "Cover your own injured players",
     "Direct backups to players already on your roster who are hurt."),
    ("takeover", "Could take over a starting job",
     "Next man up behind an injured starter somewhere in the league."),
    ("rising", "Being added right now",
     "Ownership climbing fast - the market has noticed something."),
    ("gem", "Hidden gems",
     "Low rostered, but worth more than a replacement starter at the position."),
]
