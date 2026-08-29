# Fantasy Draft Analyzer

Live draft dashboard for ESPN fantasy football. Watches your draft as it
happens and tells you who's left, who's gone, what your roster still needs,
and what the news says about the players actually in front of you.

Built and verified against live 2026 ESPN data.

## What it shows

- **Top 30 available**, ranked by a blend of value, roster need, positional
  scarcity and how far a player has slipped past ADP
- **Tiers** per position, so you can see the cliff before you fall off it
- **"Will he last?"** — probability each player survives to your next pick,
  from ADP with realistic variance
- **Positional runs** — "5 of the last 7 picks were RB"
- **Drop-off if I wait (VONA)** — what waiting a round actually costs you at
  each position
- **Last season on hover** — hover any player's name on the board for a
  week-by-week scoring chart, with the points printed on each bar, scored
  under your league's rules. Weeks they didn't play show as hatched gaps.
- **News + analyst outlook** for every player on the board — click any row
- **Injury & red-flag risk** on the top 30 available — IR, surgery, holdouts,
  suspensions, practice status, and how many managers are dropping him.
  Flagged players get a badge and a red marker; click for the sourced notes.
- **A three-player shortlist when you're on the clock** — best overall, best
  value, and the best answer to your biggest roster need, each with reasoning.
  It recommends; you make the pick yourself in ESPN. Nothing is ever
  auto-drafted for you.

## Running it

Double-click **Fantasy Draft Analyzer** on your Desktop, or:

```bash
.venv/bin/dfa app
```

That opens a menu that walks you through it:

1. **Sign in to ESPN** — opens a browser window; sign in normally. The session
   is saved to `espn-session.json` (gitignored) and the browser profile
   persists, so this is usually a one-time step.
2. **Choose a league** — every football league on your account is listed.
3. **Pick a mode:**
   - **Practice draft** — a full mock against bots using that league's real
     settings. Choose your slot and clock, then draft from the board. If the
     clock runs out the app takes its own top recommendation for you.
   - **Draft** — follows your real draft. Start it before the draft opens and
     it waits, then opens the draft room in a window. **Make your picks in
     that window.** ESPN allows only one draft-room session per team, so a
     separate watcher joining alongside your own tab gets evicted the moment
     you join and the board silently stops updating. Sharing one session is
     the only arrangement that works. Picks made before it connects are
     recovered from the room's INIT frame.
   - **Free agency analysis** — locked until that league's draft is complete,
     because there are no rosters to analyse before then.

No config file is required. `config.toml` still works if you prefer to pin a
league or credentials by hand.

### Refreshing the ESPN cookies

Private leagues need `espn_s2` and `SWID`, and they expire periodically.
Firefox stores cookies unencrypted, so they can be read straight out of your
profile:

```bash
.venv/bin/python - <<'EOF'
import sqlite3, shutil, re, pathlib, glob
src = glob.glob(str(pathlib.Path.home() /
    "Library/Application Support/Firefox/Profiles/*default-release/cookies.sqlite"))[0]
tmp = "/tmp/ck.sqlite"; shutil.copy(src, tmp)
vals = dict(sqlite3.connect(tmp).execute(
    "SELECT name, value FROM moz_cookies "
    "WHERE host LIKE '%espn.com%' AND name IN ('espn_s2','SWID')"))
p = pathlib.Path("config.toml"); t = p.read_text()
t = re.sub(r'^espn_s2\s*=.*$', f'espn_s2 = "{vals["espn_s2"]}"', t, 1, re.M)
t = re.sub(r'^swid\s*=.*$',    f'swid = "{vals["SWID"]}"',       t, 1, re.M)
p.write_text(t); p.chmod(0o600)
print("cookies refreshed")
EOF
```

Chrome on macOS encrypts its cookie store behind Keychain, so use Firefox for
this, or copy the values by hand from DevTools > Application > Cookies.

### Install

Needs Python 3.11+.

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/playwright install chromium   # only needed for mock/practice drafts
```

## Running it

### Your real ESPN league draft

```bash
.venv/bin/dfa watch
```

Polls ESPN every 3 seconds, reading league id and cookies from `config.toml`.
League size, scoring, roster slots and your draft slot are all auto-detected;
`--league` and `--slot` override them if you need to.

A private league without valid cookies fails fast with a clear message rather
than silently showing an empty board.

**Note on ESPN's pre-draft board:** before a draft starts, ESPN's API already
returns one pick per slot (160 of them for a 10-team, 16-round league), each
with `playerId: -1`. These are placeholders, not selections, and are filtered
out — otherwise the draft would look complete before it began. Similarly, IR
slots are excluded when computing the number of draft rounds.

### Practice draft you drive yourself (recommended)

```bash
.venv/bin/dfa mock --practice
```

Runs ESPN's *League Specific Practice Draft* for your league — your real
scoring, roster slots and draft slot, against ESPN's auto teams. Nobody else
is in the room. Add `--random-slot` to draw a different draft position each
run.

A browser window opens with **two tabs**:

1. **The ESPN draft room** — you make every pick here yourself
2. **The analyzer dashboard** — when you're on the clock it shows three
   suggestions with reasoning; the rest of the time it's the live board

Flip between them with `Cmd+Option+←/→`. Picks you make appear on the
dashboard within a second or two, as do every other team's.

Leave the terminal running for the whole draft — closing it closes the
browser. `Ctrl+C` when you're done.

### Public ESPN mock drafts

```bash
.venv/bin/dfa mock --slot 5 --teams 10
```

Opens the mock lobby; join a room and picks stream the same way. Note these
rooms contain other real people.

Both modes pick up your ESPN login automatically from Firefox.

### Any other draft — Sleeper, Yahoo, in person

```bash
.venv/bin/dfa serve
```

Dashboard with no watcher. Type picks into the search box as they happen.
This also works as a manual override during a live draft if a watcher stalls —
the search box and Undo button are always active in every mode.

### Pre-draft prep

Open <http://127.0.0.1:8765/prep> (or the **Prep** link in the dashboard
header) while any mode is running.

Browse all 32 depth charts one team at a time — arrow keys or the team strip —
showing QB/RB/WR/TE only. Each player card has:

- **Headshot**; hover it for last season's week-by-week fantasy points,
  scored under *your* league's rules, with missed weeks marked in red
- **Injury & recovery history** button — current designation, injury-classified
  news, and games missed inferred from the weekly log
- **Tag chips** — Hunch, Split Share, Injury Likely, Undervalued

Tags persist to `draft-prep.json` and show up on the draft board rows and the
on-the-clock shortlist, so prep work pays off live. The vocabulary is data,
not code — add your own without a code change:

```bash
curl -X POST "http://127.0.0.1:8765/api/tags/new?label=League%20Winner&tone=good"
```

`tone` is `good` (blue) or `warn` (pink) and controls the badge colour.

### In-season waiver wire

```bash
.venv/bin/dfa serve      # then open http://127.0.0.1:8765/waivers
```

Monitors free agents in any of your leagues (pick from the dropdown) and
sorts them by *why* you would want them, in four independently-ranked
sections:

- **Cover your own injured players** — direct backups to players already on
  your roster who are hurt. The most actionable list on the page.
- **Could take over a starting job** — next man up behind an injured starter
  anywhere in the league, weighted by how good that starter is.
- **Being added right now** — ownership climbing fast; the market has noticed
  something you may not have.
- **Hidden gems** — best available by value over a replacement starter, with
  low ownership as the tie-breaker.

Takeover detection joins the ESPN depth charts to live injury designations:
if the man ahead of a free agent is Out, IR, or Questionable, he surfaces
with the reason attached. Ranking is by value over replacement, not raw
projection — otherwise every backup quarterback floats to the top simply
because quarterbacks score more.

In a shallow league it is normal for no free agent to beat a replacement
starter; the page says so explicitly rather than presenting negative-value
players as recommendations.

### Offline dry run

```bash
.venv/bin/dfa simulate
```

Drafts a full league against real projections and prints the reasoning at each
of your picks. Useful for sanity-checking strategy before draft day.

Dashboard is at <http://127.0.0.1:8765>.

## Where the data comes from

| Data | Source | Notes |
|---|---|---|
| Projections, ADP, ranks, injury status, analyst outlooks | ESPN `kona_player_info` | No auth needed |
| Live draft picks (real league) | ESPN league API `mDraftDetail` | Cookies for private leagues |
| Live draft picks (mocks) | Browser automation | See caveat below |
| Per-player news | ESPN fantasy news feed | Host is `site.web.api.espn.com` |
| Bye weeks | ESPN `proTeamSchedules_wl` | Cosmetic; failure is non-fatal |

Player data caches for 6 hours, news for 30 minutes. `dfa fetch --force`
refreshes immediately.

## Mock draft watching

**Verified against live practice drafts** on 2026-08-06. The draft room speaks
a plain text protocol over its own websocket — `SELECTED <teamId> <playerId>
<slotId>` and friends — fully documented in
[docs/draft-protocol.md](docs/draft-protocol.md). Across runs, 104/104 and
34/34 picks parsed and matched the raw capture exactly, on both player and
team.

Two failure modes were found and fixed, both worth knowing about if ESPN
changes something:

- **DOM scraping does not work for picks.** The room renders a team-by-team
  roster grid plus the whole available pool. Scanning the body reported 115
  phantom picks; scanning a scoped container returned draft-slot order instead
  of chronological order. `poll_dom()` is now a no-op unless explicitly given
  a selector, and nothing calls it.
- **Playwright's sync API needs its event loop pumped.** Idling with
  `time.sleep()` buffers websocket frames until the next Playwright call, so
  the board looks frozen mid-draft. The watch loop uses
  `page.wait_for_timeout()`.

If ESPN changes the protocol, re-record it:

```bash
.venv/bin/dfa mock --practice --capture
```

That writes `cache/capture/capture.log`, which is everything needed to
re-calibrate the parser.

Regardless: the manual search box always works as a fallback.

## Notes on the model

- **Replacement level is derived from the market, not from slot counts.** The
  obvious approach — `teams x starting slots` — badly misprices positions
  whose real roster demand differs from their slot count. In 2026 PPR the
  market drafts 35 WRs by pick 100 against 24 starting WR slots, but only 9
  TEs against 10. So the baseline is the count of each position actually
  drafted inside the starter window (round 10), taken from live ADP, floored
  at the position's dedicated starting slots. The slot-count method with flex
  shares (RB 45% / WR 45% / TE 10%) survives as the fallback when ADP is
  unavailable.

  This fixed a real bug: with slot-count baselines the engine took Josh Allen
  in round 2. Market baselines move him to board rank 22 against an ADP of
  22.6 — the model now agrees with consensus that QB waits in 1QB leagues.

- **VOR** is computed against a static preseason replacement level, so numbers
  don't jump around mid-draft.

- **Bye collisions** carry a small penalty (5% per clash, capped at 10%) when
  a pick would put two starters at one position on the same bye. Real, but
  never a reason to pass on a clearly better player.

## Risk detection

Risk reports are built on a background thread and kept warm for the top 30, so
they're already there when you land on the clock — a 30-second pick clock
leaves no room to go fetch anything. Sources: Sleeper's structured injury
records (joined on its own `espn_id` field), RotoWire's news RSS, the ESPN
per-player feed, and Sleeper's trending-drops counter.

**Precision is the whole game here.** A false "torn Achilles" on a healthy
player is worse than showing nothing, because it talks you off a good pick.
Naive keyword matching was badly wrong on live data — it reported
season-ending ACL tears for A.J. Brown, Josh Jacobs and Kenneth Walker III
(all healthy; the phrases came from multi-player roundup articles), and
flagged Saquon Barkley as retiring because a story mentioned he "spoke with
*retired* running back Todd Gurley." Three defences now apply:

1. **Beat-note gate** — only notes actually written about the player count.
   `Rice (knee) is participating in 11-on-11 drills` qualifies; `Fantasy
   football sleepers, busts and breakouts` does not. This is the main defence
   against roundups, where names and injuries sit words apart.
2. **Proximity** — a phrase must appear within 80 characters of the player's
   name.
3. **Specific phrases, with good news overriding** — `tore his acl`, not
   `acl`. Being "cleared" downgrades a scare, except for terminal items
   (surgery, IR, a torn ligament) that clearing cannot undo.

On the live 2026 top 30 this yields 28 clean, one genuine high (Puka Nacua's
suspension exposure from an ongoing NFL review) and one genuine medium. The
regression tests in `tests/test_engine.py` are built from the real false
positives above.
- **Value is roster-aware.** Raw VOR rates a third tight end as highly as the
  first, even though only one can start. Players are scaled by whether they'd
  fill an open slot (full), upgrade a starter (75%), or ride the bench (30%).
- **K and DST** are hard-capped at one each and suppressed until the final two
  rounds. Their apparent scarcity is an artifact of VOR.
- **Tiers** bound the projected-points spread inside a tier rather than
  splitting on outlier gaps, which otherwise produces one-man tiers at the top
  of a position and ten-man tiers in the middle.
