# SIG Predictions Cup 2026: post-mortem

I wrote this on 7 Nov 2026 from the logs on the run-logs branch (36 daily reports, 31 leader studies and the election-night logs), the git history, the final all-time leaderboard, and SIG's public trade records for launch day and election night.

## 1. Result

| | balance | profit | volume | rank |
|---|---:|---:|---:|---:|
| nimble-beacon-4953 | 1,519.8k | +1,419.8k | 25.2M | 1 |
| steady-finch-4087 | 1,456.8k | +1,356.8k | 31.9M | 2 |
| steady-ember-8793 | 1,394.2k | +1,294.2k | 2.7M | 3 |
| kind-brook-6861 | 1,189.3k | +1,089.3k | 20.2M | 4 |
| bright-badger-3145 | 985.8k | +885.8k | 7.0M | 5 |
| **steady-acorn-6517 (me)** | **211.6k** | **+111.6k** | 5.97M | **34 of 5,141** |

(This is the final board after every market settled. On 7 Nov, when the first version of this was written, I was #32
at +111.7k with 50 markets still open.)

**Where my money came from:**

| period | account at Kalshi/Polymarket fair value | change | per day |
|---|---:|---:|---:|
| 1 Oct 16:00Z open to 2 Oct 00:14Z | 100.0k | 0 | could not trade yet |
| 2 Oct to 6 Oct | 100.0k to 173.8k | **+73.8k** | ~15k |
| 6 Oct to 3 Nov | 173.8k to 211.1k | +37.3k | ~1.3k |
| election night and settlement | 211.1k to 211.7k | ~+0.6k | n/a |

**Where the leaders' money came from:** 3rd place was already **+656k by 2 Oct 21:45Z** (about 30 hours in) and +744k
on 4 Oct. From then until 3 Nov, 3rd place only gained about 200k, most of it from SIG's own marks on held
favourites. Election night and settlement then added 300–400k for the top two and **+1.29M for steady-ember-8793,
who was not in the top 10 before the polls closed**.

So the tournament was decided in two windows: **the first ~30 hours** and **election night**. I missed the first 8
hours of the first and had 1k of free cash in the second.

## 2. Launch day (1 Oct 16:00Z to 2 Oct 22:00Z)

This comes from SIG's public trade record (207.8k trades), valued against Kalshi's minute-by-minute price:

- **161M shares traded and about 10.0M SUSQies of mispricing against Kalshi** in 30 hours. Of that, 5.4M was in
  trades at least 10c from fair and 2.9M in trades at least 25c from fair. It came from players' market orders
  (the price defaults to 1 for buys and 0 for sells) sweeping thin order books. For example, NH Senate R sold at
  0.055 when fair was 0.865 (102k shares, 17:34Z). RI Senate D sold at 0.03–0.115 when fair was 0.99 (several
  40–90k blocks, 17:10–18:07Z). U.S. House D sold at 0.005 when fair was 0.915 (43k shares, 16:20Z). OK Senate D was
  bought at 0.99 when fair was 0.02 (97k shares, 16:49Z). Each of those was a gift of 25–80k to whoever had a standing
  order there.
- **The first 8 hours (16:00Z to 00:14Z) held about 4.6M of it, roughly 3.7M in trades at least 10c from fair.** I
  couldn't place any orders in those hours. Mispricing of 10c or more ran at 0.5–0.8M an hour from 16Z to 22Z, then
  fell to about 0.1–0.2M an hour as other players' very low bids filled the books, with bursts later (0.26M at 21Z
  on 2 Oct).
- The leaders' +650–760k by 21:45Z on 2 Oct is about 7% of that 10M. **Being there with deep standing orders on every
  market, and enough cash behind them, was worth hundreds of thousands. I earned +16k that day.**

## 3. Election night (3 Nov 21Z to 4 Nov 17Z)

This comes from SIG's public trade record (117.7k trades) valued against Kalshi's minute-by-minute price, plus the
settled results:

- **94.5M shares traded** (about 50M SUSQies bought by takers). That's about 2.5 times the busiest October day, in 20
  hours.
- **About 9.8M SUSQies of mispricing against Kalshi**, of which about 6.9M was in trades at least 10c from fair and
  about 5.2M at least 25c from fair. It peaked between 00Z and 04Z as polls closed in the East and Midwest, at
  roughly 1.3–1.8M an hour. Most of it was the same story as day 1: market orders sweeping thin books. VA-05 D was
  bought at 0.985 at 00:13Z and lost. Winners in FL-09, MI-10 and Mississippi sold at 0.01–0.035. Rhode Island
  Senate D, already called, was dumped at 0.095–0.10 at 16:23Z, 40 minutes before the close.
- **Buying called winners turned out to be a small game.** In the first 10 minutes after my bot's calls, only 1.5M
  shares of winners traded under 0.97, worth about 103k to the whole market. Spending 200k of my cash there at
  0.80–0.97 would have made about 24k. The big money was in the wild trades, which deep standing orders (low bids
  at 0.50, 0.30 and 0.10 on both sides of every live race) would have caught. Later in the night (an hour or more
  after the calls) another 14M shares of winners traded under 0.97, because players were slow to reprice races that
  were already decided.
- **The night was a Democratic wave.** Pre-night prices expected 70.4 Democratic wins out of 114 races, and
  Democrats won 81. They took almost every toss-up and several long shots: Mississippi Senate (0.14), South Carolina
  Senate (0.31), CO-04 (0.33), Kansas Governor (0.36), Texas Governor (0.37), TX-23 and MN-01 (0.39), plus the Senate
  (priced at 0.74) and the House (0.94).
- **What I did:** I started the night with 1,005 of free cash. The status lines read `takes 0` almost all night. A
  handful of fills under 2k each, the bold overlay spent about 25 SUSQies (the chance of finishing top 3 was 0.000 in
  every plan), and trying to sell called favourites at 0.99 to raise cash found no buyers.
- **steady-ember-8793 started the night with about 1.7 times my balance and finished +1.29M.** Their profit was
  about 230k on 20–21 Oct (balance ~330k) and still under the 10th-place cut (~257k) at 00:45Z on 4 Nov. So they
  gained about 1.0M on the night, roughly 3–4 times their bankroll. Thin order books weren't the limit: catching about 10% of the
  night's off-fair trades, or rolling a Democratic-wave basket from early-closing races into later ones, was enough
  to reach 3rd place.

## 4. What I did well

- **Reliability.** 34 days of scheduled runs, with cloud jobs every 2 hours, practice mode by default, an expiry on
  every order, the /account red line kept, no leaked keys and no runaway losses. The worst day was −5.1k at fair
  value. The election-night bots handed over between the 2-hourly runs all night, with one bot trading at a time.
- **The fair-value comparison was good.** The Kalshi and Polymarket blend priced 234 of 237 markets. My purchases
  kept about 95% of their advantage after 30 minutes, so I wasn't being picked off. **The race-call detector called
  111 races correctly.** It made one wrong call: Mississippi Senate went Republican at 04:41Z on 4 Nov with Kalshi at
  0.976 and Polymarket at 0.984, and Democrats won. Both markets were wrong together, so "both at 0.97 or higher" is
  not safe on its own.
- **The edge on entry was real.** +404k against fair across the tournament (ladder +219k, sweep catchers +153k,
  sniper +32k, from the daily-report totals).
- **Measuring before changing.** The profit study, the fill model, the trade replays and the daily leader study
  caught real mistakes (selling holdings below fair value, the cost of busy-hour preparation, losses on small
  quotes), and I switched those rules off.
- **My return on capital in October was competitive.** On several days I beat every big leader per SUSQie at fair
  value (11 Oct: 5.6% against 4.3% or less). The gap to the top was capital and timing, not the quality of each
  trade.
- **Holding to settlement paid.** The favourites I carried into the night (NY Governor, SD Governor, MN Senate, OK
  Senate, RI Senate) paid about +17k over cost. The one loser (Mississippi Senate, a Democratic upset) was mostly
  sold during the night.

## 5. What I did badly

1. **Launch day.** Every order from 16:00Z to about 00:14Z was rejected, and my pre-open checks only ever saw
   "Tournament has not started yet", which hid the problem. Then `run_once.py` traded small (caps of 1,500 per market
   and 12k per run) until maker.py went live at about 02:30Z on 2 Oct, roughly 10.5 hours after the open. The sweep
   catchers, the approach that made the leaders' money, only arrived at about 12:30Z, around 20 hours in.
   **launch.py itself was sized for "small, safe edge plus information"**: 200-share quotes, 1,000-share takes, a
   40k reserve, a full cancel every 45 seconds and 5-minute expiries. Even if it had run from the start, it couldn't
   have caught what the leaders caught.
2. **Exits gave back 77% of the edge.** Buying earned +404k against fair, and selling cost **−311k** (6,366 sell
   fills, 5.2M shares). SIG priced favourites systematically under Kalshi and Polymarket. Buying a favourite cheap
   and selling it back at SIG's own price a few minutes later handed most of the gap back. Holding everything would
   have needed about 11 times the cash-days, so some recycling was right, but the "turnover first" phase (2–9 Oct)
   recycled far too cheaply.
3. **Too many strategy changes.** Within 8 days my cash policy flipped between "fast turnover", "hold for value",
   "free stuck positions even at a loss", "busy-hour cash floor" and back again. That came to 107 commits, 65 of them
   to maker.py. Each flip cost realised losses (freeing positions on 4 Oct cost about 5k plus about 11.5k of edge
   given up; busy-hour preparation from 13 to 26 Oct cost about 19k at fair to fund about 21k of catches).
4. **I played for the average, not for the top 3.** From about 9 Oct the account moved steadily into low-risk
   favourites at 0.82–0.90 (about 10–15% to settlement). That was right for the average result and wrong for a prize
   that only pays the top 3. **Every bold plan on election night worked out a 0.000 chance of finishing top 3 and spent
   nothing** (a few tiny orders, under 25 SUSQies in total). An account with about 1.7 times my balance
   (steady-ember) did reach 3rd place by gaining 3–4 times its bankroll on the night (section 3).
5. **I went into election night with 1k of free cash.** On 1–3 Nov the maker had under 5k of cash in 96% of cycles,
   with about 192k locked in holds. election.py called 111 races correctly and **made essentially zero takes all
   night** (`takes 0 (0, edge 0)` in almost every status line). The fallback for freeing cash offered about 1.26M of
   called winners at 0.99 and almost nothing filled, because on SIG nobody buys a race that's already been called at
   0.99. The plan depended on selling holds after their calls to fund the sweeps, and that market didn't exist.
6. **I rejected market making after two losing evenings.** The two leaders by profit (nimble-beacon, steady-finch)
   were 70%+ market makers on 25–32M of volume. My test was 500–1,000 shares at the back of the queue behind their
   walls, on 10–15 markets, judged over two evenings. That was too small and too short a test of the strategy that
   won the tournament.
7. **Volume.** I traded 6.0M against 20–35M for the market-making leaders. On 8 Oct the weekly leaders were earning
   the same ~1–1.5% of volume I was. After week one the gap kept growing because of volume (and capital), not the
   quality of each trade.

## 6. What the top players did

These are patterns from the 31 daily leader studies (matched against the trade record; low confidence for any one
account but consistent overall):

| account | final profit | what they did |
|---|---:|---|
| nimble-beacon-4953 | 1,419.8k | About 693k by 9 Oct. Then all month: 55–75% market making at or inside the best price on hundreds of markets (+0.3–0.7c against the midpoint), holding the favourites that long shots were sold against, and U.S. Senate control. +413k from election night and settlement. |
| steady-finch-4087 | 1,356.8k | About 718k by 8 Oct. Then pure market making: 30k trades averaging ~200–250 shares, 66–76% of them standing orders, about 1% of capital a day, the most consistent earner. +333k from the night and settlement. |
| steady-ember-8793 | 1,294.2k | #14 at about 230k profit on 20 Oct with a book of favourites and long shots. **About +1.0M on election night**, 3–4 times their bankroll, on only 2.7M of lifetime volume. |
| kind-brook-6861 | 1,089.3k | **+904k by 8 Oct on 8.9M of volume (10% of everything traded)**: big standing orders on long shots and favourites while the books were thin, then large favourite blocks during long-shot sweeps. Gained only about 185k after 8 Oct, mostly from marks and settlement. |
| bright-badger-3145 | 885.8k | Concentrated one-way bets. +143k on 2 Nov alone from SIG's marks on election-eve positions. |

The common thread: **#1, #2 and #4 were all at roughly 690–910k by 9 Oct** (day-1 liquidity), then compounded with
market making and holding cheap favourites. #3 came from nowhere on election night.

## 7. What other players did badly (and well)

- **Beginners' market orders funded the leaders.** On day 1, long shots traded at 0.995 and favourites at 0.005. Later,
  SIG priced long shots 2–9c over the outside price all month, and now and then someone dumped a whole balance into
  long shots (RI Senate R, 591k shares on 2 Oct; ten long shots at once on 1 Nov at 14:08Z), which pushed favourites
  5–10c under fair for a few minutes.
- **Mark-pushers.** Some accounts (calm-badger on D Senate control on 10 Oct, gentle-coral on CO-04) pushed SIG's
  price on a market they held to mark themselves up. It looked good on the daily board and was worth nothing at
  settlement unless the outside price agreed.
- **Lottery tickets did as well as I did.** On the final board, quick-coral-1596 (+127k, 2 trades), tidy-cedar-9237
  (+111k, 3 trades), merry-birch-0253 (+173k, 4 trades) and quick-finch-0376 (+206k, 31 trades) put their whole
  balance on one or two races and won. Plenty of others will have done the same and lost, and the board only shows
  the winners. With a prize that only pays the top 3, finishing #34 with a careful bot is worth the same as losing
  everything on a coin flip: nothing.

## 8. How I'd improve the bot

1. **A real test order the moment trading opens**, plus a check of every sign-up and terms page. Send me a phone alert
   if the first order fails for any reason other than "not started yet".
2. **One process, driven by the live feed.** I had a maker, a sniper thread, an order-book cache, a leader tracker and
   an election bot all sharing a 100-read budget against an API that took about 5 seconds to answer. A single
   event-driven core fed by pushed order books and trades, using the regular API only to double-check, would cut
   delays from seconds to well under a second and free up the read budget.
3. **Quote with the queue in mind.** Track my place in the queue from the pushed order books, and quote where I'm near
   the front (new price levels, one step inside) instead of behind 300k-share walls. Shift quotes with my inventory
   and the outside price instead of cancelling back to flat.
4. **Treat cash as the scarce resource.** One allocator that prices every use of a SUSQie (dip catch, value hold,
   quote inventory, election-night reserve) by return per day locked up, instead of rules fighting each other (the
   busy-hour floor against rotation against the value floor against follow-ups). Set aside the election-night
   bankroll weeks ahead.
5. **Simulate before shipping.** Most rule changes were tested on one practice run and then a live day. A simulator
   that replays the recorded trades and models queue position (I had the data) would have killed the fast-exit and
   busy-hour rules in an afternoon and tested market making properly.
6. **Election night needs liquidity that actually exists.** Don't plan on selling called winners at 0.99. Keep the
   cash, hold positions that settle early (paired off), or place sweeps sized to the cash that's really free.

## 9. How I'd improve the strategy

1. **Front-load everything.** The first 24–36 hours were worth about 700k to the leaders and about 17k to me. Next
   time the opening plan is the main plan: maximum-size standing orders deep on both sides of every market (at fair
   value ± 0.10, 0.20, 0.35 and 0.60 across all 237 markets) plus a taker for anything far through fair, with
   capital sized to the full balance, not a 40k reserve.
2. **Hold the long-shot premium instead of flipping it.** SIG priced favourites under fair all month. The leaders held
   them. I bought them and sold them back at SIG's price minutes later. The rule should be that something bought
   under the outside price is held unless the cash has a clearly better use right now, and the exit is priced at fair
   value, not at SIG's level.
3. **Size the plan to the prize.** Only the top 3 of 5,141 were paid. Once the gap to 3rd was clear (about 4 Oct, at
   700k), playing for the average could not win. The plan should switch from "maximise SUSQies" to "maximise the
   chance of top 3": keep compounding cheap favourites to grow the base, but enter election night with the whole
   bankroll free and a high-risk plan decided in advance.
4. **Take a correlated view on election night.** Races aren't independent: a national swing moves all of them. I had the
   swing model and the "correlated basket" idea in my notes from day one and never used either for a bet. Democrats
   won 81 of 114 races against 70.4 expected from pre-night prices, and won nearly every toss-up (Kansas Governor
   0.36, Texas Governor 0.37, TX-23 0.39, MN-01 0.39, SC Senate 0.31, CO-04 0.33, Mississippi Senate 0.14). Once the
   first ~20 races came in leaning Democratic, the direction of the night was readable. My swing model misread it as a
   Republican wave until a fitting bug was fixed at 02:08Z, and nothing traded on it anyway. A bot that puts the whole
   bankroll on uncalled Democratic toss-ups in later-closing states (and rolls each called race into the next) is the
   high-variance play that could turn 200k into 1M or more.

## 10. Other approaches beyond Kalshi/Polymarket arbitrage

- **Providing liquidity at the open** (the biggest single source of profit in the tournament): deep standing orders far
  from fair value to catch beginners' market orders.
- **Market making and spread capture** at scale, with queue priority and inventory control (nimble-beacon,
  steady-finch).
- **Price rules inside SIG:** the Democratic and Republican prices of one race should add up to about 1 (I saw YES bids
  summing to 1.035–1.045), chamber-control markets should match the sum of the race markets, and SIG's own
  `/relationships/constraints` scanner (empty at the start and never re-checked later) could be used.
- **Following the flow:** dumps and pumps by single big players left favourites 5–10c cheap for 5–30 minutes (18 of 20
  dumps between 12 and 22 Oct). A fast, funded follow-up captures that.
- **Trading on results as they arrive:** AP, NBC and NYT calls, county-level vote counts against expected margins, and
  early-closing bellwether counties feeding a swing estimate for races still open.
- **My own forecasts** from polls and fundamentals for the races Kalshi and Polymarket price thinly (House seats,
  Alaska Governor). I didn't do this.
- **Settlement timing:** races called early but certified late tie up other players' cash too. Buying called winners at
  0.97–0.98 from players who need cash is a steady return if yours is free.
- **Taking on risk deliberately:** when only the top 3 is paid, one big correlated bet (a wave) beats a portfolio of
  many independent small edges.

## 11. Key takeaways for the next tournament

1. **The first day is the tournament.** Be fully set up and tested, and put the whole balance into deep standing
   orders from the first second.
2. **Rank, not return.** Work out on day 2 what final balance the top 3 needs and plan backwards. If steady edge can't
   get there, risk has to.
3. **Keep cash free for the known big event** (election night here). Locking 99% of my money in 10% favourites the week
   before was the single decision that took me out of contention on the night.
4. **Don't hand value back to the crowd.** If you buy under fair, sell at fair or hold.
5. **Change fewer rules, test them in a simulator, and give each strategy enough data before judging it.**
6. **Look at the board's winners early and copy their patterns, not their trades.** By 4 Oct it was clear the leaders'
   money was day-1 liquidity provision plus holding favourites, then market making.
