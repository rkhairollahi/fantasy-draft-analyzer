"""Decode the draft room's INIT frame into the picks already made.

Joining a room mid-draft does not replay past picks as SELECTED frames; the
room sends its whole current state once, base64-encoded, as INIT. ESPN's
league API is no help either - it reports playerId -1 for every slot until the
draft finishes - so this is the only way to recover picks made before we
connected.

Layout, verified against a live draft: a run of fixed-width records, each
starting with four big-endian int32s:

    teamId, overallPickNumber, playerId, lineupSlotId

Records are `STRIDE` bytes apart. The run is located by finding the record
whose overallPickNumber is 1 and whose neighbours count up from there, rather
than by trusting a fixed offset.
"""

from __future__ import annotations

import base64
import re
import struct
from dataclasses import dataclass

STRIDE = 45
_INIT_LINE = re.compile(r"^INIT (\S+)", re.M)
_TOKEN_LINE = re.compile(r"^TOKEN \d+:(\d+):")


@dataclass
class InitPick:
    overall: int
    team_id: int
    player_id: int
    slot_id: int


def decode_init(blob: str) -> list[InitPick]:
    """Picks encoded in one INIT payload, in draft order."""
    try:
        raw = base64.b64decode(blob + "===")
    except Exception:
        return []
    start = _find_run_start(raw)
    if start is None:
        return []

    picks: list[InitPick] = []
    offset = start
    expected = 1
    while offset + 16 <= len(raw):
        team_id, overall, player_id, slot_id = struct.unpack_from(">iiii", raw, offset)
        if overall != expected or not _plausible(team_id, player_id):
            break
        picks.append(InitPick(overall, team_id, player_id, slot_id))
        expected += 1
        offset += STRIDE
    return picks


def picks_from_log(text: str, league_id: str | None = None) -> list[InitPick]:
    """Picks from the newest INIT frame belonging to `league_id`.

    A capture log can hold sessions from several leagues, so simply taking the
    INIT with the most records is wrong - it happily returned another league's
    draft. Each INIT is followed by the TOKEN naming the league it belongs to
    (INIT arrives first on the wire), so attribution looks *forward* to the
    next TOKEN.
    """
    lines = text.splitlines()
    pending: list[tuple[int, str]] = []
    per_league: dict[str, list[InitPick]] = {}

    for index, line in enumerate(lines):
        if line.startswith("INIT "):
            pending.append((index, line[5:]))
        elif line.startswith("TOKEN "):
            match = _TOKEN_LINE.match(line)
            if not match:
                continue
            league = match.group(1)
            for _, blob in pending:
                decoded = decode_init(blob)
                if len(decoded) > len(per_league.get(league, [])):
                    per_league[league] = decoded
            pending.clear()

    if league_id is not None:
        return per_league.get(str(league_id), [])
    best: list[InitPick] = []
    for decoded in per_league.values():
        if len(decoded) > len(best):
            best = decoded
    return best


def _find_run_start(raw: bytes) -> int | None:
    """Offset of the record for overall pick 1.

    Prefers an anchor confirmed by a pick-2 record at the same stride, so a
    stray 1 in unrelated binary cannot capture the parse. Falls back to a lone
    plausible record, which is what a draft one pick old actually looks like.
    """
    fallback: int | None = None
    for offset in range(0, len(raw) - 16):
        team_id, overall, player_id, _ = struct.unpack_from(">iiii", raw, offset)
        if overall != 1 or not _plausible(team_id, player_id):
            continue
        if offset + STRIDE + 16 <= len(raw):
            _, next_overall, next_player, _ = struct.unpack_from(
                ">iiii", raw, offset + STRIDE
            )
            if next_overall == 2 and _plausible(1, next_player):
                return offset
        if fallback is None:
            fallback = offset
    return fallback


def _plausible(team_id: int, player_id: int) -> bool:
    """Team ids are small positives; defenses are negative but never -1."""
    if not (1 <= team_id <= 32):
        return False
    return player_id > 0 or player_id < -1
