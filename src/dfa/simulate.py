"""Offline draft simulation.

Drafts an entire league using ADP with realistic noise so the board, needs,
runs and recommendations can be exercised without waiting for a real draft.
"""

from __future__ import annotations

import random

from .analysis.flow import detect_runs
from .session import DraftSession


def simulate_pick(session: DraftSession, rng: random.Random, jitter: float = 6.0) -> int | None:
    """Pick roughly by ADP, with noise, respecting rough positional sanity."""
    state = session.ensure_state()
    drafted = state.drafted_ids
    pool = [rp for rp in session.board.ranked if rp.id not in drafted]
    if not pool:
        return None

    overall = state.next_overall
    scored = []
    for rp in pool[:60]:
        adp = rp.player.adp if rp.player.adp else overall + 40
        # Lower is more likely to be taken next.
        scored.append((adp + rng.gauss(0, jitter), rp))
    scored.sort(key=lambda pair: pair[0])

    # Nobody drafts a K or DST before the last couple of rounds.
    rounds_left = state.settings.rounds - state.round_and_pick(overall)[0]
    for _, rp in scored:
        if rp.player.pos in ("K", "DST") and rounds_left > 1:
            continue
        return rp.id
    return scored[0][1].id


def run_simulation(session: DraftSession, seed: int = 7, report_every: int = 12) -> None:
    rng = random.Random(seed)
    state = session.ensure_state()
    settings = state.settings
    if not settings.my_draft_slot:
        settings.my_draft_slot = 4
    settings.my_team_id = settings.my_draft_slot

    total = settings.teams * settings.rounds
    print(f"Simulating {settings.teams}-team {settings.scoring}, "
          f"{settings.rounds} rounds, from slot {settings.my_draft_slot}.\n")

    while state.next_overall <= total:
        overall = state.next_overall
        slot = state.slot_on_the_clock(overall)

        if slot == settings.my_draft_slot:
            recs = session.recommendations(limit=8)
            if not recs:
                break
            _report_my_turn(session, state, recs)
            chosen = recs[0].player.espn_id
        else:
            chosen = simulate_pick(session, rng)
            if chosen is None:
                break
        session.record_pick(chosen, team_id=slot)

        if overall % report_every == 0:
            _report_flow(session, state)

    _report_final(session, state)


def _report_my_turn(session, state, recs) -> None:
    rnd, in_round = state.round_and_pick()
    roster = session.roster_state()
    needs = ", ".join(
        f"{pos} {score:.2f}" for pos, score in sorted(
            roster.needs.items(), key=lambda kv: -kv[1]) if score > 0.05)
    print(f"--- MY PICK {rnd}.{in_round:02d} (#{state.next_overall}) ---")
    print(f"    needs: {needs}")
    for i, r in enumerate(recs[:5], 1):
        why = "; ".join(r.reasons) or "-"
        print(f"    {i}. {r.player.name:22s} {r.player.pos:3s} T{r.tier} "
              f"vor={r.vor:6.1f} vona={r.vona:5.1f} surv={r.survival:.2f} "
              f"score={r.score:5.1f}  [{why}]")
    print(f"    -> taking {recs[0].player.name}\n")


def _report_flow(session, state) -> None:
    runs = detect_runs(state)
    if runs:
        print(f"    [flow @#{state.next_overall - 1}] " + "; ".join(r.label for r in runs))


def _report_final(session, state) -> None:
    print("\n=== MY ROSTER ===")
    roster = session.roster_state()
    for pick in state.my_roster():
        print(f"  R{pick.round:<3d} {pick.player_name:24s} {pick.pos}")
    print("\nstarting slots:")
    for slot, filled, required in roster.slot_summary():
        flag = "ok" if filled >= required else "UNFILLED"
        print(f"  {slot:5s} {filled}/{required}  {flag}")
    print(f"\nbench depth: {roster.bench}")
    total_proj = 0.0
    for pick in state.my_roster():
        rp = session.board.get(pick.player_id)
        if rp:
            total_proj += rp.player.proj
    print(f"roster projected points: {total_proj:.0f}")
