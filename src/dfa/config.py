"""Configuration loading: TOML file, overridable by environment variables."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .models import LeagueSettings

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.toml"
DEFAULT_CACHE_DIR = PROJECT_ROOT / "cache"

# The season whose player pool and projections we draft against.
SEASON = int(os.environ.get("DFA_SEASON", "2026"))


@dataclass
class Config:
    season: int = SEASON
    league_id: str | None = None
    espn_s2: str | None = None
    swid: str | None = None
    league: LeagueSettings = field(default_factory=LeagueSettings)
    cache_dir: Path = DEFAULT_CACHE_DIR
    # How long cached player/news payloads stay fresh, in seconds.
    player_cache_ttl: int = 6 * 3600
    news_cache_ttl: int = 30 * 60
    poll_interval: float = 3.0
    host: str = "127.0.0.1"
    port: int = 8765

    @property
    def has_private_league_auth(self) -> bool:
        return bool(self.espn_s2 and self.swid)


def load_config(path: Path | None = None) -> Config:
    """Read config.toml if present, then let env vars win over it."""
    path = path or DEFAULT_CONFIG_PATH
    raw: dict = {}
    if path.exists():
        with path.open("rb") as fh:
            raw = tomllib.load(fh)

    league_raw = raw.get("league", {})
    starters = league_raw.get("starters")
    league = LeagueSettings(
        teams=int(league_raw.get("teams", 10)),
        rounds=int(league_raw.get("rounds", 16)),
        scoring=str(league_raw.get("scoring", "PPR")).upper(),
        snake=bool(league_raw.get("snake", True)),
        my_team_id=league_raw.get("my_team_id"),
        my_draft_slot=league_raw.get("my_draft_slot"),
    )
    if starters:
        league.starters = {k.upper(): int(v) for k, v in starters.items()}

    espn_raw = raw.get("espn", {})
    server_raw = raw.get("server", {})

    cfg = Config(
        season=int(raw.get("season", SEASON)),
        league_id=_env_or(espn_raw.get("league_id"), "DFA_LEAGUE_ID"),
        espn_s2=_env_or(espn_raw.get("espn_s2"), "DFA_ESPN_S2"),
        swid=_env_or(espn_raw.get("swid"), "DFA_SWID"),
        league=league,
        host=str(server_raw.get("host", "127.0.0.1")),
        port=int(server_raw.get("port", 8765)),
    )
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)

    # Normalise SWID to the {...} form ESPN expects.
    if cfg.swid and not cfg.swid.startswith("{"):
        cfg.swid = "{" + cfg.swid.strip("{}") + "}"
    return cfg


def _env_or(value, env_key: str):
    """Environment variables take precedence so secrets can stay out of the file."""
    env = os.environ.get(env_key)
    if env:
        return env
    return value
