# Complete trade-cycle exercise

The only launchable gameplay scenario, `--scenario trade-cycle`, tests every
currently supported bilateral trade item and the negotiation's accept, decline,
withdraw, supersede and deadline paths. Rome
and Greece use their ordinary HTTPS services, including off-turn communication
jobs while the host holds gameplay.

The Gods & Kings fixture starts with two cities per player, peaceful economic
infrastructure, the required technologies, and an existing war whose minimum
negotiation period has passed. Rome has 2,000 gold; Greece starts with zero.
Cities produce Gold with growth disabled. No agreements are pre-signed.

## Run it

Use a fresh directory. In four terminals, start the host, wait for it to become
ready, start both services, then start scheduling:

```sh
uv run civ-agent host start --state-dir /tmp/civ-trade-cycle-1 --scenario trade-cycle
uv run civ-agent agent-server start Rome --state-dir /tmp/civ-trade-cycle-1 --port 8867
uv run civ-agent agent-server start Greece --state-dir /tmp/civ-trade-cycle-1 --port 8868
uv run civ-agent match start --state-dir /tmp/civ-trade-cycle-1 \
  --max-turns 13 --max-invocations 96 --turn-timeout 300 --max-seconds 7200 --verbose
```

Append `--model NAME --reasoning-effort LEVEL` to `match start` to override both
players' model settings. Otherwise their configured defaults apply.

The host defaults to three shared counteroffers and a 300-second negotiation
deadline. This exercise requires the counteroffer limit to remain three.
**The fourth negotiation deliberately waits for the deadline.** Expect a
five-minute pause in gameplay there. The service waits through HTTPS; models
finish their communication jobs without polling or consuming tokens to wait.
Changing `--trade-timeout` changes this deliberate pause and also the time allowed
for every other negotiation, including the three-counteroffer bargain.

The host supplies only the current step's instructions. Rome initiates at most
one new negotiation per turn; Greece handles it off turn. After settlement Rome
refreshes its view and finishes gameplay, then Greece takes its ordinary turn.
Eleven steps plus two further complete rounds fit in 13 game rounds. An agent
that postpones a step may need additional rounds; inspect the report before
extending the match limits.

## What happens

All quantities below are explicit scenario instructions. The agents still read
the permitted options for actual item names, durations, costs and city handles.
Give/receive always use the acting player's perspective.

| Step | Negotiation | Expected result |
| --- | --- | --- |
| Paid peace | Rome offers mutual peace and 40 Gold; Greece counters with 30 Gold | Rome accepts the counter; the engine makes peace and transfers 30 Gold |
| Decline | Rome offers a 7 Gold gift | Greece declines; no transfer |
| Withdraw | Rome offers an 11 Gold gift; Greece asks Rome privately to withdraw | Rome's communication job withdraws; no transfer |
| Deadline | Rome offers a 13 Gold gift; Greece reads and leaves it unanswered | Host expires the proposal; no transfer; gameplay resumes |
| Gift | Rome offers 17 Gold with nothing requested in return | Greece accepts the one-sided gift |
| Resource bargain | Rome gives 1 Silk and 2 Iron; Greece gives 1 Gold Ore plus a negotiated price | Three counters change 4 GPT to 3 GPT + 10 Gold, then 2 GPT + 20 Gold, then 2 GPT + 25 Gold; Rome accepts with no counters remaining |
| Friendship | Rome offers a Declaration of Friendship | Greece accepts the engine agreement |
| Embassy in Rome | Rome offers its Accept Embassy item | Greece gains an embassy in Rome; Rome has none in Athens yet |
| Embassy in Athens | Rome requests Greece's Accept Embassy item | Mutual embassies unlock the treaty prerequisites |
| Greek access | Rome offers its Open Borders | Greece may enter Roman territory; Rome still lacks reciprocal access |
| Funded treaties and cities | Rome gives Antium and the exact research signing cost; Greece gives Sparta and Open Borders; both sides include matching Research Agreement and Defensive Pact terms | Greece accepts; Rome funds both signatures, cities exchange owners, and reciprocal borders and both treaties take effect |

The agents inspect unavailable-action reasons while establishing prerequisites,
read immutable terms before acting, and retain concise sourced economy and
relationship notes. A social promise never counts as an enacted engine deal.
Completed proposals determine the next step, so fresh jobs and host restarts do
not depend on an agent remembering what it did. Wrong terms, extra counters or
an unexpected terminal outcome produce `scenario.phase=FAILED`; agents are told
to record the failure and continue ordinary turns without replacement deals.

The city exchange buys directly from each city's founder. Unciv automatically
keeps these cities; no liberation popup is expected in this fixture. If a city
disposition is present, the player must inspect and resolve it. Tests for
third-party-founded cities and their Keep/Liberate choices remain in the engine
and focused bilateral suite.

## Verify an agent run

Run this while the host is running or after stopping it:

```sh
uv run civ-agent match verify-trade-cycle --state-dir /tmp/civ-trade-cycle-1
```

It reads the atomic `exploration.json` checkpoint and the scheduler's round
snapshots. It writes `verification/trade-cycle.json`, prints every step's
`PASSED`, `PENDING` or `FAILED` status, and returns 0 only when all checks pass.
Exit 1 means incomplete or failed objectives; exit 2 means unreadable or
incorrect input. `--checkpoint PATH` selects another checkpoint from this match.
Neither player credentials nor model transcripts are required.

The audit checks all canonical versions and prices, consent and engine outcomes,
unchanged turn ownership throughout each negotiation, terminal paths, and the
absence of extra or open proposals. It compares the native save against the
resource/GPT contract, embassies, friendship, research and defense flags,
directional and reciprocal borders, exact city ownership and unresolved city
choices. The round preceding the final deal must show that Greece needed the
immediate research funding. Both players must then complete two further rounds.
Native durations decrease according to Unciv's per-player accounting; they need
not remain identical across the two saved treaty copies.

Instant-only gifts and past peace negotiations leave durable engine outcomes
even after their native trade entries disappear. Those checks use the host's
committed receipts. The scripted test also measures exact gold changes at each
settlement. The audit does not infer past gold transfers from a final treasury
that also includes ordinary turn income.

For reasoning and memory, inspect `agents/Rome/`, `agents/Greece/` and their
relationship/economy files. To inspect the actual world in the desktop UI:

```sh
uv run civ-agent host inspect --state-dir /tmp/civ-trade-cycle-1
```

## Scripted verification and recovery

This runs the same fixture through authenticated HTTPS without launching models:

```sh
./gradlew verifyTradeCycle -PtradeCycleDir=/tmp/civ-trade-cycle-scripted-1
uv run civ-agent match verify-trade-cycle --state-dir /tmp/civ-trade-cycle-scripted-1
```

Choose a fresh output directory. The probe retains `exploration.json`, a native
checkpoint after each named step, round snapshots, `verified-final.json`, and
a separate `rollback.json`. It checks balance changes, exact clauses, consent
off turn, blocked gameplay during negotiations, directional effects, city
ownership and loss of seller authority. It restarts during an unanswered offer
and a resource counteroffer, confirms that deadlines and versions survive,
replays final acceptance before and after restart without charging twice, then
restores a copy to round zero and verifies initial ownership, balances and an
empty ledger. The deadline is advanced by a test clock; live deadlines remain
ordinary wall time. The probe also runs in the Kotlin integration suite.

The existing negotiation, peace and bilateral regression probes cover stale or
forged versions, an attempted fourth counteroffer, invalid treaty shapes,
unavailable resources/cities, privacy, unattended timer expiry and checkpoint
failure injection. Together these exercise the implemented trade surface.
Third-party clauses, city-state trades, mods, and an enacted-agreement
expiration/cancellation workflow remain outside the current implementation.
Scripted success is separate from a successful run by live agents.
