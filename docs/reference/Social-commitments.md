# Social commitments and the host rule boundary

Social promises model communication between human players. The host records exact
words, parties, consent and subsequent messages. Players interpret ambiguous
conditions, discuss disagreements and decide whether to keep their word. The host
does not adjudicate a breach, impose penalties, perform promised actions, or require
withdrawal before a conflicting native-legal action.

## Nonblocking advice

`player_turn.socialAdvisories` quotes the reader's currently accepted `kind=peace`
promises: proposal/version, participants, terms, conditions and stated expiry.
It identifies declarations, unit/city attacks and war trades as potentially
relevant actions. The compact agent overview retains this advice. War options
provide the reader's advice under `war.socialAdvisories`; trade reads include
`socialAdvisories` for declaring or sponsoring a war against another participant.
Advisories never contain another player's private promises.

These are possible conflicts, not verdicts. Conditions remain unparsed text;
"unless provoked" does not need an executable definition. Other kinds of promises
remain available through the communication ledger for human or agent reasoning.
Players may ask for clarification, explain a change of intent, disagree, or act.

Advice is separate from native action availability, native declaration warnings,
game-state revisions and consent to a stored trade version. A new or changed social
promise does not invalidate a previously inspected native action or war deal.
Accepted promises remain in the ledger after a conflicting action; an engine
receipt records what happened without automatically retracting the speaker's words.
Optional `withdraw` and `reconsider` messages remain available.

The obsolete `socialVersion`, `peaceVersions` and `warConsentVersions` fields are
accepted only for old request/receipt and checkpoint decoding. They have no effect
on gameplay or consent.

## Audit boundary

| Check | Treatment |
| --- | --- |
| Social peace blocks on direct declarations, unit attacks and city attacks | Removed; advice only |
| Social peace blocks on declaring or sponsoring a war trade | Removed; reader-private advice only |
| Author's consent invalidated by a changed social promise | Removed; exact native proposal consent remains |
| Native peace treaties, declaration eligibility and native settlement-promise confirmations | Retained from shared engine operations |
| Visibility, ownership, unknown-target filtering and private communication access | Retained; the host may enforce privacy beyond native UI checks |
| Authentication, player turns, stale native observations, exact retries and atomic recovery | Retained as execution safeguards |
| Private plan/evidence freshness | Retained when an agent elects to cite that plan; it cannot require social withdrawal or forbid an independently chosen legal action |
| Negotiation holds, deadlines and counteroffer budgets | Existing operator-configured orchestration; social conversations do not acquire these holds |
| Guided scenario prices and military objectives | Exercise instructions only; no global game rule |

The audit reviewed current action, diplomacy/trade and communication paths. It
found no other social-promise legality gate. It also identified existing missing
native controls: the context menu's explicit Next Turn override for nonmodal
picker prompts, and post-victory One more turn. These are API coverage gaps, not
permission to bypass modal capture/popups or all pending choices. They are tracked
in the [coverage ledger](Player-api-coverage.md); this audit does not establish
complete desktop/API parity.

## Regression coverage

- `VerbalBetrayalProbe`: direct declaration despite an accepted, unexpired promise,
  visible nonblocking advice, unchanged conversation, exact retry and restart;
  unit and city attacks during a social ceasefire without reconsideration.
- `CommunicationProbe`: private advice, no game-revision or option changes from
  consent, optional reconsideration, superseded terms and coordinated recovery.
- `ThirdPartyTradeProbe`: paid, sponsored and joint declarations with active
  promises on both signers; renewed promises during negotiation; private advice,
  exact native effects and continued enforcement of actual peace treaties.
- Python gateway/observation regressions: advice reaches the model without
  becoming a native-state dependency; independent hostile choices remain legal
  after social consent; native peace still invalidates stale attacks.

The earlier live war and lifecycle reports record the rules in force when those
runs happened. The [September 19 social-promise live exercise](../archive/reports/Social-promises-live-results.md)
separately verified advisory delivery, player discussion, war with unchanged
social consent, visibility and restart recovery over three complete rounds.
It included no unit attacks or city bombardments; those remain scripted checks.

Validation on September 19, 2026:

- JVM full run: 77 of 78 tests passed. The remaining production-scenario probe
  still expected mandatory reconsideration. After updating it, all five targeted
  launch and social-promise checks passed, together with formatting/style checks.
- Python full run: 412 of 413 tests and all 90 subtests passed. The remaining
  assertion caught an overlong scheduler prompt. After shortening it, all 31
  scheduler tests passed. The 56 focused gateway/recovery/trade/observation tests
  also passed.
- Ruff lint/format and Pyrefly passed (six existing missing-stub warnings).
  Validation used source checkouts; no distribution build was run.

Local records: [JVM full run](../../build/verification/social-promises-jvm-full-20260919.json),
[JVM corrected rerun](../../build/verification/social-promises-jvm-rerun-20260919.json),
and [Python checks](../../build/verification/social-promises-python-20260919.json).
