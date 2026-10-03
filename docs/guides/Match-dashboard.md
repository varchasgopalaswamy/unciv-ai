# Terminal match dashboard

Open the dashboard in another terminal while the game runs:

```sh
uv sync --locked
uv run civ-agent match watch --state-dir ~/.local/state/unciv-host/vanilla-full
```

Use the directory of the match you want to inspect. The Vanilla launcher prints
the matching command under `Dashboard`. The viewer can attach during a turn, before
startup has finished, or after a match has stopped. It requires an interactive
terminal. `NO_COLOR` enables a monochrome display. For container games it runs
from the saved image without network access; only SQLite coordination directories
and the control-request directory are writable. The game state and credentials
remain read-only, and all database connections use read-only mode.

To use an updated dashboard with an existing container game, add `--local`:

```sh
uv run civ-agent match watch --local --state-dir /path/to/match
```

This runs the installed viewer directly on your machine and reads the saved
databases using read-only connections. It requires no image rebuild or game
restart. Without `--local`, container games use their pinned viewer image, which
does not change when you update the checkout or rebuild images.

## What it shows

- Saved scheduler status, the current turn/player, stop reasons, and the age of the
  most recent records. A saved `running` status does not prove that a process is
  still alive; the record age helps identify a stalled or abandoned run.
  The turn's wall timer includes pauses; the turn table separately reports native
  gameplay and bookkeeping measurements.
- Player scores and controllers, including built-in AI and human players when the
  host records them. Agent rows distinguish gameplay from background bookkeeping.
  The player and strategy tables show each agent's reported model beside its
  civilization, including opening reviews before the first gameplay turn.
- Recorded input, cached-input, output, and reasoning-token counts. Cached tokens
  are part of input; reasoning tokens are part of output. Accounting can remain
  partial while a turn or its bookkeeping is in progress.
  Selecting a turn shows that turn's token breakdown above the activity feed.
- Assistant messages, confirmed actions, errors, and lifecycle changes. Repeated
  startup context and detailed tool activity are hidden unless verbose mode is on.
- Concise diplomatic messages, offers, counteroffers and outcomes in Activity,
  including communication outside the active player's turn. These are the actual
  shared communications, not private model reasoning. Long messages are shortened
  here; the Diplomacy tab shows their full text.
- A negotiation banner when a formal shared offer pauses gameplay. It identifies
  the participants, whose response is needed and the remaining host deadline.
  An elapsed deadline remains labeled as awaiting the host outcome until the host
  records it; the viewer never expires a deal itself. Native AI offers do not
  display a gameplay pause. The active player's row shows
  `Negotiating (model suspended)` after its service confirms that generation has
  stopped. Communication agents continue negotiating; gameplay resumes in the
  same conversation when the host reports the outcome.
- A Diplomacy tab grouping messages and offers by participants, with pending
  negotiations first and public chat in a separate conversation. Select a row to
  read the exchanged messages, exact directional trade terms, offer versions,
  additions/removals in counteroffers, decisions and failure reasons. Native
  enactment is distinct from social promises; the viewer does not interpret
  ambiguous promises or adjudicate whether they were broken.
- A Strategy tab showing each agent's latest completed adviser report, its source
  turn and the first turn whose gameplay preload included it. Select a civilization
  to read the recommendation, up to three competing plans and conditions for
  reconsideration. Each plan shows its objective, investment, projection, relative
  outcome and risks. A recommendation can leave the choice open. Short-, mid- and
  long-term next steps, diplomatic initiatives and brief outlooks for every enabled
  victory follow. Advice is not a record of adopted decisions.
  The last completed report stays visible while another review runs. Pending work,
  attempt counts, failures, duration and token usage appear alongside it.
  The main status area shows when active conversations are finishing before a
  shared review, then lists the remaining reviewers while play and chat are paused.
  Active reviews show whether the adviser is thinking, reading tools, drafting,
  validating or publishing. The selected civilization also shows its model and
  reasoning effort, tool-call count, report character count, time since the last
  model activity, and connection waits/retries. Counts describe observed activity;
  they do not estimate a completion percentage. Heartbeats and token-usage updates
  do not count as model progress. Progress is saved with the normal heartbeat,
  without extra model requests.
  Strategy has **Report** and **Live trace** views. Selecting a civilization with
  a pending review opens Live trace, which streams the model's exposed reasoning
  summaries, assistant messages/draft JSON, tool activity and connection notices.
  The draft is unvalidated; the Report view continues to show completed advice.
  The trace follows new text when scrolled to the bottom and keeps your reading
  position when you scroll up; `f` toggles following and Space freezes the viewer.
  Only already-filtered, credential-redacted history enters the trace. Raw model
  reasoning, startup context and full tool results are excluded. The preview is
  limited to 64 entries and 32,768 text characters, with truncation shown explicitly.
  If that civilization's next turn arrives before the report is saved, the main
  banner shows the review being awaited and its player row shows `Review wait`.
- Model connection warnings remain visible without verbose mode. A quiet model
  produces a notice after 60 seconds without progress, updated at most once a
  minute. This indicates a wait, not a confirmed timeout. Pending tool calls,
  including negotiations, do not count as model silence. Token-usage updates and
  service heartbeats do not count as model progress.
- Confirmed provider retries show a reason, retry attempt/backoff when available,
  and time without progress. The current player's banner and row show waiting or
  retrying; new model output clears that warning and records recovery in Activity.
  Earlier warnings remain in the saved turn history.
- A turn table with gameplay/bookkeeping time, calls, tokens, and accounting
  completeness. Select an older turn to load its saved event history.
- An API-call breakdown for the selected turn. The Game API total counts host
  `tool:` calls once; `command:` rows are a breakdown of those calls. Model-request
  counts are labeled unavailable because the current records do not contain them.
- The native winner and final player scores when the game finishes.

## Controls

| Control | Effect |
| --- | --- |
| Tab / Shift-Tab | Move between controls. |
| Click a tab | Switch between Activity, Diplomacy, Strategy, Turns, and API calls. |
| Enter on a player | Filter activity, totals, strategy, and diplomacy involving that player. |
| `d` | Open Diplomacy and focus the conversation list. |
| Enter on a conversation | Read its messages, proposals and outcomes. |
| `s` | Open Strategy and focus the civilization list. |
| Enter on a strategy row | Read that civilization's latest completed strategic report. |
| Enter on a turn | Inspect that player's recorded activity for that turn. |
| `a` | Return to all players and recent activity. |
| `l` | Return to recent activity for the selected player. |
| `f` | Toggle following the end of the Activity and Diplomacy logs. Scrolling up also preserves your reading position. |
| Space | Freeze/unfreeze dashboard updates. The game continues. |
| `v` | Toggle verbose context, tool details, and exposed reasoning summaries. |
| `q` / Ctrl-C | Close the viewer. The game continues. |

The activity view retains up to 600 recent entries from the latest two recorded
turns per player, plus ongoing gameplay and bookkeeping attempts. Selecting a turn
loads that turn's attempts. Long entries are bounded in the display; full records
remain in `runner/monitoring.sqlite3` and can be exported on demand. Verbose mode can also be selected at launch:

```sh
uv run civ-agent match watch --state-dir /path/to/match --verbose
```

Diplomacy retains complete conversation history. A selected turn narrows the list
to conversations with activity on that turn; their full history is still shown.
The metrics panel is hidden while reading Diplomacy to leave room for messages
in small terminals. Saved communications have game-turn numbers, but no original
wall-clock timestamps, so the display does not invent times for them.
Within a game turn, Activity positions them by database publication time so newly
arrived communications appear at the live end of the feed.

Strategy also hides the metrics panel and preserves the report's scroll position
on refresh. It always shows the latest completed report on the current timeline;
selecting an older activity turn does not select historical advice. The date on
each report matters: a review may finish several turns after its position was
captured. A blank `Preloaded turn` means it has not yet appeared in a gameplay
activation, not that the agent rejected it.

## Data and validation

This is a local operator view of `runner/monitoring.sqlite3`. Strategy also reads
the local agents' `agent-services/<civilization>/civilization/memory.sqlite3`
databases for committed reports and pending tasks. It uses read-only
connections and short transactions; it makes no game API or model requests.
Review progress and the bounded live trace come from each service's
`telemetry/accounting.sqlite3` database, updated with the normal review heartbeat.
Services must load the tracking code before they can publish these fields;
otherwise the viewer says progress or the trace is unavailable. Restart the viewer
and load the updated services on the next game launch/resume to enable them.
Viewing, filtering, freezing and exiting do not change scheduler or native state.
See [monitoring storage](../architecture/Monitoring.md) for ownership. Review
databases on another machine are unavailable to this local viewer until accessible
at the match's normal local paths. Private notes and frozen review inputs are not
loaded to display the reports.

The metrics area also shows accepted/rejected memory edits, batches containing
rejections, and reason counts for the selected turn, player, or game. These are
separate from API errors: a successful `memory_update` request can contain failed
edits. Older reports display unavailable or partial memory accounting rather than
assuming there were no rejections. The ordinary console includes the same counts
when reporting a completed turn.

The reader fetches changed rows by revision and new events by sequence number.
It reads committed data only and retains the last readable view with a warning
when storage is unavailable. Epoch and source checks keep different players and
restored timelines separate. Message deltas and final text use the same decoder
as the console. A poll reads at most 1,000 new events per selected stream, then
closes its transaction before rendering.

The host projects committed recipient streams into the operator database,
deduplicating shared copies of a message. New records are published after the
checkpoint commits. Restart backfills saved communication history, and a turn
rewind publishes the retained history under its new epoch; abandoned events stay
outside the current view. Already-running hosts need a rebuild and restart to
publish these records. No additional migration or model requests are needed.

The service records `model_status` events in its private `diagnostics.jsonl` and
turn history; the scheduler records them in the monitoring database and prints them in
the normal console log. Gameplay, background reviews, and communication jobs use
the same model monitor. These notices do not add model requests, change timeouts,
or retry native game commands. Existing turn failure/interrupt behavior still
applies when Codex exhausts its retries.

The monitor uses [Codex app-server events](https://learn.chatgpt.com/docs/app-server#errors)
and a narrow stderr fallback for `codex_core::responses_retry`, needed because
some automatic retries do not emit protocol errors. Only known error categories,
numeric HTTP status codes, attempt counts, and durations are retained. Raw stderr,
provider error bodies, headers, and credentials are discarded; oversized lines
are drained without being saved. The fallback was checked against Codex 0.155.1;
unrecognized future log formats still leave the elapsed-wait notices available.

`tests/test_match_watch.py` covers those reader behaviors, accounting presentation,
keyboard navigation, live refresh, freezing, small terminals, completion, and
clean viewer exit. The UI uses [Textual](https://textual.textualize.io/) and its
[headless test harness](https://textual.textualize.io/guide/testing/).

`tests/test_diplomacy_watch.py` covers counteroffer differences, recipient filters,
host-confirmed negotiation pauses, rewind isolation and small-terminal rendering.
The host's `EngineTradeProbe` verifies the projection against real trade enactment,
expiry, repeated social decisions and restart/rewind recovery.

`tests/test_strategy_watch.py` covers report publication, first preload delivery,
pending reviews and retries, player/timeline isolation, read-only access, scroll
preservation, freezing, keyboard selection and small terminals. Existing completed
reports use the current plan-comparison schema directly; older report formats
are unsupported. No file export is needed.

`tests/test_model_status.py` exercises waits, retries, tool execution, stale event
filtering, and bounded log parsing without wall-clock delays. The scheduler/service
integration test checks the complete path to console, saved history, and dashboard
activity. An optional installed-Codex test uses a local fake Responses endpoint
to verify actual timeout/retry warnings, with no paid requests:

```sh
CIV_TEST_NATIVE_CODEX=/path/to/codex uv run pytest -q tests/test_model_status_native.py
```

Already running processes keep their loaded code. These notices become available
when the services, scheduler, and dashboard are next started with this version.
