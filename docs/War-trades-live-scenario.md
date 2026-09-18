# Live scenario: Three-Cornered War

Status: both scripted engine scenarios pass. The September 18 live runs enacted
all three declaration forms and verified restart preservation, but both exhausted
their wall-time budgets before finishing the second continuation round. See the
[live results and evidence](War-trades-live-results.md).
The launcher accepts `war-paid` and `war-joint` alongside `trade-cycle`.

Use guided diplomatic milestones with live agents negotiating prices, reading
their own observations and choosing military orders. The exercise has two short
runs from the same prepared opening: paid war followed by joining, then a joint
declaration. This gives each native declaration type an eligible starting position
without spending a long live run waiting for peace treaties to expire.

This is the operator's complete playbook. Agents receive only their own current
objectives. Neither shared scenario labels nor France's instructions may disclose
Rome and Greece's pending deals, prices or private commitments with each other.

## Prepared opening

- Rome, Greece and France occupy Human slots, in that turn order.
- Vanilla, Quick speed, a small triangular land map, no city-states, barbarians,
  ruins or mods. All three have met, but retain ordinary fog of war.
- Everyone starts at peace, with no engine treaties or defensive pacts.
- Each has 500 Gold, a capital and a frontier city, sustainable income, basic land
  technologies and a small army. Capitals sit behind the frontier so the opening
  negotiations do not immediately eliminate a participant.
- The exercise asks for a limited frontier war. Capturing capitals is outside its
  objectives; agents still choose movement, defensive positions and legal attacks.

## Run A: paid war, then joining

Rome wants pressure on France while initially remaining neutral. Greece wants
payment and eventual Roman participation. France wants to protect its frontier
and understand the changing diplomatic situation.

This variant also begins with a pre-existing social non-aggression promise between
Greece and France, stored as an ordinary accepted communication record. Only those
two players receive that history. This is prepared background, not a claim that
live agents negotiated it. Rome's opening instructions do not depend on reading
or detecting the private promise.

| Milestone | Private player objectives | Observable result |
| --- | --- | --- |
| Offer paid war | On its first turn, Rome offers 30 Gold for Greece to declare war on France, with a private spending limit of 60. Greece privately requires at least 40 Gold. | The original offer is pending. No payment or declaration occurs yet. |
| Reconsider, counter and settle | Greece explicitly reconsiders its accepted promise to France, then counters at a price in the 40–60 Gold range. Publishing a counteroffer itself records consent, so reconsideration must happen first. Rome reads and accepts the counter; both players inspect their own warnings. | The agreed payment and Greece–France war occur once. Rome remains at peace with France. France receives the reconsideration and native declaration, without automatic access to the hiring contract. |
| Recruit Rome | During a subsequent Greek gameplay turn, Greece offers Rome 10–20 Gold to join the existing war. Rome is willing to join within that range. | Rome declares on France through a one-sided war clause. The engine reports that Rome joined Greece; France now faces both. |
| Continue after restart | Stop at a completed round after both deals, preserve the audit snapshot, restart the host and services, and resume for two more rounds. | The agents acknowledge their own alerts, update sourced plans, continue military play and preserve completed deals across fresh jobs. |

Prices within the specified ranges are choices for the agents. Acceptance must
refer to the final inspected counteroffer. A refusal or expired proposal is a
valid engine result but leaves the corresponding live feature objective unmet;
the report must say so instead of calling the run a success.

The social promise is a deliberate exercise of the host's reconsideration rule.
France may learn that Greece revoked its promise and may infer Roman involvement.
It may also obtain information through voluntary chat. Record those sources;
privacy does not require France to remain unaware of things it can legitimately
learn or infer.

## Run B: joint declaration

Start a separate match from the same prepared peaceful opening, with fresh private
memory. Omit Run A's pre-existing social promise. Rome and Greece negotiate a joint
war against France, including any agreed immediate payment. The one accepted
proposal must contain a `WarDeclaration` against France from **each** negotiator.

France receives only its defensive objective before war, followed by its native
joint-war notices. Run two further rounds so all three agents must process the
changed relationships and act on the consequences. Never tell France the hidden
contract through its scenario instructions.

## Audit and recovery evidence

The operator's report should check native saves, committed receipts and each
player's own event stream, together with the reasoning and memory records:

- Each declaration's author, target, agreed price and accepted proposal version;
  actual native war status and gold transfer at settlement.
- Rome's neutrality after the first paid war, Roman entry after recruitment, and
  both declarations from the single joint-war acceptance in Run B.
- Greece's explicit reconsideration in Run A and both players' treatment of the
  final counteroffer and declaration warnings.
- France's full native notices and its knowledge sources. Payments and private
  proposal identifiers must not appear through an unauthorized observation.
  Native joining and joint-war notices legitimately identify both allies.
- Unchanged turn ownership during each negotiation, subsequent scheduled
  handoffs, resolved alerts and sourced updates to private plans.
- Preserved proposals, outcomes and receipts across restart; no duplicate payment
  or replacement deals caused by fresh agent conversations. Compare settlement
  snapshots rather than estimating old transfers from a treasury changed by income.

After archiving a completed run's evidence, an optional coordinated rollback to
round zero can exercise restoration of all three players' memory and game state.
Keep that as a separately reported recovery exercise so its abandoned timeline
cannot be mistaken for another live settlement.

Pending-offer restarts, exact request replay, lost responses, injected checkpoint
write failures, forged terms and paired hidden-world comparisons already have
scripted regression coverage. This live exercise adds actual agent use and
post-restart continuation; it does not replace those checks. With all three
players mutually known, it also cannot demonstrate hiding a fourth civilization
or an unknown defensive-pact partner.

## Implementation and run limits

The prepared opening has paid/joining and joint variants, private current objectives
through both gameplay and communication contexts, and a checkpoint verifier.
Progress follows committed events and permitted observations; a global phase derived from a private offer
must not reveal that offer to France.

Start with a limit of eight complete rounds and 96 model invocations for Run A,
and four rounds and 48 invocations for Run B. Allow up to one hour per run and
600 seconds per activation, with downtime included in the wall-time limit.
The first live attempt used 300 seconds per activation; two Roman jobs timed out
before handoff, so the activation limit was raised without extending the overall
wall-time or invocation budgets. These limits are not cost estimates.
Use the players' configured models.
Report unmet milestones before extending limits. No package-distribution build
is required for preparing or running the exercise.

See [third-party war trades](Third-party-war-trades.md) for the implemented
contract and [recovery commands](Chat-memory-and-recovery.md#interruption-and-emergency-restore)
for the existing pause, restart and rollback workflow.

## Launch and inspect

Run the scripted engine probe before spending model calls:

```sh
./gradlew spotlessApply test --tests uncivai.GameLaunchIntegrationTest --tests uncivai.CommunicationIntegrationTest
./gradlew verifyWarScenario -PwarScenarioDir="$PWD/build/generated/war-check-1"
uv run pytest tests/test_verify_war_scenario.py tests/test_launch_host.py
```

The probe needs a fresh output directory. It checks both production openings,
blocked consent before reconsideration, direct/joining/joint declarations, immediate
balances, player context privacy, turn ownership, and checkpoint reloads. It runs
no models. The test suite also checks scenario identity on resume.

For Run A, use a fresh directory. Run each of these long-lived services in its own
terminal (set `WAR_MATCH` to the same absolute path in each terminal):

```sh
export WAR_MATCH="$HOME/.local/state/unciv-host/three-cornered-paid"
uv run civ-agent host start --state-dir "$WAR_MATCH" --new --scenario war-paid
uv run civ-agent agent-server start Rome --state-dir "$WAR_MATCH"
uv run civ-agent agent-server start Greece --state-dir "$WAR_MATCH"
uv run civ-agent agent-server start France --state-dir "$WAR_MATCH"
```

In another terminal, begin with a one-round checkpoint so that the restart can
happen promptly after the two settlements:

```sh
uv run civ-agent match start --state-dir "$WAR_MATCH" --max-turns 1 --max-invocations 96 --max-seconds 3600 --turn-timeout 600 --verbose
uv run civ-agent match verify-war-scenario --state-dir "$WAR_MATCH"
```

Before continuation, the verifier intentionally returns nonzero for missing
further rounds and the restart archive. Inspect each milestone in the printed
report path. If either is incomplete, report why; resume one additional round
at a time using increasing `--max-turns`, reserving two rounds within the total
limit of eight. Do not silently retry failed or expired negotiations.

Once both milestones pass at a completed round (Rome is next), pause and preserve
the checkpoint. `match pause` is safe even when the scheduler has stopped at its
round limit:

```sh
uv run civ-agent match pause --state-dir "$WAR_MATCH"
umask 077
cp -n "$WAR_MATCH/exploration.json" "$WAR_MATCH/before-restart.json"
```

Stop the host and three agent services with Ctrl-C. Restart the same four commands,
**omitting `--new` from the host command**. Resume with `--max-turns` equal to the
archived completed round plus two (for example, `3` if archived after round `1`):

```sh
uv run civ-agent match resume --state-dir "$WAR_MATCH" --max-turns 3 --verbose
uv run civ-agent match verify-war-scenario --state-dir "$WAR_MATCH" --before-restart "$WAR_MATCH/before-restart.json"
```

Limits and elapsed wall time are cumulative across resume. Archive process logs
as evidence of the restart; two saves alone cannot prove a process restarted.

For Run B, stop Run A's services to release the default ports. Use a separate fresh
`WAR_MATCH`, launch the host with `--new --scenario war-joint`, and start the same
three agent services against that directory. Then run:

```sh
uv run civ-agent match start --state-dir "$WAR_MATCH" --max-turns 4 --max-invocations 48 --max-seconds 3600 --turn-timeout 600 --verbose
uv run civ-agent match verify-war-scenario --state-dir "$WAR_MATCH"
```

The verifier writes an owner-only `verification/war-scenario.json`. It checks
canonical terms, negotiated ranges, consent/version receipts, stable turn ownership,
exact immediate gold changes and native wars in host-only settlement snapshots,
France's trade-event isolation, and continued rounds. Run A additionally requires
preserved proposals, outcomes and receipts across the supplied restart archive.
Settlement snapshots are stored only in operator checkpoint audit records; player
responses, events and receipt lookups do not expose foreign balances or wars.
Older checkpoints without these snapshots cannot pass this verifier.

A passing automated report is not the whole live review. Inspect histories and
private memory for final-term/warning inspection, full alert handling, sourced plan
updates and the provenance of France's knowledge. Retain those findings alongside
the checkpoint report. No live run is claimed until these checks have been performed.
