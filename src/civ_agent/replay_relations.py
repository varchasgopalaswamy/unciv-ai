"""Formal diplomatic relations for the major civilizations in a replay."""

from collections import defaultdict
from itertools import combinations


def _labels(pair, *, war=False, treaty=False, friendship=False, defense=False, research=False, borders=()):
    if war:
        return ["War"]
    labels = ["Peace treaty" if treaty else "Peace"]
    if friendship:
        labels.append("Friendship")
    if defense:
        labels.append("Defensive pact")
    if research:
        labels.append("Research agreement")
    if all(name in borders for name in pair):
        labels.append("Open borders both ways")
    else:
        labels.extend(f"{name} grants open borders" for name in pair if name in borders)
    return labels


def saved_relations(world, roster):
    civs = {civ.get("civID", civ.get("civName")): civ for civ in world.get("civilizations", [])}
    rows = []
    for first, second in combinations(roster, 2):
        pair = (first, second)
        players = (civs.get(first, {}), civs.get(second, {}))
        deals = [player.get("diplomacy", {}).get(other) for player, other in zip(players, reversed(pair), strict=True)]
        if any(player.get("hasEverOwnedOriginalCapital") and not player.get("cities") for player in players):
            labels = ["Eliminated"]
        elif deals == [None, None]:
            labels = ["Not met"]
        else:
            # Unciv omits the default War value when serializing a diplomacy manager.
            status = {deal.get("diplomaticStatus", "War") for deal in deals if deal is not None}
            flags = set()
            grants = set()
            treaty = False
            for index, deal in enumerate(deals):
                if deal is None:
                    continue
                flags.update(flag for flag, turns in deal.get("flagsCountdown", {}).items() if turns > 0)
                for trade in deal.get("trades", []):
                    for side, giver in (("ourOffers", pair[index]), ("theirOffers", pair[1 - index])):
                        for offer in trade.get(side, []):
                            if offer.get("duration", 0) <= 0:
                                continue
                            if offer.get("name") == "Peace Treaty":
                                treaty = True
                            if offer.get("name") == "Open Borders":
                                grants.add(giver)
            labels = _labels(
                pair,
                war="War" in status,
                treaty=treaty,
                friendship="DeclarationOfFriendship" in flags,
                defense="DefensivePact" in status or "DefensivePact" in flags,
                research="ResearchAgreement" in flags,
                borders=grants,
            )
        rows.append([first, second, labels])
    return rows


def observed_relations(observation, observer):
    rows = {}
    for other in (observation.get("diplomacy") or {}).get("knownCivilizations", []):
        partner = other.get("civilizationId")
        if not partner or partner == observer:
            continue
        pair = tuple(sorted((observer, partner)))
        bilateral = other.get("bilateral") or {}
        borders = []
        if bilateral.get("theyMayEnterYourTerritory"):
            borders.append(observer)
        if bilateral.get("youMayEnterTheirTerritory"):
            borders.append(partner)
        rows[pair] = (
            ["Eliminated"]
            if other.get("defeated")
            else _labels(
                pair,
                war=other.get("atWar", False),
                treaty=(bilateral.get("peaceTreatyTurns") or other.get("peaceTreatyTurns") or 0) > 0,
                friendship=(bilateral.get("friendshipTurns") or 0) > 0,
                defense=other.get("status") == "DefensivePact" or (bilateral.get("defensivePactTurns") or 0) > 0,
                research=(bilateral.get("researchAgreementTurns") or 0) > 0,
                borders=borders,
            )
        )
    return rows


def record_relations(history, step, rows):
    if rows and (not history or history[-1][1] != rows):
        if history and history[-1][0] == step:
            history[-1][1] = rows
        else:
            history.append([step, rows])


def _confirmed_relations(streams, roster):
    reports = defaultdict(dict)
    for observer in roster:
        for delivery in streams.get(observer, []):
            if delivery["kind"] != "engine":
                continue
            body = delivery["body"]
            turn = body.get("turn")
            pair = tuple(sorted(body.get("participants", [])))
            confirmation = body.get("confirmation") or {}
            if (
                confirmation.get("source") != "engine"
                or type(turn) is not int
                or len(pair) != 2
                or observer not in pair
                or any(c not in roster for c in pair)
            ):
                continue
            first, second = pair
            placed = confirmation.get("relationships") or {}
            if any(
                placed.get(name, {}).get("otherCivilizationId") != other
                for name, other in ((first, second), (second, first))
            ):
                continue
            states = (placed[first], placed[second])
            reports[turn, pair][observer] = tuple(
                _labels(
                    pair,
                    war=any(state.get("atWar") for state in states),
                    treaty=any(state.get("peaceTreatyTurns", 0) > 0 for state in states),
                    friendship=any(state.get("friendshipTurns", 0) > 0 for state in states),
                    defense=any(state.get("defensivePactTurns", 0) > 0 for state in states),
                    research=any(state.get("researchAgreementTurns", 0) > 0 for state in states),
                    borders=[
                        other
                        for other, state in zip(reversed(pair), states, strict=True)
                        if state.get("mayEnterOtherTerritory")
                    ],
                )
            )
    return {key: set(values.values()) for key, values in reports.items()}


def snapshot_relations(world, roster, turns, last_step, observations, streams=None):
    history = []
    pairs = list(combinations(roster, 2))
    confirmations = _confirmed_relations(streams or {}, roster)
    for step, turn in turns:
        rows = []
        for first, second in pairs:
            pair = tuple(sorted((first, second)))
            reports = {tuple(report[pair]) for player in pair if pair in (report := observations[player].get(turn, {}))}
            states = confirmations.get((turn, pair), reports)
            if ("Eliminated",) in reports:
                labels = ["Eliminated"]
            elif not states:
                labels = ["Unrecorded"]
            elif len(states) == 1:
                labels = list(next(iter(states)))
            else:
                labels = ["Reports differ"]
            rows.append([first, second, labels])
        record_relations(history, step, rows)
    record_relations(history, last_step, saved_relations(world, roster))
    return history
