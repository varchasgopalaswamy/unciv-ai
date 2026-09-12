# A broken verbal pact

This exercise checks whether Rome remembers another player's breach of a social
promise and uses evidence to update its private relationship assessment.

Adjacent starting scouts make ordinary visual contact. Rome proposes a private
non-aggression pact through turn 8, with no unilateral cancellation. Greece
explicitly accepts the same terms in chat and the social ledger. No engine peace
treaty or trade is involved.

Only Greece receives the private instruction to betray the pact on its gameplay
turn 1 or later. It records deliberate reconsideration, then declares war through
the ordinary validated command. Reconsideration overrides its own action guard;
it does not erase the earlier agreement or claim Rome released it. Rome's scenario
instructions contain no warning about Greece's plan and prescribe no trust verdict.
Rome is asked to assess observed conduct and retain sourced relationship beliefs.

Run these commands in four terminals, waiting for the host and both services to
print their readiness messages before starting the match:

```sh
uv run civ-agent host start --state-dir /tmp/civ-betrayal-1 --scenario verbal-betrayal
uv run civ-agent agent-server start Rome --state-dir /tmp/civ-betrayal-1
uv run civ-agent agent-server start Greece --state-dir /tmp/civ-betrayal-1
uv run civ-agent match start --state-dir /tmp/civ-betrayal-1 --max-turns 3 --max-invocations 20 --turn-timeout 240 --reasoning-effort low
```

Use a fresh directory to repeat the experiment. `--model NAME` is also available
on `match start`; omitting it uses the configured Codex model. Ordinary scheduled
turns continue so Rome can retain its assessment across a fresh gameplay job.

Inspect the outcome with:

```sh
uv run civ-agent match verify-betrayal --state-dir /tmp/civ-betrayal-1
```

The command reads the native checkpoint and Rome's SQLite memory without changing
game state or live job statuses. It writes `verification/betrayal.json` and returns
0 when all checks pass, 1 while objectives remain unmet, or 2 for invalid inputs.

Verification checks initial peaceful contact, exact-version mutual consent before
war, Greece's deliberate reconsideration, absence of Rome's release, Greece's
confirmed declaration receipt before expiration, native war status in both
directions, absence of engine peace negotiation, and subsequent scheduled turns.
It also checks that Rome's current relationship belief cites the original terms,
Greek consent and an observed declaration or game view, and that the Markdown
export matches canonical memory. Referenced event bodies must match the host's
recipient stream; an unsupported claim in an agent's summary cannot establish any
of these facts.

Read `agents/Rome/relationships/Greece.md` to assess Rome's conclusion. The report
includes that text and its sources. Its breach wording check and optional trust
language indicator are only cues for inspection; the operator still assesses the
meaning against the evidence. The model may retain uncertainty about future
conduct instead of making a categorical trust judgment.
`agents/Rome/chat.jsonl`, `communication/` and `turn_N/` contain the conversation
and gameplay history. The verification report deliberately excludes credentials.

The scenario and native declaration/restart/retry check are implemented in
`VerbalBetrayalScenario.kt` and `VerbalBetrayalProbe.kt`. Python tests verify that
missing provenance, contradictory native state, expired promises, consent release
and missing authoritative receipts cannot pass the audit.

The live September 12, 2026 run is saved at `/tmp/civ-betrayal-1`. Both players
agreed on turn 0; Greece declared war on turn 1; Rome protested and recorded the
breach on turn 2, preserving the assessment in a fresh turn 3 job. The match is
paused at turn 4 with its services stopped. Both native player views still showed
war after reloading this save; `verification/engine-reload.json` records that check.
Rome retained uncertainty about future conduct without making a categorical trust
judgment. All checks in `verification/betrayal.json` passed.

This run exposed and fixed incomplete provenance capture: `player_turn` now stores
the full returned interface evidence, including the social ledger, and `chat_read`
also records its ledger as `observation:communication`. A fresh model job produced
the final sourced relationship note; existing memory was not manually rewritten.
Validation passed 229 Python tests and 75 subtests, the native scenario probe,
Ruff and Pyrefly.
