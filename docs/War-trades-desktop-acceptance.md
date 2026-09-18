# War-trade desktop acceptance

Visual acceptance passed on September 18, 2026 against pinned engine
`0bad73285ffa306334b89bcecbe395dc3b3b0bda`. The real Unciv desktop rendered
16 screenshots at 1100 × 760; all were inspected. The launcher clicked native
offer, acceptance, acknowledgement and confirmation controls and asserted the
resulting treasury balances and diplomatic relationships.

The fixtures use Vanilla, with human Rome, Greece and France, initially at peace
and holding 500 Gold each. They are separate from the saved live model games.
No host connection or agent service was used, and the desktop process exited
successfully after the exercise.

## Observed results

| Case | Native settlement | Visible result |
| --- | --- | --- |
| Paid declaration | Rome pays Greece 60 Gold. Final treasuries: Rome 440, Greece 560, France 500. | Greece's acceptance shows the payment against its declaration. France sees Greece's declaration and an Enemy relationship; Rome remains Neutral. |
| Joining an existing war | From the paid position, Greece pays Rome 20 Gold. Final treasuries: Rome 460, Greece 540, France 500. | Rome accepts its declaration against France. France sees a Roman declaration and the notification that Rome joined Greece in the war. Both are enemies. |
| Joint declaration | From a fresh peaceful position, Rome pays Greece 50 Gold and both declare. Final treasuries: Rome 450, Greece 550, France 500. | Acceptance shows both declarations and the payment. France receives two leader popups plus the combined Rome-and-Greece declaration notification. Both diplomatic relationships are Enemy. |
| Native declaration warning | Separate peaceful fixture: Greece has a native nonaggression promise; France has a defensive pact with Rome. | Clicking **Declare war** shows the promise-breaking warning and says Rome will also join. **Cancel** preserves peace. |

Rome and Greece remain at peace with each other after every settlement. The target's
paid-war popup and notification do not name the sponsor or payment. Joining and
joint notifications legitimately identify the participants in the war; they do
not disclose prices. Rome's declaration flavor text mentions its treasury, as the
native leader dialogue normally does, without reporting the contract or balance.

## Evidence

Generated artifacts are retained under
[`build/verification/war-trades-desktop-20260918`](../build/verification/war-trades-desktop-20260918).
The [acceptance manifest](../build/verification/war-trades-desktop-20260918/acceptance.json)
records results and screenshot hashes. Each screenshot has a companion text dump
of scene labels. Native saves preserve the baseline, three settlement states and
cancelled-warning state.

- Paid: [offer](../build/verification/war-trades-desktop-20260918/01-paid-offer.png),
  [consent](../build/verification/war-trades-desktop-20260918/02-paid-consent.png),
  [target notice](../build/verification/war-trades-desktop-20260918/03-paid-notice.png),
  [enemy](../build/verification/war-trades-desktop-20260918/04-paid-enemy.png),
  [neutral sponsor](../build/verification/war-trades-desktop-20260918/05-paid-neutral-sponsor.png).
- Joining: [offer](../build/verification/war-trades-desktop-20260918/06-joining-offer.png),
  [consent](../build/verification/war-trades-desktop-20260918/07-joining-consent.png),
  [target notice](../build/verification/war-trades-desktop-20260918/08-joining-notice.png),
  [enemy](../build/verification/war-trades-desktop-20260918/09-joining-enemy.png).
- Joint: [offer](../build/verification/war-trades-desktop-20260918/10-joint-offer.png),
  [consent](../build/verification/war-trades-desktop-20260918/11-joint-consent.png),
  [Roman notice](../build/verification/war-trades-desktop-20260918/12-joint-first-notice.png),
  [Greek notice](../build/verification/war-trades-desktop-20260918/13-joint-second-notice.png),
  [Roman relationship](../build/verification/war-trades-desktop-20260918/14-joint-rome-enemy.png),
  [Greek relationship](../build/verification/war-trades-desktop-20260918/15-joint-greece-enemy.png).
- [Native promise and pact warning](../build/verification/war-trades-desktop-20260918/16-native-promise-and-pact-warning.png).

These generated artifacts are local and ignored by Git. The original successful
run is also retained at `/tmp/unciv-war-desktop-20260919-2`; its directory suffix
does not describe the actual acceptance date.

## Reproduction and scope

From a graphical desktop session, choose a new absolute output directory:

```sh
./gradlew runWarTradeDesktop \
  -PwarDesktopDir="$PWD/build/verification/war-trades-desktop-new" \
  --console=plain
```

The [private test launcher](../src/test/kotlin/uncivai/WarTradeDesktopProbe.kt)
uses the selected engine's normal desktop launcher and copied assets. It prepares
trade drafts directly in `DiplomacyScreen`, then sends pointer events to **Offer
trade**, **Sounds good!**, **Farewell.**, and France's **Very well.** controls.
Rendering comes from the actual desktop framebuffer, not a reconstructed image.
It changes the viewing player only in isolated fixture copies, without advancing
turns, so settlement balances remain exact and independent of economic income.

This exercise covers the three requested war-trade forms, their visible native
effects, and the shared native declaration warning. Amount-picker editing,
every possible screen size, unknown defensive-pact partners and Gods & Kings
rendering were not part of this visual check. The warning is the ordinary direct
declaration confirmation; the desktop trade-acceptance popup itself does not
show that warning. The host's private social commitments remain covered by the
separate host checks. These results do not establish full trade or player-API parity.
