"""The live draft session: holds state, and renders the dashboard payload."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .analysis.board import Board
from .analysis.flow import detect_runs, vona_by_position
from .analysis.recommend import Recommendation, recommend
from .analysis.roster import RosterState, evaluate_roster
from .config import Config
from .models import POSITION_ORDER, DraftState, LeagueSettings, Pick, Player
from .sources import espn_players, news as news_source
from .sources.risk import RiskFeed, RiskReport


@dataclass
class DraftSession:
    config: Config
    players: list[Player] = field(default_factory=list)
    board: Board | None = None
    state: DraftState | None = None
    team_names: dict[int, str] = field(default_factory=dict)
    # Draft slot -> ESPN team id. Empty means slot and team id are the same
    # number, which is true for mocks and simulations.
    slot_to_team: dict[int, int] = field(default_factory=dict)
    status: str = "idle"        # idle | watching | error | complete
    status_detail: str = ""
    news: dict[int, list] = field(default_factory=dict)
    # Player ids from the most recent ranking, so the news worker doesn't
    # have to recompute the board just to know who to fetch.
    last_rec_ids: list[int] = field(default_factory=list)
    # Injury / red-flag intelligence, kept warm by a background worker.
    risk: dict[int, RiskReport] = field(default_factory=dict)
    risk_feed: RiskFeed | None = None
    risk_updated: float = 0.0
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # -- setup -------------------------------------------------------------

    def load_players(self, force: bool = False) -> list[Player]:
        self.players = espn_players.fetch_players(
            season=self.config.season,
            scoring=self.config.league.scoring,
            cache_dir=self.config.cache_dir,
            ttl=self.config.player_cache_ttl,
            force=force,
        )
        self._rebuild_board()
        return self.players

    def _rebuild_board(self) -> None:
        settings = self.state.settings if self.state else self.config.league
        self.board = Board.build(self.players, settings)

    def ensure_state(self, settings: LeagueSettings | None = None) -> DraftState:
        with self._lock:
            if self.state is None:
                self.state = DraftState(settings=settings or self.config.league)
            elif settings is not None:
                self.state.settings = settings
            return self.state

    def apply_settings(self, settings: LeagueSettings) -> None:
        """Adopt league settings detected from ESPN, keeping manual overrides."""
        with self._lock:
            configured = self.config.league
            # A slot the user pinned in config.toml wins over auto-detection.
            if configured.my_draft_slot and not settings.my_draft_slot:
                settings.my_draft_slot = configured.my_draft_slot
            if configured.my_team_id and not settings.my_team_id:
                settings.my_team_id = configured.my_team_id
            self.ensure_state(settings)
            self._rebuild_board()

    # -- draft mutation ----------------------------------------------------

    def record_pick(self, player_id: int, team_id: int | None = None) -> Pick | None:
        """Append a pick whose overall number we have to infer ourselves.

        Used by the mock-draft watcher, which sees players but not always the
        pick metadata.
        """
        with self._lock:
            state = self.ensure_state()
            if player_id in state.drafted_ids:
                return None
            ranked = self.board.get(player_id) if self.board else None
            overall = state.next_overall
            rnd, in_round = state.round_and_pick(overall)
            pick = Pick(
                overall=overall,
                round=rnd,
                pick_in_round=in_round,
                team_id=team_id if team_id is not None else state.slot_on_the_clock(overall),
                player_id=player_id,
                player_name=ranked.player.name if ranked else str(player_id),
                pos=ranked.player.pos if ranked else "",
            )
            state.picks.append(pick)
            return pick

    def undo_pick(self) -> Pick | None:
        with self._lock:
            state = self.ensure_state()
            return state.picks.pop() if state.picks else None

    def label_picks(self) -> None:
        """Backfill name/position on picks that arrived as bare player ids."""
        if not self.board:
            return
        for pick in (self.state.picks if self.state else []):
            if not pick.pos or not pick.player_name:
                ranked = self.board.get(pick.player_id)
                if ranked:
                    pick.player_name = ranked.player.name
                    pick.pos = ranked.player.pos

    # -- analysis ----------------------------------------------------------

    def team_name_for_slot(self, slot: int) -> str:
        team_id = self.slot_to_team.get(slot, slot)
        return self.team_names.get(team_id, "")

    def roster_state(self) -> RosterState:
        state = self.ensure_state()
        my_picks = state.my_roster()
        total = state.settings.teams * state.settings.rounds
        picks_left = max(0, (total - state.next_overall) // max(1, state.settings.teams))
        proj_by_id, bye_by_id = {}, {}
        if self.board:
            for rp in self.board.ranked:
                proj_by_id[rp.id] = rp.player.proj
                if rp.player.bye:
                    bye_by_id[rp.id] = rp.player.bye
        return evaluate_roster(
            my_picks, state.settings, picks_left, proj_by_id, bye_by_id
        )

    def recommendations(self, limit: int = 30) -> list[Recommendation]:
        state = self.ensure_state()
        if not self.board:
            return []
        return recommend(self.board, state, self.roster_state(), limit=limit)

    def refresh_news(self, recs: list[Recommendation], top_n: int = 30) -> None:
        """Pull news for the top N available players."""
        self.refresh_news_for([r.player.espn_id for r in recs[:top_n]])

    def refresh_risk_for(self, player_ids: list[int]) -> None:
        """Rebuild injury/red-flag reports for these players.

        Called from a background worker so the data is already sitting there
        when you land on the clock - there is no time to go fetch it then.
        """
        if not player_ids or not self.board:
            return
        if self.risk_feed is None:
            self.risk_feed = RiskFeed(cache_dir=self.config.cache_dir)
        self.risk_feed.refresh()

        fresh: dict[int, RiskReport] = {}
        for pid in player_ids:
            ranked = self.board.get(pid)
            if not ranked:
                continue
            fresh[pid] = self.risk_feed.report(ranked.player, self.news.get(pid))
        with self._lock:
            self.risk.update(fresh)
            self.risk_updated = time.time()

    def refresh_news_for(self, player_ids: list[int]) -> None:
        if not player_ids:
            return
        fetched = news_source.fetch_news(
            player_ids,
            cache_dir=self.config.cache_dir,
            ttl=self.config.news_cache_ttl,
        )
        # Merge rather than replace so players who drop out of the top 30
        # keep their news if the board shifts back.
        self.news.update(fetched)

    # -- dashboard payload -------------------------------------------------

    def dashboard(self, top_n: int = 30) -> dict:
        with self._lock:
            state = self.ensure_state()
            self.label_picks()
            recs = self.recommendations(limit=top_n)
            self.last_rec_ids = [r.player.espn_id for r in recs]
            roster = self.roster_state()
            runs = detect_runs(state)
            my_next = state.my_next_picks(count=3)
            vona = vona_by_position(self.board, state, my_next[1] if len(my_next) > 1 else None)
            rnd, in_round = state.round_and_pick()

            return {
                "status": self.status,
                "status_detail": self.status_detail,
                "on_clock": {
                    "overall": state.next_overall,
                    "round": rnd,
                    "pick_in_round": in_round,
                    "slot": state.slot_on_the_clock(),
                    "team_name": self.team_name_for_slot(state.slot_on_the_clock()),
                    "is_me": state.is_my_pick(),
                    "my_slot": state.settings.my_draft_slot,
                    "my_next_picks": my_next,
                },
                "league": {
                    "teams": state.settings.teams,
                    "rounds": state.settings.rounds,
                    "scoring": state.settings.scoring,
                    "starters": state.settings.starters,
                },
                "roster": {
                    "slots": roster.slot_summary(),
                    "counts": roster.counts,
                    "needs": [
                        {"pos": pos, "score": roster.needs.get(pos, 0.0)}
                        for pos in POSITION_ORDER
                        if roster.needs.get(pos, 0.0) > 0.02
                    ],
                    "players": [
                        {"name": p.player_name, "pos": p.pos, "round": p.round}
                        for p in state.my_roster()
                    ],
                },
                "top3": self._top_three(recs, roster),
                "runs": [{"pos": r.pos, "label": r.label} for r in runs],
                "vona": vona,
                "available": [self._rec_payload(r) for r in recs],
                "recent_picks": [
                    {
                        "overall": p.overall,
                        "round": p.round,
                        "pick": p.pick_in_round,
                        "name": p.player_name,
                        "pos": p.pos,
                        "team": self.team_names.get(p.team_id, f"Team {p.team_id}"),
                        "is_me": p.team_id == state.settings.my_team_id,
                    }
                    for p in reversed(state.picks[-12:])
                ],
                "counts": {
                    "picked": len(state.picks),
                    "total": state.settings.teams * state.settings.rounds,
                },
                "pos_taken": self._positions_taken(),
            }

    def _rec_payload(self, rec: Recommendation) -> dict:
        player = rec.player
        items = _prefer_player_specific(self.news.get(player.espn_id, []), player.name)
        risk = self.risk.get(player.espn_id)
        return {
            "risk_level": risk.level if risk else "none",
            "risk_flags": risk.flags[:3] if risk else [],
            "risk_notes": risk.notes[:4] if risk else [],
            "id": player.espn_id,
            "name": player.name,
            "short_name": player.short_name,
            "pos": player.pos,
            "team": player.pro_team,
            "bye": player.bye,
            "proj": round(player.proj, 1),
            "adp": player.adp,
            "tier": rec.tier,
            "pos_rank": rec.pos_rank,
            "vor": rec.vor,
            "vona": rec.vona,
            "score": rec.score,
            "survival": rec.survival,
            "bargain": rec.bargain,
            "is_value": rec.is_value,
            "will_last": rec.will_likely_last,
            "injury": player.injury,
            "injury_flag": player.is_injury_risk,
            "reasons": rec.reasons,
            "outlook": player.outlook,
            "news": [
                {
                    "headline": n.headline,
                    "story": n.story[:400],
                    "age": n.age_label,
                }
                for n in items[:2]
            ],
        }

    def _top_three(self, recs: list[Recommendation], roster: RosterState) -> list[dict]:
        """Three distinct options to choose between, each with a rationale.

        Deliberately three *different* cases rather than the top three by
        score, which often means three near-identical players: the best pick
        overall, the biggest bargain, and the best answer to the position the
        roster most needs. The tool suggests; you decide.
        """
        if not recs:
            return []

        chosen: list[tuple[str, Recommendation]] = [("BEST OVERALL", recs[0])]
        used = {recs[0].player.espn_id}
        window = recs[:14]

        # Biggest faller past ADP that we haven't already surfaced.
        bargains = [r for r in window if r.player.espn_id not in used and r.bargain >= 6]
        if bargains:
            best = max(bargains, key=lambda r: r.bargain)
            chosen.append((f"BEST VALUE · {int(best.bargain)} past ADP", best))
            used.add(best.player.espn_id)

        # Best available at the position the roster most needs.
        need_pos = max(roster.needs.items(), key=lambda kv: kv[1], default=(None, 0))[0]
        if need_pos:
            fits = [
                r for r in window
                if r.player.pos == need_pos and r.player.espn_id not in used
            ]
            if fits:
                chosen.append((f"FILLS {need_pos}", fits[0]))
                used.add(fits[0].player.espn_id)

        # Top up from the board if the special cases didn't yield three.
        for rec in window:
            if len(chosen) >= 3:
                break
            if rec.player.espn_id not in used:
                chosen.append(("STRONG ALTERNATIVE", rec))
                used.add(rec.player.espn_id)

        out = []
        for tag, rec in chosen[:3]:
            payload = self._rec_payload(rec)
            payload["tag"] = tag
            out.append(payload)
        return out

    def _positions_taken(self) -> dict[str, int]:
        state = self.ensure_state()
        taken: dict[str, int] = {}
        for pick in state.picks:
            if pick.pos:
                taken[pick.pos] = taken.get(pick.pos, 0) + 1
        return taken


def _prefer_player_specific(items: list, name: str) -> list:
    """Put genuine player notes ahead of round-up articles.

    ESPN's per-player feed mixes real beat-reporter notes with generic season
    previews that merely mention the player somewhere; the notes are what
    actually matter on the clock.
    """
    if not items or not name:
        return items
    surname = name.split()[-1].lower()

    def specific(item) -> int:
        haystack = f"{item.headline} {item.story}".lower()
        return 0 if surname in haystack else 1

    return sorted(items, key=specific)
