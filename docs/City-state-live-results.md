# City-state live acceptance

The September 19, 2026 exercise passed. Rome, Greece and France completed three
rounds against native Geneva and Sidon in a prepared Vanilla position. Nine
gameplay activations and three chat jobs all used **gpt-5.6-luna, medium reasoning**.
The host and agent services restarted after round one; the same match reached
native turn 3, handoff 9, then stopped at its configured turn budget. All services
are stopped. All **24 artifact checks** passed.

## Observed results

| Player | Native result |
| --- | --- |
| Rome | Chose a 500 Gold gift to Geneva, completed Give Gold and reached 85 influence; funded the 200 Gold Gems Mine, gifted a Warrior for 5 influence, conducted the Great Merchant mission for 234 Gold and 30 influence, then pledged protection |
| Greece | Chose a 1,000 Gold gift to Geneva, reached 135 influence and replaced Rome as ally; pledged protection to Sidon and completed its quest |
| France | Took 60 Gold tribute from Sidon with a native 15 influence loss; observed the cooldown on later turns |
| Greece and France | Greece answered the native protected-city-state incident by siding with Sidon; both players reaffirmed peace in chat, while native diplomatic penalties remained |

Geneva's final ally was Greece. Roman influence decayed from 120 to 117; Greek
influence decayed from 135 to 133.5. Protection and its countdowns persisted, as
did consumed units, the Mine, quest completion, tribute effects and all archived
gameplay receipts. The incident response is also backed by the host's durable
communication receipt and native `BulliedProtectedMinor`/`SidedWithProtectedMinor`
modifiers.

Every major started with at least 5,000 Gold, enough for the guided objectives.
The fixture supplied native quest assignments, a Merchant and giftable Warrior in
Geneva, and a French force near Sidon. Models selected gift amounts and diplomatic
responses. The runner issued no gameplay commands on their behalf. War/peace and
Worker tribute were exercised separately in native/HTTPS and desktop checks;
the live players remained at peace.

## Artifacts

Local evidence is in `/tmp/unciv-city-state-live-20260919-1`:

- `initial-game.json`, final `exploration.json`, and `city-state-live-verification.json`.
- `agents/{Rome,Greece,France}/turn_*`: decisions, receipts and observations.
- `agent-services/*/activations` and `chat-jobs`: model settings, conversation IDs,
  completion and handoff records.
- `runner/history` and `runner/run.json`: scheduler history and budget stop.
- `before-restart.json`: first-round checkpoint baseline, copied immediately after
  restart and before resumed gameplay; first-run logs establish the earlier stop.
- `city-state-live-prompts.json`: guided roles; private memory and chat are retained
  in the match directory.

The first and resumed runner logs are
`/tmp/unciv-city-state-live-1.log` and
`/tmp/unciv-city-state-live-resume-1.log`.

## Desktop comparison

`/tmp/unciv-city-state-desktop-20260919-2` contains 13 rendered screenshots and a
`COMPLETE` marker. All 13 were inspected. Actual native controls exercised Gold
gifts, improvement funding, protection, the Merchant mission, tribute, war and
peace. Screens show the gift amounts and influence, alliance, native mission
notification, tribute willingness, protection withdrawal cooldown, disabled peace
during the declared-war cooldown and the subsequent peace treaty.

The comparison uses isolated saves. The war cooldown is advanced with ten native
diplomacy-manager ticks. It does not show ten complete desktop rounds, a native
unit-gift click, Worker tribute execution, or every quest tooltip. Those actions
and effects have separate engine/HTTPS tests. The first desktop attempt in the
directory ending `-1` stopped because the test selector omitted the Merchant
button's consumption icon; the corrected selector passed in `-2`.

## Reproduce

Use fresh directories and an authenticated local model runtime:

```sh
./gradlew generateCityStateLiveFixture -PcityStateLiveDir=/tmp/city-state-live-new --console=plain
uv run python -m tests.live_city_states --state-dir /tmp/city-state-live-new --port 8941 --max-turns 1 --max-invocations 30 --max-seconds 3600 --turn-timeout 600
cp /tmp/city-state-live-new/exploration.json /tmp/city-state-live-new/before-restart.json
uv run python -m tests.live_city_states --state-dir /tmp/city-state-live-new --port 8941 --resume --max-turns 3 --max-invocations 30 --max-seconds 3600 --turn-timeout 600
uv run python -m tests.verify_city_state_live /tmp/city-state-live-new
```

The wrapper starts the host and three services, fixes all model settings, runs the
ordinary scheduler and cleans up its processes. Ports 8941–8944 must be free.
The verifier checks this guided outcome, including Greece's observed decision to
side with Sidon; another permitted response requires reviewing that run's evidence
and adjusting the expected outcome explicitly.

For desktop acceptance, use an available graphical display:

```sh
./gradlew runCityStateDesktop -PcityStateDesktopDir=/tmp/city-state-desktop-new --console=plain
```

These prepared positions establish the stated milestones, persistence and native
effects. They do not establish an unrestricted full game, live crash recovery, all
quests, expansion mechanics, or an embedded human desktop controller. Automated
tests separately inject failures before and after checkpoint replacement.
