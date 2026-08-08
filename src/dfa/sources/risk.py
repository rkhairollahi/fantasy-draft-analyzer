"""Injury and red-flag intelligence for the players on your board.

Pulls from four sources the program can reach without a browser or API key:

  Sleeper player DB   structured injury_status / status / body part, joined to
                      our players on Sleeper's own `espn_id` field
  RotoWire RSS        freshest beat-reporter notes, in `Player: update` form
  ESPN player news    per-player feed we already fetch for the board
  Sleeper trending    how many managers are dropping him right now

Everything is cached and refreshed on a background thread, because a 30-second
pick clock leaves no room to go fetch anything when you're on the clock.
"""

from __future__ import annotations

import html
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .espn_players import USER_AGENT

SLEEPER_PLAYERS = "https://api.sleeper.app/v1/players/nfl"
SLEEPER_TRENDING = "https://api.sleeper.app/v1/players/nfl/trending/{kind}"
ROTOWIRE_RSS = "https://www.rotowire.com/rss/news.php?sport=NFL"

# Phrases must be specific. Bare keywords produce nonsense: "retire" matched
# "spoke with retired running back Todd Gurley" and flagged Saquon Barkley as
# retiring; "achilles" and "suspension" misfired the same way.
HIGH_SIGNALS = [
    "placed on injured reserve", "placed on ir", "to injured reserve",
    "season-ending", "season ending", "out for the season", "miss the season",
    "torn acl", "tore his acl", "torn achilles", "tore his achilles",
    "torn meniscus", "ruptured", "underwent surgery", "will undergo surgery",
    "had surgery", "needs surgery", "fractured", "broken foot", "broken hand",
    "broken leg", "broken collarbone", "suspended for", "facing a suspension",
    "his suspension", "announced his retirement", "plans to retire",
    "is retiring", "placed on the pup", "on the pup list",
    "physically unable to perform", "holding out", "contract holdout",
    "carted off", "will miss the",
]
# Phrases that good news cannot undo - being "cleared" does not un-tear an ACL.
TERMINAL_SIGNALS = {
    "placed on injured reserve", "placed on ir", "to injured reserve",
    "season-ending", "season ending", "out for the season", "miss the season",
    "torn acl", "tore his acl", "torn achilles", "tore his achilles",
    "torn meniscus", "ruptured", "underwent surgery", "will undergo surgery",
    "had surgery", "needs surgery", "announced his retirement",
    "plans to retire", "is retiring", "suspended for",
}
MEDIUM_SIGNALS = [
    "did not practice", "didn't practice", "limited participant",
    "limited in practice", "missed practice",
    # Qualified: a bare "questionable" matched "questionable off-field
    # decisions" and flagged a healthy player.
    "is questionable", "listed as questionable", "questionable for",
    "is doubtful", "listed as doubtful",
    "concussion protocol", "strain", "sprain", "soreness", "tightness",
    "day-to-day", "day to day", "setback", "re-injured",
    "contract dispute", "trade request", "lost the starting",
]

# Human-readable badges. Falls back to the matched phrase, uppercased.
FLAG_LABELS = {
    "placed on injured reserve": "IR", "placed on ir": "IR",
    "to injured reserve": "IR", "season-ending": "SEASON", "season ending": "SEASON",
    "out for the season": "SEASON", "miss the season": "SEASON",
    "torn acl": "TORN ACL", "tore his acl": "TORN ACL",
    "torn achilles": "ACHILLES", "tore his achilles": "ACHILLES",
    "torn meniscus": "MENISCUS", "ruptured": "RUPTURE",
    "underwent surgery": "SURGERY", "will undergo surgery": "SURGERY",
    "had surgery": "SURGERY", "needs surgery": "SURGERY",
    "suspended for": "SUSPENDED", "facing a suspension": "SUSP RISK",
    "his suspension": "SUSPENDED", "announced his retirement": "RETIRING",
    "plans to retire": "RETIRING", "is retiring": "RETIRING",
    "placed on the pup": "PUP", "on the pup list": "PUP",
    "physically unable to perform": "PUP", "holding out": "HOLDOUT",
    "contract holdout": "HOLDOUT", "carted off": "CARTED",
    "fractured": "FRACTURE", "will miss the": "WILL MISS",
}


GOOD_SIGNALS = [
    "full participant", "full practice", "returned to practice",
    "back at practice", "cleared", "no limitations", "expected to play",
    "activated", "practiced", "participating in", "expected to be ready",
]


def flag_label(phrase: str) -> str:
    return FLAG_LABELS.get(phrase, phrase.upper()[:10])

# ESPN / Sleeper status strings that are bad news on their own.
STATUS_SEVERITY = {
    "INJURY_RESERVE": "high", "IR": "high", "OUT": "high", "PUP": "high",
    "SUSPENSION": "high", "NON_FOOTBALL_INJURY": "high", "Inactive": "high",
    "DOUBTFUL": "medium", "QUESTIONABLE": "medium", "DAY_TO_DAY": "medium",
    "Questionable": "medium", "Doubtful": "medium", "Out": "high",
}

_LEVEL_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}


@dataclass
class RiskReport:
    player_id: int
    level: str = "none"
    flags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def is_concerning(self) -> bool:
        return _LEVEL_RANK[self.level] >= 2

    def add(self, level: str, flag: str, note: str) -> None:
        if _LEVEL_RANK[level] > _LEVEL_RANK[self.level]:
            self.level = level
        if flag and flag not in self.flags:
            self.flags.append(flag)
        if note and note not in self.notes:
            self.notes.append(note)


class RiskFeed:
    """Caches the shared feeds and builds per-player reports from them."""

    def __init__(self, cache_dir: Path | None = None, ttl: int = 1800):
        self.cache_dir = cache_dir
        self.ttl = ttl
        self._sleeper: dict[int, dict] = {}
        self._sleeper_at = 0.0
        self._rotowire: list[tuple[str, str]] = []
        self._rotowire_at = 0.0
        self._drops: dict[str, int] = {}
        self._sleeper_id_to_espn: dict[str, int] = {}

    # -- feeds -------------------------------------------------------------

    def refresh(self, force: bool = False) -> None:
        """Refresh whichever shared feeds have gone stale."""
        now = time.time()
        if force or now - self._sleeper_at > max(self.ttl, 6 * 3600):
            self._load_sleeper(force)
        if force or now - self._rotowire_at > 600:
            self._load_rotowire()
            self._load_drops()

    def _load_sleeper(self, force: bool) -> None:
        raw = None
        cache = self.cache_dir / "sleeper_players.json" if self.cache_dir else None
        if cache and cache.exists() and not force:
            if time.time() - cache.stat().st_mtime < 6 * 3600:
                try:
                    raw = json.loads(cache.read_text())
                except Exception:
                    raw = None
        if raw is None:
            try:
                resp = httpx.get(SLEEPER_PLAYERS, timeout=90.0)
                resp.raise_for_status()
                raw = resp.json()
                if cache:
                    cache.write_text(json.dumps(raw))
            except Exception:
                return

        by_espn: dict[int, dict] = {}
        id_map: dict[str, int] = {}
        for sleeper_id, player in raw.items():
            espn_id = player.get("espn_id")
            if not espn_id:
                continue
            try:
                espn_id = int(espn_id)
            except (TypeError, ValueError):
                continue
            by_espn[espn_id] = player
            id_map[sleeper_id] = espn_id
        self._sleeper = by_espn
        self._sleeper_id_to_espn = id_map
        self._sleeper_at = time.time()

    def _load_rotowire(self) -> None:
        try:
            resp = httpx.get(ROTOWIRE_RSS, headers={"User-Agent": USER_AGENT}, timeout=20.0)
            resp.raise_for_status()
            body = resp.text
        except Exception:
            return
        items = []
        for chunk in re.findall(r"<item>(.*?)</item>", body, re.S):
            title = _tag(chunk, "title")
            desc = _tag(chunk, "description")
            if title:
                items.append((title, desc))
        self._rotowire = items
        self._rotowire_at = time.time()

    def _load_drops(self) -> None:
        try:
            resp = httpx.get(
                SLEEPER_TRENDING.format(kind="drop"),
                params={"lookback_hours": 72, "limit": 100},
                timeout=20.0,
            )
            resp.raise_for_status()
            self._drops = {r["player_id"]: r.get("count", 0) for r in resp.json()}
        except Exception:
            self._drops = {}

    # -- report building ---------------------------------------------------

    def report(self, player, news_items: list | None = None) -> RiskReport:
        """Assemble everything we know that could sink this player's season."""
        report = RiskReport(player_id=player.espn_id)

        # 1. ESPN's own injury designation.
        level = STATUS_SEVERITY.get(player.injury)
        if level:
            report.add(level, _short_status(player.injury),
                       f"ESPN lists him {player.injury.replace('_', ' ').lower()}")

        # 2. Sleeper's structured injury record.
        sleeper = self._sleeper.get(player.espn_id) or {}
        status = sleeper.get("injury_status")
        if status and status not in ("NA", "Healthy"):
            part = sleeper.get("injury_body_part")
            detail = f" ({part})" if part and part != "NA" else ""
            report.add(STATUS_SEVERITY.get(status, "medium"), status.upper()[:5],
                       f"Sleeper: {status}{detail}")
        if sleeper.get("status") in ("Inactive", "PUP", "IR", "Non Football Injury"):
            report.add("high", str(sleeper["status"])[:5],
                       f"Roster status: {sleeper['status']}")
        if sleeper.get("injury_notes"):
            report.add("medium", "", f"Note: {sleeper['injury_notes'][:180]}")
        if sleeper.get("practice_participation"):
            report.add("medium", "PRAC",
                       f"Practice: {sleeper['practice_participation']}")

        # 3. Fresh beat-reporter notes keyed to his name. RotoWire headlines
        #    are already `Player: update`, so the name check is the gate.
        for title, desc in self._rotowire:
            if not is_beat_note(title, player.name):
                continue
            level, hit = classify_about(f"{title} {desc}", player.name)
            if level != "none":
                report.add(level, flag_label(hit) if level == "high" else "",
                           f"RotoWire: {title}")

        # 4. ESPN news, but only genuine notes about him and only recent ones.
        #    Roundup articles stay on the board as reading material; they are
        #    not evidence of *this* player's health.
        for item in news_items or []:
            if not is_beat_note(item.headline, player.name):
                continue
            age = item.age_days
            if age is not None and age > MAX_NOTE_AGE_DAYS:
                continue
            level, hit = classify_about(f"{item.headline} {item.story}", player.name)
            if level in ("medium", "high"):
                report.add(level, flag_label(hit) if level == "high" else "",
                           f"{item.age_label}: {item.headline[:150]}")

        # 5. Are managers bailing on him?
        drops = self._drop_count(player.espn_id)
        if drops > 2000:
            report.add("medium", "DROPS", f"{drops:,} managers dropped him in 72h")

        return report

    def _drop_count(self, espn_id: int) -> int:
        for sleeper_id, mapped in self._sleeper_id_to_espn.items():
            if mapped == espn_id:
                return self._drops.get(sleeper_id, 0)
        return 0


# How close a red-flag phrase must sit to the player's name to count as being
# about him. Roundup articles mention six players and four injuries.
PROXIMITY_CHARS = 80
# Beyond this, an injury note is history rather than a draft-day risk.
MAX_NOTE_AGE_DAYS = 45


def classify(text: str) -> tuple[str, str]:
    """Grade a blob of news text. Returns (level, matched phrase)."""
    lowered = " ".join((text or "").lower().split())
    for phrase in HIGH_SIGNALS:
        if phrase in lowered:
            return "high", phrase
    for phrase in MEDIUM_SIGNALS:
        if phrase in lowered:
            return "medium", phrase
    for phrase in GOOD_SIGNALS:
        if phrase in lowered:
            return "low", phrase
    return "none", ""


def classify_about(text: str, name: str) -> tuple[str, str]:
    """Grade text, but only counting phrases that sit near the player's name.

    Without this, an article headlined "Do Not Draft list" that mentions one
    player's torn Achilles flags every other player it namechecks. Verified:
    naive whole-text matching wrongly reported season-ending ACL tears for
    A.J. Brown, Josh Jacobs and Kenneth Walker III.
    """
    lowered = " ".join((text or "").lower().split())
    surname = _surname(name)
    if not surname or surname not in lowered:
        return "none", ""

    windows = []
    for match in re.finditer(re.escape(surname), lowered):
        start = max(0, match.start() - PROXIMITY_CHARS)
        windows.append(lowered[start : match.end() + PROXIMITY_CHARS])
    near = " || ".join(windows)

    good = next((p for p in GOOD_SIGNALS if p in near), "")

    for phrase in HIGH_SIGNALS:
        if phrase not in near:
            continue
        # Good news downgrades a scare unless the injury is terminal.
        if good and phrase not in TERMINAL_SIGNALS:
            return "medium", phrase
        return "high", phrase
    for phrase in MEDIUM_SIGNALS:
        if phrase in near:
            return "low" if good else "medium", phrase
    if good:
        return "low", good
    return "none", ""


def is_beat_note(headline: str, name: str) -> bool:
    """True for a note written about this player, not a multi-player roundup.

    Beat notes read `Surname (knee) practiced Wednesday...` or `Surname: back
    at practice`. Roundups read `Fantasy football sleepers, busts and
    breakouts`. Only the former is evidence about this specific player.
    """
    surname = _surname(name)
    if not surname:
        return False
    head = " ".join((headline or "").lower().split())
    if not head:
        return False
    # Name at the very front, or the `Surname (bodypart)` convention anywhere.
    return (
        head.startswith(surname)
        or f"{surname} (" in head
        or head.startswith(f"{_first_initial(name)}. {surname}")
    )


def _surname(name: str) -> str:
    parts = [p for p in (name or "").split() if p]
    if len(parts) < 2:
        return ""
    # Skip generational suffixes so "Harold Fannin Jr." keys on "fannin".
    while len(parts) > 2 and re.sub(r"[^a-z]", "", parts[-1].lower()) in (
        "jr", "sr", "ii", "iii", "iv", "v"
    ):
        parts = parts[:-1]
    return re.sub(r"[^a-z]", "", parts[-1].lower())


def _first_initial(name: str) -> str:
    parts = (name or "").split()
    return parts[0][0].lower() if parts else ""


def _mentions(text: str, name: str) -> bool:
    """Does this text mention the player at all?"""
    surname = _surname(name)
    return bool(surname) and surname in " ".join((text or "").lower().split())


def _tag(chunk: str, name: str) -> str:
    match = re.search(
        rf"<{name}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{name}>", chunk, re.S
    )
    if not match:
        return ""
    return html.unescape(re.sub(r"<[^>]+>", " ", match.group(1))).strip()


def _short_status(status: str) -> str:
    return {
        "QUESTIONABLE": "QUES", "DOUBTFUL": "DOUBT", "OUT": "OUT",
        "INJURY_RESERVE": "IR", "SUSPENSION": "SUSP", "DAY_TO_DAY": "DTD",
        "PUP": "PUP",
    }.get(status, status[:5])
