# Ordinary notifications

`player_turn.notifications` contains the player's pending ordinary messages,
including their full translated `text`, `category`, `icons`, event `locations`,
short `handle` (`notice1`, etc.), and `canDismiss`. The Python compact overview
includes this content in full. Reading a notification does not clear it.

These are messages from the player's own Unciv notification list, separate from
blocking popups and combat reports. Map locations come from the notification's
navigation links; they do not reveal terrain or units behind the link. The API
does not execute UI navigation actions.

After reading the messages and updating private assessments when useful, an
agent can clear a selected batch:

```json
{"kind": "DISMISS_NOTIFICATIONS", "targets": ["notice1", "notice3"]}
```

The agent service supplies credentials, request identity, and the revision.
Direct host clients supply the usual command bookkeeping. Up to 256 distinct
handles can be dismissed in one command. The host validates the entire batch
against a freshly read `player_turn` before changing anything. An unknown,
already dismissed, unread, or stale selection cannot clear other messages.
Messages arriving after that observation invalidate its revision.

Dismissal removes messages from the player API's pending list. It preserves
Unciv's native notifications so that combat reporting and the desktop's turn
history remain intact. `receipt.dismissedNotifications` retains the exact
messages; agent turn receipts preserve that content for later inspection.
Identical messages have separate handles, and a new occurrence is not hidden
because an earlier one was dismissed.

Ordinary messages do not block `END_TURN` or require an assessment update. Unciv
archives its current notifications at turn end. The host does not automatically
replay that archive into subsequent turns; the agent's observations and receipts
remain available through its existing history tools. There are no special rules
for encampments, economic events, or other notification categories.

Pending handles, dismissals, and receipts are part of checkpoint format **4**.
Restart requires a fresh read before an uncommitted dismissal, while committed
requests replay their original receipts. Turn and round rollback restore the
pending list, and discarded handles are not reissued. Older host checkpoint
formats require a new game.

Native loading can append a notice while rebuilding caches, such as a captured
city becoming disconnected from the capital. Restore validates saved handles
against the original serialized notices and verifies that loading preserved their
order and content. Appended notices receive fresh handles on observation; missing
or reordered saved notices and inconsistent handle counts still fail recovery.

The turn query also checkpoints a refreshed revision after a rejected command;
an internal validation read must not cause the subsequent public read to skip
persisting newly issued references.

`NotificationsIntegrationTest` exercises the native game over HTTPS through
separate process restarts, including private messages, batch validation,
duplicate content, late arrivals, receipt replay, turn rollback, and native
history preservation. It also checks appended load notices and corrupt/reordered
notification state; the conquest regression verifies an actual native
city-disconnection notice across restart. `tests/test_notifications.py` checks the Pydantic schemas,
full model-facing content, agent-authored evidence, and durable receipt history.
