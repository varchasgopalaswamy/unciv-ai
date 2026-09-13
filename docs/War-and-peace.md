# War declarations and peace negotiations

Peace is a term in the [shared asynchronous trade system](Asynchronous-trade.md).
It uses the same proposal IDs, immutable versions, consent, events, receipts and
checkpoints as gold and resource trades. There are no separate host peace commands.

## Observations and actions

`player_turn.diplomacy.knownCivilizations` exposes met major civilizations,
`status`, `atWar`, `peaceTreatyTurns`, war availability and confirmation text,
peace-negotiation eligibility, and current bilateral agreements. Unknown defensive
pact partners remain unnamed in declaration warnings.

Declare war through `player_command` with `kind: DECLARE_WAR`, the known
civilization ID as `target`, and `confirmWar: true`. This requires a fresh
`player_turn` that presented the confirmation, the active gameplay turn and all
native war eligibility checks. The receipt contains the opponent's response.
The opponent receives a `WarDeclaration` alert with the full leader message and
acknowledgement choices; reading and acknowledging it does not declare war again.

Use `trade_options(partner)` to inspect peace eligibility and the exact current
terms. The ordinary engine negotiation cooldown still applies. During war,
`requiresPeaceTreaty` is true: **both sides must contain exactly one offered
`Treaty` named `Peace Treaty`, amount 1, with the engine's duration**. Gold, GPT
and luxury/strategic resources may accompany it. Other treaties, one-sided peace,
duplicate items and fabricated durations are rejected. At peace, a new peace
treaty is unavailable, while ordinary economic trade remains available.

For example, `trade_offer` can publish the following terms. Durations are
illustrative; read the actual values from `trade_options`:

```json
{
  "partner": "Greece",
  "give": [
    {"type": "Treaty", "name": "Peace Treaty", "amount": 1, "duration": 10},
    {"type": "Gold", "name": "Gold", "amount": 25, "duration": -1}
  ],
  "receive": [
    {"type": "Treaty", "name": "Peace Treaty", "amount": 1, "duration": 10}
  ]
}
```

The other player reads `trade_read(proposal)` and calls `trade_decide` with only
the proposal ID and `accept` or `decline`. The author can withdraw it. A
counteroffer uses `trade_offer` with the inspected proposal ID and replacement
terms, requiring fresh consent. Python attaches versions, credentials and retry
IDs. Neither player reconstructs an acceptance from prose.

## Scheduling and recovery

Both players can negotiate and consent during either gameplay turn, including
scheduler gaps. Publishing a proposal never blocks END_TURN. Communication jobs
receive events without model polling and cannot move units or take ordinary
gameplay actions. No active-player switching is involved.

Acceptance rechecks relations, rules, duration and affordability under the game
executor. Native `TradeLogic` applies the complete stored deal: both directions of
peace, treaty restrictions, gold payments, recurring obligations, resource access
and other normal peace effects. Consent, engine outcome, acknowledgement content,
events and retry receipt commit in the same checkpoint. A competing expense can
invalidate the proposal without enacting peace or transferring anything.

Accepted peace invalidates other pending proposals between the pair, both players'
observations and hostile-action policy versions. An attack planned before peace
fails without damage. An unrelated greeting leaves gameplay valid. Gameplay jobs
refresh affected observations and continue; the scheduler does not restart the
whole turn. Native UI requests for peace between these players are cleared as
obsolete; unrelated requests are preserved. Imported UI trade requests remain
readable unsupported blockers, with hidden city/third-party identifiers redacted.
They are not another negotiation interface.

Exact acceptance retries survive restart without repeating transfers. The existing
operator rollback restores the native war state, proposal versions, receipts,
events and all player memory together under a new epoch. Use a new save for this
schema; no compatibility layer is maintained.

## Prepared asynchronous peace exercise

Start the host, two agent services, then the scheduler in separate terminals:

```sh
uv run civ-agent host start --state-dir /tmp/civ-peace-1 --scenario async-peace
uv run civ-agent agent-server start Rome --state-dir /tmp/civ-peace-1
uv run civ-agent agent-server start Greece --state-dir /tmp/civ-peace-1
uv run civ-agent match start --state-dir /tmp/civ-peace-1 --max-turns 3 --max-invocations 32
```

Wait for each service to report ready. The fixture starts after the war cooldown,
with viable capitals, 1,000 gold each, surplus resources and adjacent Warriors.
The armies should hold position. Rome offers mutual peace and one Silk for four
GPT; Greece counters with mutual peace, two GPT and twenty immediate gold for
that Silk; Rome accepts through communication work. Both then verify the deal,
record sourced economy/relationship notes and continue scheduled turns.

Inspect with `civ-agent trade read`, `trade options`, native saves and the files
under `agents/Rome` and `agents/Greece`. Canonical durations record the original
deal; native durations tick down with each civilization's turns. Check actual
bilateral peace and obligations, not just model claims. See the trade runbook for
human inspection commands and the normal model/reasoning options.

## Verification

`WarPeaceRunIntegrationTest` covers contact through movement, declaration and its
message, natural cooldown, off-turn decline/counteroffer/acceptance, restarts before
and after acceptance, native payment/treaty checks and continued scheduled rounds.
`PeaceTradeProbe` covers the prepared scenario, invalid terms, lost funds,
concurrent proposals, an old attack, native resource/GPT effects, exact retry,
round rollback and checkpoint failures before/after replacement. Existing combat
verification also makes peace through this shared path.

Python HTTPS scheduler/service tests exercise the same counteroffer/acceptance
flow for both economic and peace terms while gameplay recovers from a concurrent
memory conflict. Engine tests check off-turn eligibility without loosening the
desktop mutation fence. These are scripted tests; live-model acceptance remains
a separate exercise. Broader trade and diplomacy/UI coverage is still incomplete.
