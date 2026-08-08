# ESPN draft room wire protocol

Reverse-engineered from live practice drafts on 2026-08-06. This is what the
mock/practice draft watcher parses. Undocumented by ESPN and subject to change
without notice — `dfa mock --capture` re-records it.

## Transport

```
wss://fantasydraft.espn.com/game-1/league-<draftLeagueId>/JOIN
```

`<draftLeagueId>` is the *practice draft's* own league id, not your real
one — a fresh one is minted for every practice draft you start.

A second socket to `espn.connections.edge.bamgrid.com` carries Disney
telemetry and is irrelevant.

## Messages

Plain space-delimited text, one message per frame. **Not JSON.**

| Message | Meaning |
|---|---|
| `SELECTED <teamId> <playerId> <lineupSlotId>` | **A completed pick.** |
| `SELECTING <teamId> <millis>` | Team is on the clock |
| `CLOCK <n> <millis>` | Countdown tick |
| `STATE <n>` | `1` once the draft is live |
| `TOKEN 1:<leagueId>:<teamId>:<swid>:<n>` | Identifies **your own** team id |
| `JOINED <teamId> <swid>` / `LEFT …` | Membership changes |
| `AUTOSUGGEST <playerId>` | ESPN's suggested pick — *not* a selection |
| `AUTODRAFT <teamId> <bool>` | Autopick toggled |
| `PONG PING%20<ts>` | Keepalive |
| `INIT <base64>` | Opaque binary state blob; ignored |

`playerId` matches the ids from `kona_player_info`, so picks resolve directly
against the player pool (104/104 and 34/34 resolved across runs).

Pick order is simply the arrival order of `SELECTED` frames. Snake order is
confirmed by round 2 mirroring round 1.

`lineupSlotId` is the roster slot the pick was filed under (2=RB, 4=WR, 6=TE,
3=RB/WR, 5=WR/TE, 20=bench…). It reflects roster placement, not the player's
position, so don't use it to infer position — look the player up instead.

## Two traps, both hit in testing

**1. Do not scrape the DOM for picks.** The draft room renders a
team-by-team *grid* of rosters plus the full pool of available players. Two
separate live runs produced garbage:

- Scanning `body`: reported 115 phantom picks — every *available* player.
- Scanning a scoped pick container: returned players in draft-slot order
  rather than chronological order, silently corrupting every pick number.

The websocket is exact. `poll_dom()` is now a no-op unless given an explicit
selector, and nothing in the normal path calls it.

**2. Pump the Playwright event loop.** With the sync API, websocket handlers
only fire while you are inside a Playwright call. A watch loop built on
`time.sleep()` buffers every frame until the next interaction — the draft
appears frozen, then dumps everything at shutdown. Idle with
`page.wait_for_timeout()` instead.
