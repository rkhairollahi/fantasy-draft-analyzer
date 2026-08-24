"""Last season's week-by-week fantasy production, scored under league rules.

Uses the same `leaguedefaults/<preset>` endpoint as the player pool, so the
weekly points automatically reflect the scoring flavour (PPR/half/standard)
the user is drafting for. Missed weeks are derived from the game log: a
scoring period with no stat line during weeks the player's team played is a
game he sat out - which doubles as a cheap injury-history signal.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .espn_players import BASE, RANK_TYPE, SCORING_PRESET, USER_AGENT

REGULAR_SEASON_WEEKS = 18
_BATCH = 40  # filterIds per request; keeps payloads well under limits


@dataclass
class SeasonHistory:
    espn_id: int
    season: int
    weekly: dict[int, float] = field(default_factory=dict)  # week -> points
    total: float = 0.0

    @property
    def games(self) -> int:
        return len(self.weekly)

    @property
    def ppg(self) -> float:
        return round(self.total / self.games, 1) if self.games else 0.0

    @property
    def missed_weeks(self) -> list[int]:
        """Weeks inside his active span with no stat line.

        Bounded by first and last game so offseason/pre-debut weeks don't
        count. A rookie or late acquisition therefore shows no false missed
        games, at the cost of missing absences at the season's very edges.
        """
        if not self.weekly:
            return []
        weeks = sorted(self.weekly)
        return [w for w in range(weeks[0], weeks[-1] + 1) if w not in self.weekly]

    @property
    def best_week(self) -> float:
        return round(max(self.weekly.values()), 1) if self.weekly else 0.0


def fetch_histories(
    espn_ids: list[int],
    season: int,
    scoring: str = "PPR",
    cache_dir: Path | None = None,
    ttl: int = 7 * 24 * 3600,
) -> dict[int, SeasonHistory]:
    """Prior-season weekly lines for the given players, cached aggressively
    (a finished season doesn't change)."""
    scoring = scoring.upper()
    out: dict[int, SeasonHistory] = {}
    to_fetch: list[int] = []

    for espn_id in espn_ids:
        cached = _read_cache(cache_dir, espn_id, season, scoring, ttl)
        if cached is not None:
            out[espn_id] = cached
        else:
            to_fetch.append(espn_id)

    preset = SCORING_PRESET.get(scoring, 3)
    url = f"{BASE}/{season}/segments/0/leaguedefaults/{preset}"
    for start in range(0, len(to_fetch), _BATCH):
        batch = to_fetch[start : start + _BATCH]
        try:
            resp = httpx.get(
                url,
                params={"view": "kona_player_info"},
                headers={
                    "User-Agent": USER_AGENT,
                    "x-fantasy-filter": json.dumps(
                        {"players": {"filterIds": {"value": batch}}}
                    ),
                },
                timeout=60.0,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception:
            continue  # history is enrichment; the page must render without it

        for wrapper in payload.get("players", []):
            raw = wrapper.get("player") or {}
            history = _parse_history(raw, season)
            out[history.espn_id] = history
            _write_cache(cache_dir, history, scoring)

    return out


def _parse_history(raw: dict, season: int) -> SeasonHistory:
    history = SeasonHistory(espn_id=raw.get("id"), season=season)
    for stat in raw.get("stats") or []:
        if stat.get("seasonId") != season or stat.get("statSourceId") != 0:
            continue
        total = stat.get("appliedTotal")
        if stat.get("statSplitTypeId") == 0:
            history.total = round(total or 0.0, 1)
        elif stat.get("statSplitTypeId") == 1:
            week = stat.get("scoringPeriodId")
            if week and 1 <= week <= REGULAR_SEASON_WEEKS and total is not None:
                history.weekly[week] = round(total, 1)
    if not history.total and history.weekly:
        history.total = round(sum(history.weekly.values()), 1)
    return history


def _cache_path(cache_dir: Path | None, espn_id: int, season: int, scoring: str):
    if not cache_dir:
        return None
    folder = cache_dir / f"history_{season}_{scoring}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{espn_id}.json"


def _read_cache(cache_dir, espn_id, season, scoring, ttl) -> SeasonHistory | None:
    path = _cache_path(cache_dir, espn_id, season, scoring)
    if not path or not path.exists():
        return None
    if time.time() - path.stat().st_mtime > ttl:
        return None
    try:
        data = json.loads(path.read_text())
        return SeasonHistory(
            espn_id=data["espn_id"],
            season=data["season"],
            weekly={int(k): v for k, v in data["weekly"].items()},
            total=data["total"],
        )
    except Exception:
        return None


def _write_cache(cache_dir, history: SeasonHistory, scoring: str) -> None:
    path = _cache_path(cache_dir, history.espn_id, history.season, scoring)
    if path:
        path.write_text(json.dumps({
            "espn_id": history.espn_id,
            "season": history.season,
            "weekly": history.weekly,
            "total": history.total,
        }))
