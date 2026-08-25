"""Unit tests for draft mechanics and roster logic - no network required."""

from __future__ import annotations

import unittest

from dfa.analysis.board import Board, replacement_levels
from dfa.analysis.flow import detect_runs, survival_probability
from dfa.analysis.recommend import roster_fit
from dfa.analysis.roster import evaluate_roster
from dfa.models import DraftState, LeagueSettings, Pick, Player


def mkplayer(pid, name, pos, proj, adp=None):
    return Player(espn_id=pid, name=name, pos=pos, pro_team="FA", proj=proj, adp=adp)


def pool():
    players = []
    pid = 1
    for pos, count, top in (("QB", 24, 380), ("RB", 60, 360), ("WR", 70, 350), ("TE", 24, 240)):
        for i in range(count):
            players.append(mkplayer(pid, f"{pos}{i+1}", pos, top - i * 6, adp=pid))
            pid += 1
    for pos in ("K", "DST"):
        for i in range(20):
            players.append(mkplayer(pid, f"{pos}{i+1}", pos, 150 - i * 2, adp=200 + pid))
            pid += 1
    return players


class TestSnakeOrder(unittest.TestCase):
    def setUp(self):
        self.settings = LeagueSettings(teams=10, rounds=16, my_draft_slot=4)
        self.state = DraftState(settings=self.settings)

    def test_round_one_is_forward(self):
        self.assertEqual(self.state.slot_on_the_clock(1), 1)
        self.assertEqual(self.state.slot_on_the_clock(10), 10)

    def test_round_two_reverses(self):
        self.assertEqual(self.state.slot_on_the_clock(11), 10)
        self.assertEqual(self.state.slot_on_the_clock(20), 1)

    def test_round_three_forward_again(self):
        self.assertEqual(self.state.slot_on_the_clock(21), 1)

    def test_round_and_pick(self):
        self.assertEqual(self.state.round_and_pick(1), (1, 1))
        self.assertEqual(self.state.round_and_pick(11), (2, 1))
        self.assertEqual(self.state.round_and_pick(25), (3, 5))

    def test_my_next_picks_alternate_correctly(self):
        # Slot 4 in a 10-team snake: 4, 17, 24, 37, 44...
        self.assertEqual(self.state.my_next_picks(4), [4, 17, 24, 37])

    def test_non_snake_is_linear(self):
        settings = LeagueSettings(teams=10, rounds=16, snake=False)
        state = DraftState(settings=settings)
        self.assertEqual(state.slot_on_the_clock(11), 1)
        self.assertEqual(state.slot_on_the_clock(20), 10)


def pool_without_adp():
    """Same pool, but no ADP - forces the slot-count fallback."""
    players = pool()
    for p in players:
        p.adp = None
    return players


class TestReplacement(unittest.TestCase):
    """Slot-count fallback, used when ADP is unavailable."""

    def test_flex_pushes_replacement_deeper(self):
        players = pool_without_adp()
        with_flex = LeagueSettings(teams=10, starters={"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1})
        without = LeagueSettings(teams=10, starters={"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 0})
        a = replacement_levels(players, with_flex)
        b = replacement_levels(players, without)
        # A deeper replacement pool means a lower replacement score.
        self.assertLess(a["RB"], b["RB"])
        self.assertLess(a["WR"], b["WR"])

    def test_more_teams_lowers_replacement(self):
        players = pool_without_adp()
        ten = replacement_levels(players, LeagueSettings(teams=10))
        twelve = replacement_levels(players, LeagueSettings(teams=12))
        self.assertLess(twelve["RB"], ten["RB"])

    def test_slot_baseline_counts(self):
        from dfa.analysis.board import _slot_baseline_count

        settings = LeagueSettings(teams=10, starters={"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1})
        self.assertEqual(_slot_baseline_count("QB", settings), 10)
        # 2 starters + 45% of one flex slot, over 10 teams.
        self.assertEqual(_slot_baseline_count("RB", settings), 24)


class TestEmpiricalBaseline(unittest.TestCase):
    """ADP-derived baselines: what the market actually drafts."""

    def setUp(self):
        from dfa.analysis.board import _adp_baseline_counts

        self.counts = _adp_baseline_counts
        self.settings = LeagueSettings(
            teams=10, starters={"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1}
        )

    def _market(self, per_pos: dict[str, int]) -> list[Player]:
        """Build a pool where `per_pos` players of each position go by pick 100."""
        players, adp = [], 1
        for pos, count in per_pos.items():
            for i in range(count):
                players.append(mkplayer(len(players) + 1, f"{pos}{i}", pos, 200 - i, adp=adp))
                adp += 1
        # Pad well past the cutoff so totals are realistic.
        for i in range(120):
            players.append(mkplayer(len(players) + 1, f"X{i}", "WR", 50, adp=200 + i))
        return players

    def test_counts_what_the_market_drafts(self):
        got = self.counts(self._market({"QB": 12, "RB": 27, "WR": 35, "TE": 9}), self.settings)
        self.assertEqual(got["QB"], 12)
        self.assertEqual(got["RB"], 27)

    def test_never_shallower_than_dedicated_starters(self):
        # Only 4 TEs drafted early, but 10 teams must start one each.
        got = self.counts(self._market({"QB": 12, "RB": 27, "WR": 35, "TE": 4}), self.settings)
        self.assertEqual(got["TE"], 10)

    def test_falls_back_when_adp_is_missing(self):
        self.assertEqual(self.counts(pool_without_adp(), self.settings), {})

    def test_deeper_market_lowers_the_baseline_score(self):
        """More of a position drafted early => weaker replacement => higher VOR."""
        shallow = replacement_levels(
            self._market({"QB": 12, "RB": 27, "WR": 20, "TE": 9}), self.settings)
        deep = replacement_levels(
            self._market({"QB": 12, "RB": 27, "WR": 40, "TE": 9}), self.settings)
        self.assertLess(deep["WR"], shallow["WR"])


class TestRoster(unittest.TestCase):
    def setUp(self):
        self.settings = LeagueSettings(teams=10, rounds=16)

    def _picks(self, *specs):
        return [
            Pick(overall=i + 1, round=i + 1, pick_in_round=1, team_id=1,
                 player_id=i + 1, pos=pos, player_name=f"p{i}")
            for i, pos in enumerate(specs)
        ]

    def test_empty_roster_needs_everything(self):
        state = evaluate_roster([], self.settings, picks_left=15)
        self.assertGreater(state.needs["RB"], 0.6)
        self.assertGreater(state.needs["WR"], 0.6)
        self.assertEqual(state.bench, 0)

    def test_spare_rb_fills_flex(self):
        picks = self._picks("RB", "RB", "RB")
        state = evaluate_roster(picks, self.settings, picks_left=12)
        self.assertEqual(state.filled["RB"], 2)
        self.assertEqual(state.filled["FLEX"], 1)
        self.assertEqual(state.open_slots["FLEX"], 0)

    def test_extra_players_go_to_bench(self):
        picks = self._picks("RB", "RB", "RB", "RB", "WR", "WR")
        state = evaluate_roster(picks, self.settings, picks_left=10)
        self.assertEqual(state.bench, 1)  # 4 RB: 2 start, 1 flex, 1 bench

    def test_kicker_capped_at_one(self):
        state = evaluate_roster(self._picks("K"), self.settings, picks_left=10)
        self.assertTrue(state.is_capped("K"))
        self.assertFalse(state.is_capped("DST"))

    def test_kicker_not_urgent_early_but_is_late(self):
        early = evaluate_roster([], self.settings, picks_left=12)
        late = evaluate_roster([], self.settings, picks_left=1)
        self.assertLess(early.needs["K"], 0.1)
        self.assertGreater(late.needs["K"], 0.5)

    def test_best_player_takes_the_starting_slot(self):
        picks = self._picks("RB", "RB")
        proj = {1: 100.0, 2: 300.0}
        state = evaluate_roster(picks, self.settings, picks_left=12, proj_by_id=proj)
        self.assertEqual(sorted(state.starter_proj["RB"], reverse=True), [300.0, 100.0])


class TestByeClash(unittest.TestCase):
    def setUp(self):
        self.settings = LeagueSettings(teams=10, rounds=16)

    def _roster(self, specs):
        """specs: list of (pos, bye)."""
        picks, byes = [], {}
        for i, (pos, bye) in enumerate(specs):
            picks.append(Pick(overall=i + 1, round=i + 1, pick_in_round=1, team_id=1,
                              player_id=100 + i, pos=pos, player_name=f"p{i}"))
            byes[100 + i] = bye
        return evaluate_roster(picks, self.settings, picks_left=10, bye_by_id=byes)

    def test_detects_same_position_same_bye(self):
        state = self._roster([("RB", 7), ("WR", 9)])
        self.assertEqual(state.bye_clash("RB", 7), 1)

    def test_different_position_is_not_a_clash(self):
        state = self._roster([("RB", 7)])
        self.assertEqual(state.bye_clash("WR", 7), 0)

    def test_different_week_is_not_a_clash(self):
        state = self._roster([("RB", 7)])
        self.assertEqual(state.bye_clash("RB", 9), 0)

    def test_counts_multiple_clashes(self):
        state = self._roster([("RB", 7), ("RB", 7)])
        self.assertEqual(state.bye_clash("RB", 7), 2)

    def test_unknown_bye_never_clashes(self):
        state = self._roster([("RB", 7)])
        self.assertEqual(state.bye_clash("RB", None), 0)


class TestRosterFit(unittest.TestCase):
    """The fix for drafting five defenses and three tight ends."""

    def setUp(self):
        self.settings = LeagueSettings(teams=10, rounds=16)
        self.board = Board.build(pool(), self.settings)

    def _fit(self, pos, proj, held, rounds_left=10):
        picks = [
            Pick(overall=i + 1, round=i + 1, pick_in_round=1, team_id=1,
                 player_id=1000 + i, pos=p, player_name="x")
            for i, p in enumerate(held)
        ]
        proj_by_id = {1000 + i: 200.0 for i in range(len(held))}
        roster = evaluate_roster(picks, self.settings, picks_left=rounds_left,
                                 proj_by_id=proj_by_id)
        rp = Board.build([mkplayer(9999, "X", pos, proj)], self.settings).ranked[0]
        return roster_fit(rp, roster, rounds_left)

    def test_open_slot_gets_full_value(self):
        self.assertEqual(self._fit("RB", 300, []), 1.0)

    def test_bench_duplicate_is_discounted(self):
        # QB slot filled and QB can't flex, so a second QB is bench only.
        fit = self._fit("QB", 100, ["QB"])
        self.assertEqual(fit, 0.30)

    def test_upgrade_over_worst_starter_beats_bench(self):
        fit = self._fit("QB", 400, ["QB"])  # better than the 200-point incumbent
        self.assertEqual(fit, 0.75)

    def test_second_kicker_is_worthless(self):
        self.assertEqual(self._fit("K", 150, ["K"], rounds_left=1), 0.0)

    def test_second_defense_is_worthless(self):
        self.assertEqual(self._fit("DST", 150, ["DST"], rounds_left=1), 0.0)

    def test_kicker_suppressed_early(self):
        self.assertLess(self._fit("K", 150, [], rounds_left=10), 0.1)

    def test_kicker_fine_at_the_end(self):
        self.assertEqual(self._fit("K", 150, [], rounds_left=1), 1.0)


class TestSurvival(unittest.TestCase):
    def test_far_past_adp_is_likely_gone(self):
        self.assertLess(survival_probability(10, 40), 0.05)

    def test_well_before_adp_is_likely_there(self):
        self.assertGreater(survival_probability(80, 40), 0.95)

    def test_at_adp_is_a_coin_flip(self):
        self.assertAlmostEqual(survival_probability(40, 40), 0.5, places=2)

    def test_monotonic_in_target_pick(self):
        probs = [survival_probability(50, t) for t in (20, 40, 60, 80)]
        self.assertEqual(probs, sorted(probs, reverse=True))

    def test_unknown_adp_defaults_high(self):
        self.assertGreater(survival_probability(None, 40), 0.8)


class TestRuns(unittest.TestCase):
    def _state(self, positions):
        settings = LeagueSettings(teams=10)
        state = DraftState(settings=settings)
        for i, pos in enumerate(positions):
            state.picks.append(
                Pick(overall=i + 1, round=1, pick_in_round=i + 1,
                     team_id=1, player_id=i + 1, pos=pos)
            )
        return state

    def test_detects_a_run(self):
        runs = detect_runs(self._state(["RB"] * 5 + ["WR", "TE"]))
        self.assertTrue(runs)
        self.assertEqual(runs[0].pos, "RB")
        self.assertEqual(runs[0].count, 5)

    def test_no_run_when_balanced(self):
        self.assertFalse(detect_runs(self._state(["RB", "WR", "TE", "QB", "RB", "WR", "TE"])))

    def test_needs_a_full_window(self):
        self.assertFalse(detect_runs(self._state(["RB", "RB", "RB"])))

    def test_uses_only_the_recent_window(self):
        # Old RB run, recent picks balanced -> no alert.
        runs = detect_runs(self._state(["RB"] * 6 + ["WR", "TE", "QB", "WR", "TE", "QB", "WR"]))
        self.assertFalse(runs)


class TestTiers(unittest.TestCase):
    def test_tiers_are_monotonic_down_the_board(self):
        board = Board.build(pool(), LeagueSettings(teams=10))
        for pos in ("RB", "WR", "TE", "QB"):
            group = [r for r in board.ranked if r.player.pos == pos]
            group.sort(key=lambda r: r.player.proj, reverse=True)
            tiers = [r.tier for r in group]
            self.assertEqual(tiers, sorted(tiers), f"{pos} tiers not monotonic")

    def test_streaming_positions_sort_below_starters(self):
        board = Board.build(pool(), LeagueSettings(teams=10))
        top20 = board.ranked[:20]
        self.assertFalse(any(r.player.pos in ("K", "DST") for r in top20))


class TestDraftState(unittest.TestCase):
    def test_drafted_ids_and_next_overall(self):
        state = DraftState(settings=LeagueSettings(teams=10))
        state.picks.append(Pick(overall=1, round=1, pick_in_round=1, team_id=1, player_id=42))
        self.assertEqual(state.drafted_ids, {42})
        self.assertEqual(state.next_overall, 2)

    def test_is_my_pick(self):
        settings = LeagueSettings(teams=10, my_draft_slot=1)
        state = DraftState(settings=settings)
        self.assertTrue(state.is_my_pick())
        state.picks.append(Pick(overall=1, round=1, pick_in_round=1, team_id=1, player_id=1))
        self.assertFalse(state.is_my_pick())


class TestEspnLeagueParsing(unittest.TestCase):
    """Parsing against the real payload shape of a league before its draft."""

    def setUp(self):
        from dfa.watch.espn_league import EspnLeagueWatcher

        self.W = EspnLeagueWatcher
        self.data = {
            "settings": {
                "size": 10,
                "rosterSettings": {
                    "lineupSlotCounts": {
                        "0": 1, "2": 2, "4": 2, "6": 1, "16": 1, "17": 1,
                        "20": 7, "21": 3, "23": 1,
                    }
                },
                "draftSettings": {"type": "SNAKE", "pickOrder": [6, 2, 5, 8, 3, 7, 9, 1, 10, 4]},
                "scoringSettings": {"scoringItems": [{"statId": 53, "points": 1.0}]},
            },
            "draftDetail": {"drafted": False, "inProgress": False, "picks": []},
            "teams": [{"id": 3, "name": "Test Team", "owners": ["{ABC}"]}],
        }

    def _placeholder_picks(self, n=160):
        return [
            {"playerId": -1, "roundId": i // 10 + 1, "roundPickNumber": i % 10 + 1,
             "overallPickNumber": i + 1, "teamId": (i % 10) + 1}
            for i in range(n)
        ]

    def test_placeholder_picks_are_ignored(self):
        detail = {"picks": self._placeholder_picks()}
        self.assertEqual(self.W._parse_picks(detail, {}), [])

    def test_real_picks_survive_alongside_placeholders(self):
        picks = self._placeholder_picks()
        picks[0] = {"playerId": 4426502, "roundId": 1, "roundPickNumber": 1,
                    "overallPickNumber": 1, "teamId": 6}
        parsed = self.W._parse_picks({"picks": picks}, {4426502: "RB"})
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].player_id, 4426502)
        self.assertEqual(parsed[0].pos, "RB")

    def test_ir_slots_do_not_add_draft_rounds(self):
        # 9 starters + 7 bench = 16 rounds; the 3 IR slots are not drafted.
        settings = self.W._parse_settings(self.W, self.data)
        self.assertEqual(settings.rounds, 16)
        self.assertEqual(settings.teams, 10)

    def test_starters_parsed_with_flex(self):
        settings = self.W._parse_settings(self.W, self.data)
        self.assertEqual(
            settings.starters,
            {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "DST": 1, "K": 1, "FLEX": 1},
        )
        self.assertEqual(settings.bench_size, 7)

    def test_ppr_detected_from_reception_scoring(self):
        self.assertEqual(self.W._detect_scoring(self.data["settings"]), "PPR")

    def test_half_ppr_detected(self):
        raw = {"scoringSettings": {"scoringItems": [{"statId": 53, "points": 0.5}]}}
        self.assertEqual(self.W._detect_scoring(raw), "HALF")

    def test_standard_detected(self):
        raw = {"scoringSettings": {"scoringItems": [{"statId": 53, "points": 0.0}]}}
        self.assertEqual(self.W._detect_scoring(raw), "STANDARD")

    def test_slot_to_team_from_pick_order(self):
        mapping = self.W._slot_to_team(self.data)
        # pickOrder [6,2,5,8,3,7,9,1,10,4]: slot 5 is team 3.
        self.assertEqual(mapping[1], 6)
        self.assertEqual(mapping[5], 3)

    def test_draft_slot_found_from_pick_order(self):
        self.assertEqual(self.W._draft_slot(self.data, my_team_id=3), 5)

    def test_draft_slot_ignores_placeholder_round_one(self):
        data = dict(self.data)
        data["settings"] = dict(self.data["settings"])
        data["settings"]["draftSettings"] = {"type": "SNAKE"}  # no pickOrder
        data["draftDetail"] = {"picks": self._placeholder_picks()}
        # Every placeholder claims a team; none should be trusted.
        self.assertIsNone(self.W._draft_slot(data, my_team_id=3))


class TestRiskClassifier(unittest.TestCase):
    """Regression guards built from real false positives seen on live data."""

    def setUp(self):
        from dfa.sources import risk

        self.classify = risk.classify_about
        self.is_beat_note = risk.is_beat_note

    # -- the false positives that actually happened ------------------------

    def test_retired_other_player_does_not_flag_retirement(self):
        text = ("Barkley spoke this offseason with retired running back Todd "
                "Gurley, who played for the Rams.")
        level, _ = self.classify(text, "Saquon Barkley")
        self.assertNotEqual(level, "high")

    def test_cleared_outranks_a_pup_mention(self):
        text = ("Head coach Jeff Hafley said Achane (shoulder) has been cleared "
                "for the start of training camp after time on the PUP list.")
        level, _ = self.classify(text, "De'Von Achane")
        self.assertNotEqual(level, "high")

    def test_questionable_off_field_decisions_is_not_an_injury(self):
        text = ("Nacua has made some questionable off-field decisions at times, "
                "but his on-field effectiveness isn't in question.")
        level, _ = self.classify(text, "Puka Nacua")
        self.assertNotEqual(level, "medium")

    def test_roundup_article_is_not_a_beat_note(self):
        for headline in (
            "Fantasy football sleepers, busts and breakouts for 2026",
            "Fantasy Football 'Do Not Draft' list: Giants' young trio among players being overvalued",
            "Why Field Yates gives Nacua the edge over Chase in fantasy",
        ):
            self.assertFalse(self.is_beat_note(headline, "A.J. Brown"), headline)

    def test_roundup_is_rejected_before_classification(self):
        """The real defence against roundups is the beat-note gate.

        Proximity cannot save us here - a roundup interleaves player names and
        injuries within a few words of each other - so the pipeline must never
        classify this text at all.
        """
        headline = ("Fantasy Football 'Do Not Draft' list: one back is coming off "
                    "a torn Achilles while Jacobs remains fine at his cost")
        self.assertFalse(self.is_beat_note(headline, "Josh Jacobs"))

    def test_distant_injury_mention_does_not_flag(self):
        text = ("Jacobs looked sharp in camp. " + "Filler copy. " * 12 +
                "Separately, a teammate suffered a torn Achilles.")
        level, _ = self.classify(text, "Josh Jacobs")
        self.assertNotEqual(level, "high")

    # -- true positives it must still catch --------------------------------

    def test_real_suspension_risk_is_caught(self):
        text = ("There is still some risk of Nacua facing a suspension due to an "
                "ongoing civil lawsuit and NFL review of an incident.")
        level, phrase = self.classify(text, "Puka Nacua")
        self.assertEqual(level, "high")
        self.assertEqual(phrase, "facing a suspension")

    def test_terminal_injury_survives_good_news(self):
        text = "Smith tore his ACL but has been cleared to begin rehab."
        level, _ = self.classify(text, "John Smith")
        self.assertEqual(level, "high")

    def test_body_part_note_is_a_beat_note(self):
        self.assertTrue(self.is_beat_note(
            "Rice (knee) is participating in 11-on-11 drills Wednesday.", "Rashee Rice"))

    def test_surname_first_headline_is_a_beat_note(self):
        self.assertTrue(self.is_beat_note(
            "McMillan participated in Thursday's practice.", "Tetairoa McMillan"))

    def test_suffix_names_resolve_to_the_right_surname(self):
        self.assertTrue(self.is_beat_note(
            "Fannin (ankle) did not practice.", "Harold Fannin Jr."))

    def test_flag_labels_are_readable(self):
        from dfa.sources.risk import flag_label

        self.assertEqual(flag_label("facing a suspension"), "SUSP RISK")
        self.assertEqual(flag_label("placed on injured reserve"), "IR")
        self.assertEqual(flag_label("tore his acl"), "TORN ACL")


class TestDraftProtocol(unittest.TestCase):
    """The ESPN draft-room wire protocol, captured from a live practice draft."""

    def setUp(self):
        from dfa.watch.espn_mock import parse_draft_message

        self.parse = parse_draft_message

    def test_selected_is_a_pick(self):
        msg = self.parse("SELECTED 10 4429795 2")
        self.assertEqual(msg, {"kind": "pick", "team_id": 10,
                               "player_id": 4429795, "slot_id": 2})

    def test_selecting_is_on_the_clock(self):
        msg = self.parse("SELECTING 5 30000")
        self.assertEqual(msg["kind"], "on_clock")
        self.assertEqual(msg["team_id"], 5)

    def test_token_reveals_our_team_id(self):
        # SWID here is a placeholder - the real one is a live session credential.
        msg = self.parse("TOKEN 1:900000001:3:{00000000-1111-2222-3333-444444444444}:-1776106168")
        self.assertEqual(msg["kind"], "token")
        self.assertEqual(msg["team_id"], 3)
        self.assertEqual(msg["league_id"], "900000001")

    def test_state_and_clock(self):
        self.assertEqual(self.parse("STATE 1"), {"kind": "state", "value": 1})
        self.assertEqual(self.parse("CLOCK 0 24973")["millis"], 24973)

    def test_autosuggest(self):
        self.assertEqual(self.parse("AUTOSUGGEST 4430807")["player_id"], 4430807)

    def test_noise_is_ignored(self):
        for line in ("PONG PING%201785996361220", "", "   ",
                     "INIT AAAAAQAAAAEqhMZOAAAAAw", '{"json":true}'):
            self.assertIsNone(self.parse(line), line)

    def test_malformed_lines_do_not_raise(self):
        for line in ("SELECTED", "SELECTED abc def", "SELECTING", "TOKEN", "TOKEN 1:2"):
            self.assertIsNone(self.parse(line), line)

    def test_snake_order_from_captured_sequence(self):
        """Round 2 must mirror round 1 - a real regression guard on ordering."""
        capture = [
            "SELECTED 10 1 2", "SELECTED 1 2 4", "SELECTED 9 3 2", "SELECTED 5 4 2",
            "SELECTED 3 5 2", "SELECTED 6 6 4", "SELECTED 4 7 4", "SELECTED 8 8 2",
            "SELECTED 2 9 4", "SELECTED 7 10 2",
            "SELECTED 7 11 3", "SELECTED 2 12 5", "SELECTED 8 13 4", "SELECTED 4 14 5",
            "SELECTED 6 15 2", "SELECTED 3 16 4", "SELECTED 5 17 6", "SELECTED 9 18 3",
            "SELECTED 1 19 2", "SELECTED 10 20 3",
        ]
        teams = [self.parse(line)["team_id"] for line in capture]
        self.assertEqual(teams[:10], list(reversed(teams[10:20])))


class TestDomFallbackSafety(unittest.TestCase):
    """DOM scanning produced wrong picks in two live drafts; it stays off."""

    def test_no_whole_page_fallback(self):
        import inspect

        from dfa.watch.espn_mock import MockDraftWatcher

        src = inspect.getsource(MockDraftWatcher._pick_region_text)
        self.assertNotIn('inner_text("body")', src)

    def test_poll_dom_is_a_noop_without_explicit_selectors(self):
        from dfa.watch.espn_mock import MockDraftWatcher

        emitted = []
        w = MockDraftWatcher.__new__(MockDraftWatcher)
        w.page = object()          # would explode if actually used
        w.on_pick = emitted.append
        w._dom_seen = []
        w._seen = set()
        w.poll_dom()               # no selector hints -> must do nothing
        self.assertEqual(emitted, [])

    def test_cli_does_not_poll_dom(self):
        import inspect

        from dfa import cli

        self.assertNotIn("watcher.poll_dom()", inspect.getsource(cli._run_mock))


if __name__ == "__main__":
    unittest.main()


class TestWaiverScoring(unittest.TestCase):
    """Free agent opportunity ranking."""

    def setUp(self):
        from dfa.analysis.waiver import find_opportunities, group_opportunities

        self.find = find_opportunities
        self.group = group_opportunities
        self.replacement = {"QB": 290.0, "RB": 190.0, "WR": 200.0, "TE": 180.0}

    def _fa(self, pid, name, pos, proj, owned=10.0, injury="ACTIVE"):
        p = mkplayer(pid, name, pos, proj)
        p.percent_owned = owned
        p.injury = injury
        return p

    def _room(self, backup_id, starter_name, rank=2):
        return {backup_id: {"pos": "RB", "team": "XX", "rank": rank,
                            "mates": [{"name": starter_name, "rank": 1,
                                       "adp": 20, "ahead": True}]}}

    def test_backup_to_injured_starter_is_a_takeover(self):
        starter = self._fa(1, "Hurt Starter", "RB", 250, injury="OUT")
        backup = self._fa(2, "The Backup", "RB", 120)
        opps = self.find([backup], self._room(2, "Hurt Starter"),
                         {1: starter}, [], {}, self.replacement)
        self.assertEqual(opps[0].kind, "takeover")
        self.assertIn("Hurt Starter", opps[0].headline)

    def test_backup_to_my_injured_player_is_a_handcuff(self):
        starter = self._fa(1, "Hurt Starter", "RB", 250, injury="OUT")
        backup = self._fa(2, "The Backup", "RB", 120)
        opps = self.find([backup], self._room(2, "Hurt Starter"),
                         {1: starter}, [starter], {}, self.replacement)
        self.assertEqual(opps[0].kind, "handcuff")
        self.assertEqual(opps[0].for_my_player, "Hurt Starter")

    def test_handcuff_outranks_the_same_player_as_a_takeover(self):
        starter = self._fa(1, "Hurt Starter", "RB", 250, injury="OUT")
        backup = self._fa(2, "The Backup", "RB", 120)
        room = self._room(2, "Hurt Starter")
        mine = self.find([backup], room, {1: starter}, [starter], {}, self.replacement)
        theirs = self.find([backup], room, {1: starter}, [], {}, self.replacement)
        self.assertGreater(mine[0].score, theirs[0].score)

    def test_healthy_starter_does_not_create_a_takeover(self):
        starter = self._fa(1, "Fine Starter", "RB", 250, injury="ACTIVE")
        backup = self._fa(2, "The Backup", "RB", 120)
        opps = self.find([backup], self._room(2, "Fine Starter"),
                         {1: starter}, [], {}, self.replacement)
        self.assertNotIn(opps[0].kind, ("takeover", "handcuff"))

    def test_gems_rank_by_value_not_raw_points(self):
        """A backup QB outscores every RB on raw points but is worth less."""
        qb = self._fa(1, "Backup QB", "QB", 285, owned=5)
        rb = self._fa(2, "Useful RB", "RB", 215, owned=5)
        opps = self.find([qb, rb], {}, {}, [], {}, self.replacement)
        self.assertEqual(opps[0].player.name, "Useful RB")

    def test_kickers_and_defenses_are_never_gems(self):
        k = self._fa(1, "Some Kicker", "K", 150, owned=2)
        opps = self.find([k], {}, {}, [], {}, self.replacement)
        self.assertEqual(opps, [])

    def test_rising_ownership_is_surfaced(self):
        fa = self._fa(1, "Riser", "WR", 100, owned=20)
        opps = self.find([fa], {}, {}, [], {1: 6.0}, self.replacement)
        self.assertEqual(opps[0].kind, "rising")

    def test_sections_are_ranked_independently(self):
        starter = self._fa(1, "Hurt Starter", "RB", 250, injury="OUT")
        backup = self._fa(2, "The Backup", "RB", 120)
        gem = self._fa(3, "Quiet Gem", "WR", 240, owned=8)
        opps = self.find([backup, gem], self._room(2, "Hurt Starter"),
                         {1: starter}, [], {}, self.replacement)
        sections = self.group(opps)
        self.assertTrue(sections["takeover"])
        self.assertTrue(sections["gem"])


class TestWaiverEdgeCases(unittest.TestCase):
    """Guards for the failure modes found by sweeping the live API."""

    def setUp(self):
        from dfa.analysis.waiver import RISING_THRESHOLD, find_opportunities

        self.find = find_opportunities
        self.threshold = RISING_THRESHOLD
        self.replacement = {"WR": 200.0, "RB": 190.0}

    def _fa(self, pid, name, pos, proj, owned=10.0):
        p = mkplayer(pid, name, pos, proj)
        p.percent_owned = owned
        return p

    def test_rising_threshold_catches_a_typical_week(self):
        """At 1.5 only ~2 of 300 free agents ever qualified."""
        self.assertLessEqual(self.threshold, 0.75)

    def test_modest_ownership_gain_still_surfaces(self):
        fa = self._fa(1, "Small Riser", "WR", 100)
        opps = self.find([fa], {}, {}, [], {1: 0.8}, self.replacement)
        self.assertEqual(opps[0].kind, "rising")

    def test_flat_ownership_is_not_rising(self):
        fa = self._fa(1, "Flat Guy", "WR", 100)
        opps = self.find([fa], {}, {}, [], {1: 0.0}, self.replacement)
        self.assertNotEqual(opps[0].kind, "rising")

    def test_empty_pool_returns_nothing(self):
        self.assertEqual(self.find([], {}, {}, [], {}, self.replacement), [])

    def test_missing_replacement_levels_do_not_crash(self):
        fa = self._fa(1, "Someone", "WR", 100)
        self.assertTrue(self.find([fa], {}, {}, [], {}, None))

    def test_player_already_on_my_roster_is_excluded(self):
        fa = self._fa(1, "Mine Already", "WR", 220)
        opps = self.find([fa], {}, {}, [fa], {}, self.replacement)
        self.assertEqual(opps, [])
