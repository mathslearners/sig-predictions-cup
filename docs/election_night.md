> Design document written before the night (12-31 Oct 2026). For what actually happened, see [postmortem.md](postmortem.md).

# Election-night bot (4 Nov 2026)

The bot calls races and sizes its bold bets on its own. I watched it through the night, and the way I could step in was the
**remote stop** (`config/STOP_ELECTION` on main, see "Emergency stop" below; it works on running cloud sessions), or a
local STOP file or `SIGBOT_DISABLED=1`.

## Why it is built this way

- On 12 Oct I was #23 at about +67k on the board (about 190k at Kalshi fair value). 3rd place had a **profit** of
  861.6k (all-time board, 12 Oct 10:26Z), which is a balance of about 962k. (This doc and the 12 Oct simulations used
  861.6k as the balance by mistake; election.py has always read balance = 100k + profit.) On 26 Oct, 3rd place had
  974k and I had about 200k at fair value.
- The leaders hold the favourite side of the long-shot premium, so their settled balances will probably grow. The
  realistic bar for top 3 is about 1.0–1.1M after settlement, which is **about 5 times my account**.
- The maker earns about 1–10k a day, so I'd reach 3 Nov with roughly 200–260k. Steady trading alone can't get me
  to the top 3.
- Ranking is the settled balance, and only the top 3 are paid. So the bot aims to maximise the **chance of
  finishing at or above the 3rd-place bar**, not the expected number of SUSQies.

The night has three layers:

1. **Speed.** Reliable profit, but limited by how much is on offer in the order books. Roles C, L, H, P and X below.
2. **Bold overlay** (role B). Bets across several races at once, sized by random simulation to maximise the chance of top 3 at
   the current gap. Far behind the bar, it concentrates on one scenario. Ahead of it, it buys nothing.
3. **Holding to settlement.** Every position held at the 17:00Z close settles at 1 or 0. That includes races still
   being counted (AZ, NV, CA, and ME/AK ranked-choice).

## Pieces

| file | what |
|---|---|
| `election.py` | CLI and live venue. Dry run by default. With `--live`, trades from 3 Nov 21:40Z to the close and stops by the next odd-hour :38. |
| `sigbot/night.py` | Decision engine (pure logic): fair values, call sweeps, lag snipes, chamber control, pairs, risk-off, recycle, bold. |
| `sigbot/calls.py` | Race state machine (PRE / CLOSED / CALLED) and automatic call detection from Kalshi and Polymarket; a call is retracted on a reversal. |
| `sigbot/bold.py` | Gap-rule optimiser: joint outcome draws from the swing model, every SIG offer as a candidate, greedy on a smoothed P(W >= T). |
| `sigbot/swing.py` | Swing model, now with `sample()` (joint draws, used by bold). |
| `sigbot/polyws.py` | Polymarket push prices (public CLOB websocket). |
| `sim_election.py` | Simulated nights against a fake SIG (truth, call times, lagging SIG players, rival bots). |
| `tests/test_night.py` | Offline checks: calls, sweeps, pairs, risk-off, recycle, bold, websocket parsing. |

## Roles (in budget priority order)

- **C call sweep.** Once a race is called, buy the winner's YES and every loser's NO on every SIG level up to
  confidence - 0.02 (or confidence - 0.05 when confidence is below 0.99).
  - A race counts as called when both venues hold one party >= 0.97 for 60 s, or one venue is >= 0.99 while the
    other is >= 0.95.
  - Only one venue quoting (the ~20 House races Kalshi doesn't list, or Kalshi stale): >= 0.99 for 300 s, and the
    confidence is capped at 0.985, so it is bought like an uncertain call (margin 0.05, 10% race cap).
  - Never before ALL the state's polls close (`POLL_CLOSE_H` in sigbot/swing.py: MI waits for its Central-time UP
    counties at 02:00Z, IA closes at 02:00Z, NE closes everywhere at 02:00Z).
  - Per race, up to 25% of the bankroll on a call >= 0.99, 10% otherwise.
- **P pairs.** In a two-party race, buy YES on both parties (or NO on both) when the two offers sum to < 0.97.
  - It pays 1 at settlement whoever wins, so the cash is locked until certification.
  - On 12 Oct, NO+NO pairs at 0.945-0.975 were available on safe seats (YES bids summing to over 1).
- **H chamber control** (Senate 842/843, House 840/841). The outside price is blended with the swing model's seat
  count. It needs 0.05 of edge plus half the disagreement between the two.
- **L lag snipe.** Take SIG offers >= 0.04 (plus buffer) through a trusted outside fair.
  - Trusted means the two venues agree within 0.05, or one venue and the model agree within 0.07.
  - The daytime sniper's "SIG agrees with Kalshi" guard is dropped: on the night, the gap is the edge.
- **X recycle.** While free cash is short and better takes are waiting, offer called winners (confidence 0.99 or higher) at 0.99
  to the players who market-buy winners. Pair netting then returns the cash at once.
- **B bold.**
  - **Adaptive (31 Oct, `--bold-adaptive 1`):** no plan before `--bold-min-calls` 8 race calls (the 22:30Z
    `--bold-at` plan waits for them too); then re-planned every 30 min after new calls.
  - Target T = the 3rd-place balance x (1 + U(0, 0.3)) for the leaders' own night.
  - Cash kept for the other roles earns N(m, s), measured on the night: the speed roles' (C/L/H/P) realised edge
    at fill per race called so far x the calls still expected (the last 2 h's call rate x hours to the close, at
    most the races still uncalled) / the cash kept, blended with the prior N(0.15, 0.25) by calls seen (the prior
    weighs as 10 calls). Rich stale books -> a high m -> bold keeps the cash for the sweeps; thin books or late in
    the night -> m falls -> bold bets if I'm still short of the bar. Logged on every `bold` line (`gain`,
    `ledger`) and in the ntfy note.
  - The measured edge and the call times ride in the lease file (`election-lease`, rewritten every 15 s by the
    holder; every bot merges what it reads), so a restarted run or a standby that takes over starts from the night
    so far. A failure there never touches the lease. `--bold-adaptive 0` restores the fixed prior and the
    scheduled plan.
  - It may also sell holdings (buying the opposite side is in the same search).
- **Risk-off.** 5 min before each state's FIRST polls close (`POLL_FIRST_H`: Eastern KY at 23:00Z, peninsular FL
  00:00Z, Eastern MI and the Central parts of TX/KS/SD 01:00Z; otherwise the state's one close), cancel my resting
  orders there: results start flowing then, an hour before the calls are allowed. Stale orders are what the
  other bots pick off. At the start of each run, cancel everything I have resting.

## Running it

- **Hand-over.** `maker.py` stops trading at 3 Nov 21:37Z. Routines run `election.py --live` at :40 of odd UTC
  hours, so no hand-over lands on a :00/:30 poll close:
  - one scheduled cloud job at 21:40Z and 23:40Z on 3 Nov (cron `40 21,23 3 11 *`);
  - one at 01:40Z-15:40Z on odd hours of 4 Nov (cron `40 1-15/2 4 11 *`).

  Both run the code on main. They restart a killed run at once (up to
  6 times) and push logs to run-logs `election_logs/`.
- **Baseline.** The first election run freezes the pre-results Kalshi/Polymarket prices as the model's prior
  (`data/election/swing_baseline.json`, pushed to run-logs `election_logs/`). Later runs read it back.
  `config/swing_baseline.json` (`python swing.py --save-baseline`) overrides it if committed.
- **Network.** The cloud environment's network access must allow:
  - **`realtime.thesuper.market`**: SIG's realtime host since about 2:30Z on 12 Oct (it was
    `qtbegmirhyoimootzbad.supabase.co`). It has been blocked since then, so every maker run since 12 Oct ~04Z shows
    "rt quiet 0 books", and the election bot would have no full books.
  - `ws-subscriptions-clob.polymarket.com`: without it, Polymarket is polled through Gamma every 3 s.

  Kalshi is polled every 2 s.
- **Rate limits.** Live reads are an account read every 10 s (2 reads), REST tops only while the realtime feed is
  quiet, and the leaderboard every 5 min. Writes go in batches of 25 orders with 3 writers.

## Failsafes

| what can go wrong | what happens |
|---|---|
| A run crashes or the cloud worker kills it | The routine restarts it at once (up to 6 times per slot). Meanwhile the other bot is a **hot standby** (below) and takes over within about a minute (lease 60 s, renewed every 15 s; tested 12 Oct: killed holder replaced in 43 s with a 40 s lease). |
| A routine doesn't fire, or its session fails to start | Two independent routines start election.py: the election routine (:40 of odd hours) and the existing 2-hourly maker routine (maker.py execs election.py from 3 Nov 21:37Z, until its even :29 slot end). |
| Two bots running at once | The lease on branch `election-lease` (`sigbot/lease.py`): only the holder sends orders, and the other keeps its feeds and model warm. Pushes are never forced, so two bots can't both win the lease. If git is unreachable, the holder keeps trading and the standby keeps waiting. A new holder cancels everything resting once, then trades. The lease's git fetch + push runs in its own thread, so it never stalls a take. |
| GitHub down at a hand-over or restart (26 Oct) | Before, a bot that never got the lease could not trade at all while git was down, so nobody traded. Now, after the lease has been unreadable for `--lease-ttl` + 30 s (90 s), the **primary** run (the election routine's plain `election.py --live`) clears the board and trades without the lease. maker.py's hand-over copy runs with `--secondary`: it never trades blind and stops after 60 s of outage even if it held the lease, so it has stopped before any primary starts. Primary runs never overlap, so at most one bot trades blind. When git comes back the normal lease applies again. A remote stop seen before the outage still holds. Remaining risk: if only the primary's container loses GitHub while the secondary still reaches it, both trade for that time. |
| Stale orders when a race is called | My resting orders on a state are cancelled 5 min before its first polls close. Every take expires after 60 s. Exits expire after 15 min. |
| Bad or stale prices | Outside quotes older than 20 s are ignored. A call needs both venues (or one at >= 0.99 with the other >= 0.95; one venue alone needs 300 s and is capped at 0.985 confidence) and is retracted if the winner's price falls back under 0.90. A lag snipe needs Kalshi and Polymarket to agree (or one venue plus the model). |
| One step throws | It is logged and the loop goes on. A failed account, open-orders or books read is logged and skipped: the last account snapshot stands in and risk-off retries next step. |
| Slow SIG reads (median ~5 s under load) | The account snapshot, REST books, model refresh, bold planning and the lease all run in background threads, so a step never waits on them. Fills since the last account read are netted off its cash. |
| Realtime feed down | REST tops every `--rest-tops-every` s plus full orderbooks for the called races (latest call first, `--fallback-book-reads` 30/min), so a call sweep sees the real stale depth instead of the assumed 1,000 shares. |
| Over-spending | Per-race caps (25% of bankroll on a call >= 0.99, 10% otherwise), counting the cost already held in the race plus fills of the last 30 s. Takes that lock cash until settlement (L, P) only spend above 50% of the bankroll. No second take on the same side within 15 s. SIG itself sizes buys to the cash balance. |
| Restart after the polls close | The swing prior is the baseline the first run froze before 00:00Z (pushed to run-logs). Calls are re-derived from the venues within 30 s. |
| Emergency stop | **Remote stop:** the file `config/STOP_ELECTION` on origin/main. Every election.py fetches it with each lease check (15 s). While it exists, every bot cancels everything (twice, 15 s apart, for takes in flight), the holder gives up the lease, no standby takes it, and runs started meanwhile stand by. If git is unreachable a stopped bot stays stopped. Deleting the file resumes trading within ~15 s (the first bot to take the lease clears the board first). Locally: `SIGBOT_DISABLED=1` or a STOP file cancels everything and ends the run. |

**Remote stop.** In any clone of the repo (pushes to origin/main with git plumbing;
the checkout and branch are not touched):

```bash
python stop_election.py on
```

```bash
python stop_election.py off
```

`python stop_election.py status` shows it. From a phone: create or delete `config/STOP_ELECTION` on main in GitHub's web editor.
It only stops election.py (maker.py trades until 21:37Z and doesn't read it). Remove it before 21:40Z on 3 Nov if you
tested it, or the night starts stopped. The bots log `{"kind": "lease", "state": "remote_stop"}` and notify via ntfy.

**Test without sending anything:** `python election.py --paper --minutes 5` runs the live path now: the lease (on
`election-lease-paper`), standby and take-over, the books, every decision. It logs what it would send as
`paper_orders`.

## Simulation results (12 Oct, `sim_election.py`)

Setup: 220k start, bar 860k, SIG players 5 min behind the outside venues, rivals take 90% of every stale level first.

| setup | mean | median | 10th percentile | 90th percentile | max | P(>= start) | P(>= bar) |
|---|---:|---:|---:|---:|---:|---:|---:|
| 2k shares/level, edge only (10 nights) | 336k | 337k | 311k | 382k | 397k | 1.00 | 0 |
| 20k shares/level, edge only (8 nights) | 561k | 602k | 424k | 654k | 680k | 1.00 | 0 |
| 20k shares/level, with bold (8 nights) | 567k | 589k | 344k | 751k | 763k | 0.88 | 0 |
| same, after the 12 Oct (afternoon) changes, edge only | 567k | 617k | 394k | 662k | 685k | 1.00 | 0 |
| same, after the 12 Oct (afternoon) changes, with bold (bar 864k) | 557k | 606k | 390k | 652k | 669k | 1.00 | 0 |

The pm changes are the calibrated detector, the recycle trigger, `lock_keep` relaxing late in the night, and the
structural fairs. In the sim, calls land on the tick (the detector's 60 s hold is shorter than a 10-minute tick); with
the hold applied per tick, every call was a tick late and the median fell to 290k. That was an artifact.

- Nearly all the edge comes from call sweeps (C): stale SIG orders on races the venues have already called. The
  result depends on how slow SIG's players really are and how deep the stale books are. Both are unknown until the
  night, so the 5 min lag here is a guess.
- Bold widens the outcomes upwards (90th percentile +97k, max +83k), at the cost of the bottom tail. That is the trade a
  top-3-or-nothing goal wants. Even so, no simulated night reached the bar.
- A 60% per-race cap lost 440k on one race in one night: a 0.99 race that lost, which happens about once in 100
  races. Hence the 25% / 10% caps.

## Checks

- `python tests/test_night.py`
- `python sim_election.py --nights 20 --races-cache /tmp/races.json --compare`: bold versus edge only, P(final >= bar)
- `python election.py --steps 3 --step-s 20 --rest-tops-every 60 --account-every 120`: dry run on live data. It
  reads SIG at about 8 reads/min beside the live maker and sends nothing.

## Calibration and studies (12 Oct)

**Race calls on the 2024 night** (`research/calibrate_calls.py`: Kalshi 1-minute history, 52 races with a result)

| threshold | hold | calls | wrong | median min after close (contested) |
|---|---|---:|---:|---:|
| 0.97 | 1 min | 43 | 1 (2.3%) | 519 |
| 0.97 | 2 min | 37 | 0 | 822 |
| 0.99 | 1 min | 29 | 1 (3.4%) | 465 |
| 0.99 | 2 min | 25 | 0 | 557 |

- The wrong call was NY-04 House. It was at 0.385 when its polls closed, spiked to 0.99 for one minute 19 min
  later, and then the other party won.
- Contested Senate races were called a median ~5 h after their close; House races ~17 h.
- Safe seats (>= 0.95 at the close) are effectively called at the close.
- So the detector needs both venues >= 0.97 for 60 s, or one venue >= 0.99 with the other >= 0.95 for 60 s. A
  contested race (leader < 0.80 at the close) waits 300 s in its first 30 minutes. The hold is tracked before the
  close, so a safe seat is called the second its polls close.
- Prices of called races often dip under 0.90 intra-minute (thin books). A retraction only pauses buying, and the
  call re-forms after the hold.

**SIG's reaction lag** (`research/lag_study.py`, tapes 8-12 Oct):
- Only one Kalshi move of >= 3c in 5 min happened (quiet October). SIG covered half of it in 5 min and 90% in 8 min,
  consistent with the simulation's 5 min, but it is one event.
- With moves >= 2c (3 events): half covered in 5 min, in 43 min, and never within 60 min. SIG's players are slow to
  follow small outside moves at all, which is the edge lag snipes take.
- Maker cycle overruns (reads + writes beyond the 20 s cycle): median 0 s, 90th percentile 4–12 s, 24-26 s at 16-17Z, with
  rare stalls of 4-29 min.
- Exact batch latency is logged per run from now on (`batch_latency` in the maker summary on run-logs).

**Structural markets** (`research/structural_audit.py`; outside prices <= 0.01, ballots checked by hand):
- **Nebraska Senate D: certain NO.** The nominee withdrew in July to back Osborn, who has his own SIG
  Independent market. It is in `config/structural.json`.
- Idaho and South Dakota Senate D: the nominees withdrew too, but the independents there (Achilles, Bengs) have no
  SIG market, so SIG might count a win for them as "Democratic". These are left to the outside fair (~0.006).
- Montana Senate D: Bankhead stays on the ballot; an ordinary long shot.

**Leader exposures** (`research/leader_exposure.py`):
- A ridge fit of 1d board profit steps on SIG mid steps does NOT recover my own positions: median error ~100% at
  20/36/96 h. The board's profit moves when an account trades and the print mids bounce a full spread.
- So the leader-aware bold target stays off. The engine uses `config/leader_exposure.json` only when the script
  marks it trusted.
- Plausible but unverified: calm-badger short ~109k "D wins Senate" (matches 10 Oct).
- Board on 12 Oct: 1st kind-brook 1.04M, 2nd bright-finch 956k, 3rd steady-finch 864k.

**Order paths** (`tools/election_selftest.py --live`, 12 Oct 11:59Z, 1 share each on exchanges 1017/1018): ALL PASS.
Paths tested: take YES, take NO netting it, pair YES+YES, rest + listed, cancel, 60 s expiry. Writes took 0.7-1.1 s
off-peak; it cost 0.005 plus a 1-share pair held to settlement.
- The first try on 842/843 showed SIG's no-operation rule live: a buy is accepted with `orderId: null` and fills nothing
  when my resting buys on that exchange already commit the cash.
- On the night, election.py cancels everything when it takes the lease, and it now counts no-operations (`noops` in the
  run summary). One remaining risk: an unfilled take remainder rests up to 60 s and can block further buys on that
  exchange meanwhile.

