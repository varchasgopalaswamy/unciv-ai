# Local Decisions quality comparison — 2026-10-20

Winnow-12B is a plausible selective classifier for this project, especially for
memory preservation. It does not establish equivalent quality across all current
Decisions tasks. Its aggregate raw routing result nearly matches Luna, with a
substantial difference between incoming and outgoing messages. It trails on
memory relevance and city alternatives. Confidence semantics differ enough that
copying Luna's numerical threshold gives misleading comparisons.

This assessment concerns response quality. Timing and deployment choices do not
drive the recommendation.

## Inputs and scoring

The comparison used the current fixed `diplomacy-routing-v4` and
`player-advisories-v4` prompts from the working tree. Every instruction, input
string, option key and option description was preserved. The local adapter maps
`input` to `state`, named question lists to maps, and `choices` to `criteria`.
No prompt, temperature or model tuning was applied. The endpoints were
`Winnow-12B` at the supplied loopback URL and fresh `gpt-6-luna` Decisions calls.

- Routing: 168 classified cases in 46 scenario groups. Eleven additional cases
  take a protected deterministic bypass and were excluded from classifier scores.
- Advisories: 19 batches containing 42 labeled questions. One batch with four
  questions was rejected by both providers because two questions offer only
  `defer`, violating the minimum of two alternatives. The raw quality denominator
  is therefore 38 valid answers. This is a shared input defect, rather than a
  model-quality difference.
- Production: 24 distinct retained advisory requests, selected across task
  combinations and input-size extremes from a read-only stable ledger snapshot.
  Recorded Luna responses were reused only after checking model, policy, exact
  input hash, prompt hash and question names. These are agreement comparisons;
  the saved answers are not correctness labels.

All fixture labels are provisional assistant-authored judgments. Routing cases
are grouped context variations, including some derived from archived exchanges;
they are not 168 independent observed games. Advisory labels include subjective
relevance and preference judgments. These scores measure agreement with those
labels, not gameplay strength. No gameplay was executed.

## Raw answer quality

These scores use each question's selected choice, before confidence rejection or
policy fallback. Routing's two selected answers are combined using the existing
project policy at threshold zero; explicit alternative acceptable labels remain
acceptable.

| Task | Winnow-12B | gpt-6-luna |
| --- | ---: | ---: |
| Incoming message routing | 65/88 (73.9%) | 78/88 (88.6%) |
| Outgoing draft routing | 59/80 (73.8%) | 47/80 (58.8%) |
| All routing | 124/168 (73.8%) | 125/168 (74.4%) |
| Memory preservation | 4/4 | 4/4 |
| Belief reconsideration | 4/4 | 3/4 |
| Memory relevance and priority | 5/8 | 7/8 |
| Early review | 5/6 | 5/6 |
| City advice | 10/16 | 13/16 |
| All valid advisory answers | 28/38 (73.7%) | 32/38 (84.2%) |

Routing's group-weighted mean label agreement is 74.3% local and 75.7% Luna.
On the fixture's reserved holdout groups, raw routing agreement is 55/80 versus
58/80; advisory agreement is 17/21 versus 18/21. The holdout was reserved by the
fixture author, and is not an external blind evaluation.

The city result needs separation: deciding **whether to change** matches 7/8
labels locally versus 8/8 for Luna. Selecting an alternative **if a change were
needed** matches 3/8 versus 5/8. Some preference labels expect `defer`, and are
more debatable than concrete change/no-change labels.

Concrete differences include:

- Winnow asks for attention on several already answered incoming requests,
  including the settled Sugar-for-Cotton question and an explicitly declined
  revised price. Luna recognizes those closures.
- Winnow correctly recognizes several already delivered outgoing offers and
  price revisions as redundant where Luna selects batching.
- Winnow correctly retains a belief about no visible Greek ships when the new
  observation is explicitly incomplete. Luna selects reconsideration with low
  confidence.
- Winnow omits a useful, nonurgent memory about a future luxury exchange and
  selects construction alternatives when the labeled answer is to defer.
- Both models miss some short outgoing deadlines. Raw routing contains 11
  harmful deferrals for Winnow and 16 for Luna; Winnow additionally has two
  harmful omissions. These counts use the fixture's scheduling labels.

## Confidence and selective use

Winnow documents `confidence = 1 - H(p)/log(K)`. In these captured binary Luna
answers, confidence equals `2 * max(p) - 1`, to floating-point precision.
Recorded multichoice Luna answers also fit the normalized-top formula to their
reported rounding. This description of Luna is an empirical observation from
the captures, not an official API guarantee.

For a binary distribution of 0.95 and 0.05, normalized-top concentration is 0.90
while Winnow's inverse entropy is approximately 0.714. Thus a literal 0.90 cutoff
is substantially stricter for Winnow. Neither quantity is measured correctness.

At each provider's native 0.90 confidence cutoff, Winnow accepts 2/38 advisory
answers and Luna 9/38, with no label errors among either accepted subset.
Winnow's routing results all fall back to immediate attention/delivery; Luna
produces 76 batched routes and one labeled harmful deferral. Winnow's absence of
harmful deferrals at that cutoff comes with no deferred or suppressed routes.

For a second comparison, the evaluator derives the same concentration score
from both providers' probability vectors:

```text
concentration = (K * max(p) - 1) / (K - 1)
```

Original responses remain intact. This is an algebraic comparison scale, not
empirical correctness calibration. At concentration 0.90:

| Result | Winnow-12B | gpt-6-luna |
| --- | ---: | ---: |
| Accepted advisory answers | 12/38 | 9/38 |
| Label errors among accepted advisory answers | 0 | 0 |
| Routing actions matching labels | 99/168 | 85/168 |
| Harmful deferrals | 9 | 1 |
| Harmful omissions | 1 | 0 |

This is the main quality caution for local message scheduling: similar overall
raw agreement does not imply equally useful uncertainty estimates. More of
Winnow's deadline mistakes survive a common probability-concentration gate.

An exploratory native inverse-entropy cutoff of 0.70 accepts 12 advisory answers
without an observed label error: six in calibration and six in holdout. Applying
0.70 to routing instead permits nine harmful deferrals and one harmful omission.
A 0.85 local routing cutoff leaves 37 batched routes with no observed harmful
deferrals or omissions, but was inspected after seeing the results. These are
diagnostic operating points, not validated production thresholds. The small
samples do not establish a zero-error rate.

The [official Decisions guidance](https://developers.openai.com/api/docs/guides/decisions#interpret-the-answers)
recommends application-labeled examples and the costs of false positives and
false negatives when setting thresholds. Task-specific calibration matters here;
algebraically translating confidence alone does not supply it.

## Production replay agreement

Nineteen of the 24 selected production batches returned local answers, providing
69 paired answers. The other five were rejected for context capacity and supply
no local choices to judge. They are excluded from the agreement denominator;
this service-level limitation does not drive the quality recommendation.
The selected batches were deliberately stratified by task combination and size,
so the following proportions do not estimate normal-play traffic.

| Question family | Agreements with recorded Luna answers |
| --- | ---: |
| Preservation | 30/31 |
| City focus change | 6/6 |
| City construction change | 6/6 |
| City focus alternative | 5/6 |
| City construction alternative | 0/6 |
| Early review | 3/6 |
| Retrieval inclusion | 2/4 |
| Retrieval priority | 1/4 |
| Total | 53/69 (76.8%) |

The distinction between city change gates and alternative rankings recurs in
real inputs: both models agree on every comparable city change gate, while
construction preferences differ on all six. Most such alternative answers have
low native confidence. Strong preservation agreement supports further local
evaluation; none of these agreements or disagreements establish correctness.
No production belief-reconsideration questions were present in this selection.

## Recommendation

Use Winnow as a candidate for selective memory preservation and simple belief
triage, with explicit uncertainty handling. The eight combined labels in those
categories are encouraging, but too few to establish broad reliability.
Its outgoing raw results are also promising and warrant a larger evaluation
that emphasizes deadlines and new information in repeated messages.

Keep the observed gap in incoming conversational closure, retrieval relevance
and city alternatives visible when deciding which tasks to move. A global switch
or a globally lowered confidence cutoff is not supported by this evaluation.
The useful next quality step is a larger manually reviewed, grouped corpus and
task-specific acceptance thresholds. Production settings were left unchanged.

## Reproduction and artifacts

The reusable [comparison runner](../../../scripts/compare_decisions_models.py)
freezes inputs and checkpoints raw responses under
`benchmarks/decisions-comparison-2026-10-20/`. That ignored private directory
contains `manifest.json`, `captures.json` and `results.json`. Provider credentials
are read in the trusted process and are not copied into artifacts. Production
requests do not consume new Luna calls, and the production ledger is never
opened for writing.

```sh
.venv/bin/python -m scripts.compare_decisions_models \
  --output benchmarks/decisions-comparison-2026-10-20 \
  --key-file /home/vgopalas/.local/state/unciv-host/auth/decisions.key \
  --production-ledger /home/vgopalas/.local/state/unciv-host/auth/decisions.sqlite3 \
  --production-samples 24
```

An exact rerun reuses completed captures. Fresh Luna requests had a conservative
project-rate input estimate of $0.1092708, bounded below the runner's $0.15 cap.
Validated fresh Luna usage was 162,813 input tokens and zero output tokens,
giving a measured-input project-rate cost estimate of **$0.0162813**.
The runner retains timing and transport diagnostics separately from the quality
assessment.

Validation: 84 focused tests passed, including adapter semantics, unchanged raw
confidence, probability validation, existing routing policy and concentration
threshold boundaries. Ruff lint/format and Pyrefly checks passed for the new
runner and its tests. Existing workspace edits and engine checkouts were left
intact.
