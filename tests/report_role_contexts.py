"""Combine captured-context replay with all-role paired screening accounting.

Reads preserved artifacts only. Incomplete trials are reported as unavailable,
never as zero-cost successes or evidence of gameplay equivalence.
"""

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path
from statistics import median

from civ_agent.protocol import JsonObject
from civ_agent.review_context import review_position
from civ_agent.telemetry import TOKEN_KEYS
from tests.evaluation_sources import tree_hash
from tests.report_strategy_positions import analyze


def read(path):
    return json.loads(path.read_text())


def tool_error_code(error):
    value = error.get("error", "memory rejection")
    return str(value.get("code", "STRUCTURED_ERROR")) if isinstance(value, dict) else str(value)


def replay_report(root):
    variants = {name: read(root / f"replay-{name}-final/results.json") for name in ("baseline", "candidate")}
    roles = {}
    for role in ("gameplay", "economy", "military"):
        rows = {name: [r for r in values if r["role"] == role] for name, values in variants.items()}
        startup = {name: median(r["totalPromptCharacters"] for r in values) for name, values in rows.items()}
        reduction = 100 * (1 - startup["candidate"] / startup["baseline"])
        roles[role] = {
            "medianStartupCharacters": startup,
            "reductionPercent": round(reduction, 2),
            "targetMet": reduction >= 35,
            "medianStartupSections": {
                name: {
                    section: median(r["payloadCharacters"].get(section, 0) for r in values)
                    for section in sorted({key for r in values for key in r["payloadCharacters"]})
                }
                for name, values in rows.items()
            },
            "medianSubsequentPayloadCharacters": {
                name: {
                    label: median(r["subsequentReads"][index]["characters"] for r in values)
                    for index, label in enumerate(("currentOverview", "cityStatus", "fullOverview"))
                }
                for name, values in rows.items()
            },
        }
    return {
        "roles": roles,
        "audit": {
            name: {
                "contexts": len(rows),
                "commandOffersChecked": sum(r["audit"]["commandOffersChecked"] for r in rows),
                **{
                    key: sum(len(r["audit"][key]) for r in rows)
                    for key in ("schemaErrors", "ownershipLabelErrors", "mandatoryInformationChanges")
                },
                "subsequentReadErrors": sum(bool(v["error"]) for r in rows for v in r["subsequentReads"]),
            }
            for name, rows in variants.items()
        },
        "omissions": [
            {key: row[key] for key in ("case", "role", "capturedTurn", "omitted", "pendingWork")}
            for row in variants["candidate"]
        ],
        "limitations": [
            "Four captured current positions per role, using their actual private records and reviews. Serialized characters are not tokens.",
            "Both replays use the same fake upstream catalogue and frozen permitted observations; they make no model or engine calls.",
            "No captured economy pass had a selected city scope, so both variants use a full city pass. Targeted passes have separate regression coverage.",
            "Mandatory comparison covers pending decisions, alerts, notifications, combat reports, rules, economy and output totals. Focused threat/uncertainty and ownership cases also have regressions.",
            "Baseline has no ownership metadata; zero label mismatches there is not evidence that all advertised actions were owned by the recipient.",
            "The four baseline schema mismatches are economy civilian summaries missing fields required by their advertised UnitSummary schema. Candidate summaries retain those fields.",
            "Startup section medians overlap (review/coordination subtotals) and must not be summed.",
        ],
    }


def job_accounting(path):
    records = [read(file) for file in path.glob("agent-services/*/activations/*.json")]
    result = {}
    for purpose in sorted({r.get("purpose", "gameplay") for r in records}):
        selected = [r for r in records if r.get("purpose", "gameplay") == purpose]
        usage = {key: sum(r.get("usage", {}).get(key, 0) for r in selected) for key in TOKEN_KEYS}
        usage["uncached_input_tokens"] = usage["input_tokens"] - usage["cached_input_tokens"]
        result[purpose] = {
            "jobs": len(selected),
            "phases": dict(Counter(r["phase"] for r in selected)),
            "reportedUsage": usage,
            "usageComplete": all(r.get("usageComplete", False) for r in selected),
            "reportedModelRequests": sum(r.get("modelRequests", 0) for r in selected),
            "startupCharacters": [
                r["startup"]["totalPromptCharacters"]
                for r in selected
                if (r.get("startup") or {}).get("totalPromptCharacters") is not None
            ],
            "providerFailures": dict(
                Counter(r["modelStatus"]["reason"] for r in selected if r.get("modelStatus", {}).get("reason"))
            ),
        }
    return result


def owned_tiles(path, civilization):
    saved = read(path)
    game = json.loads(saved["gameJson"]) if "gameJson" in saved else saved
    civ = next(c for c in game["civilizations"] if c["civID"] == civilization)

    def xy(position):
        return tuple(position.get(axis, 0) for axis in ("x", "y"))

    owned = {xy(tile) for city in civ.get("cities", []) for tile in city.get("tiles", [])}
    return {
        xy(tile.get("position", {})): tile
        for tile in game["tileMap"]["tileList"]
        if xy(tile.get("position", {})) in owned
    }


def improvements(path, civilization):
    return dict(
        Counter(
            t["improvement"]
            for t in owned_tiles(path, civilization).values()
            if t.get("improvement") not in {None, "City center"}
        )
    )


def improvement_changes(path, civilization):
    before = owned_tiles(path / "initial-game.json", civilization)
    after = owned_tiles(path / "exploration.json", civilization)
    return {
        "retainedLandChanges": [
            {
                "position": {"x": xy[0], "y": xy[1]},
                "before": before[xy].get("improvement"),
                "after": after[xy].get("improvement"),
            }
            for xy in sorted(before.keys() & after.keys())
            if before[xy].get("improvement") != after[xy].get("improvement")
        ],
        "improvementsOnGainedLand": dict(
            Counter(
                after[xy]["improvement"]
                for xy in after.keys() - before.keys()
                if after[xy].get("improvement") not in {None, "City center"}
            )
        ),
        "limitation": "Retained-land changes exclude improvements acquired with territory, but do not include work on tiles first acquired during the trial or repairs that retain the same improvement type. Endpoint differences can miss intermediate construction or destruction.",
    }


def final_observation_provenance(path):
    source, copied = path / "exploration.json", path / "final-observation/exploration.json"
    games = [json.loads(read(file)["gameJson"]) for file in (source, copied)]
    explored = []
    for game in games:
        for civ in game["civilizations"]:
            if "techsResearched" in civ.get("tech", {}):
                civ["tech"]["techsResearched"].sort()
        explored.append([tile.pop("exploredBy", []) for tile in game["tileMap"]["tileList"]])
    if games[0] != games[1]:
        raise ValueError(f"Final observation changed native state beyond reload bookkeeping: {path}")
    return {
        "path": str(path / "final-observation/initial-turn.json"),
        "checkpointSha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "observedCheckpointSha256": hashlib.sha256(copied.read_bytes()).hexdigest(),
        "nativeStateMatchesExceptReloadBookkeeping": True,
        "reloadExplorationChanges": [
            {"position": tile.get("position", {}), "before": before, "after": after}
            for tile, before, after in zip(games[0]["tileMap"]["tileList"], *explored, strict=True)
            if before != after
        ],
        "method": "Pinned-engine observation of an isolated checkpoint copy. Reload may refresh explored tiles and reorder researched-tech sets; all other native state must match. The fixture verifies that observation itself changes no native state.",
    }


def observed_outcomes(path, run, details):
    """Compare exact outcomes and dated rates without treating roster loss as death."""
    initial = read(Path(run["sourcePosition"]) / "initial-turn.json")
    final, reports = initial, {}
    military_names = {u["name"] for u in initial.get("map", {}).get("units", []) if u.get("military")}
    for database in path.glob("agent-services/*/civilization/memory.sqlite3"):
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
            for (body,) in db.execute("SELECT body FROM records WHERE key='observation:game' ORDER BY version"):
                value = json.loads(body)
                if value["turn"] >= final["turn"]:
                    final = value
                military_names.update(u["name"] for u in value.get("map", {}).get("units", []) if u.get("military"))
                for report in value.get("combatReports", []):
                    if report["turn"] >= initial["turn"]:
                        reports[report["id"]] = report
    checkpoint = read(path / "exploration.json")
    civilization = run.get("position", {}).get("civilization")
    saved_reports = checkpoint.get("players", {}).get(civilization, {}).get("combatReports", {})
    for report in saved_reports.values():
        if report["turn"] >= initial["turn"]:
            reports[report["id"]] = report
    last_observed = final
    final_observation = path / "final-observation/initial-turn.json"
    final_observation_source = None
    rejected_final_observation = None
    if final_observation.exists():
        try:
            final_observation_source = final_observation_provenance(path)
        except ValueError as error:
            rejected_final_observation = {"path": str(final_observation), "reason": str(error)}
    if final_observation_source is not None:
        final = read(final_observation)
        military_names.update(u["name"] for u in final.get("map", {}).get("units", []) if u.get("military"))
        for report in final.get("combatReports", []):
            if report["turn"] >= initial["turn"]:
                reports[report["id"]] = report
    losses = []
    for report in reports.values():
        combatant = None
        if (report.get("defender") or {}).get("isOwn") and report.get("outcome") in {"Destroyed", "Captured"}:
            combatant = report["defender"]
        elif (report.get("attacker") or {}).get("isOwn") and report.get("attackerDefeatedByDefender"):
            combatant = report["attacker"]
        elif report.get("ourDestroyed"):
            combatant = report.get("ourUnit")
        if combatant:
            losses.append({"report": report, "knownMilitaryType": combatant.get("name") in military_names})
    before, after = review_position(initial), review_position(final)
    change = details["outcomes"][0]
    return {
        "populationDelta": change["populationDelta"],
        "retainedCityPopulationDelta": sum(
            city["after"]["population"] - city["before"]["population"]
            for city in change["startingCities"]
            if city["after"] is not None
        ),
        "goldDelta": change["goldDelta"],
        "technologiesAcquired": change["technologiesAcquired"],
        "newCities": [city["name"] for city in change["newCities"]],
        "startingCitiesNoLongerOwned": [c["before"]["name"] for c in change["startingCities"] if c["after"] is None],
        "buildingsAddedInRetainedCities": [
            {"city": city["after"]["name"], "building": building}
            for city in change["startingCities"]
            if city["after"] is not None
            for building in sorted(set(city["after"]["buildings"]) - set(city["before"]["buildings"]))
        ],
        "ratesBefore": {"turn": before["turn"], **before["totals"]},
        "ratesAfter": {"turn": after["turn"], **after["totals"]},
        "ratesAtLastAgentObservation": {"turn": last_observed["turn"], **review_position(last_observed)["totals"]},
        "finalNativeObservation": final_observation_source,
        "rejectedFinalNativeObservation": rejected_final_observation,
        "confirmedObservedLossReports": losses,
        "savedPlayerCombatReports": {
            "civilization": civilization,
            "retainedReports": len(saved_reports),
            "source": str(path / "exploration.json"),
        },
        "confirmedObservedMilitaryLossReports": sum(r["knownMilitaryType"] for r in losses),
        "declaredWars": [
            c["arguments"].get("target")
            for c in details["commands"]
            if c["arguments"].get("kind") == "DECLARE_WAR" and c["result"].get("actionSummary")
        ],
        "confirmedCombatResults": [
            {"arguments": c["arguments"], "combat": c["result"]["combat"], "summary": c["result"].get("actionSummary")}
            for c in details["commands"]
            if c["result"].get("combat")
        ],
        "confirmedUnitUpgrades": sum(
            c["arguments"].get("kind") == "UPGRADE_UNIT" and bool(c["result"].get("actionSummary"))
            for c in details["commands"]
        ),
        "limitations": "Rates are dated observations, not cumulative yields. Final native observations include end-turn construction completions and opponent actions; without one, rates end at the last agent read. Combat evidence combines in-run observations with player combat reports retained in the original finished checkpoint. New buildings can be purchased. Unknown unit types are retained as loss reports without calling them military casualties.",
    }


def paired_comparisons(trials):
    pairs = []
    successful = {(t["case"], t["repetition"], t["variant"]): t for t in trials if t["completedEvaluation"]}
    for (case, repetition, variant), baseline in successful.items():
        if variant != "baseline" or (candidate := successful.get((case, repetition, "candidate"))) is None:
            continue
        pair: JsonObject = {"case": case, "repetition": repetition}
        for variant, trial in (("baseline", baseline), ("candidate", candidate)):
            roles = trial["allRoleAccounting"]
            complete = all(role["usageComplete"] for role in roles.values())
            usage = {
                key: sum(role["reportedUsage"][key] for role in roles.values())
                for key in (*TOKEN_KEYS, "uncached_input_tokens")
            }
            pair[variant] = {
                "rounds": trial["completedRounds"],
                "elapsedSeconds": trial["elapsedSeconds"],
                "modelRequests": sum(role["reportedModelRequests"] for role in roles.values()),
                "allRoleUsage": usage,
                "usageComplete": complete,
                "toolErrors": trial.get("toolErrors", {}),
                "roleStartupCharacters": {role: data["startupCharacters"] for role, data in roles.items()},
                "outcomes": trial["outcomes"]["summary"],
                "improvementsBefore": trial["outcomes"]["improvementsBefore"],
                "improvementsAfter": trial["outcomes"]["improvementsAfter"],
                "improvementChanges": trial["outcomes"]["improvementChanges"],
            }
        native_rates = all(pair[v]["outcomes"]["finalNativeObservation"] is not None for v in ("baseline", "candidate"))
        pair["rateEvidence"] = (
            "verified native endpoints" if native_rates else "dated last agent observations for both variants"
        )
        for variant in ("baseline", "candidate"):
            pair[variant]["comparisonRatesAfter"] = pair[variant]["outcomes"][
                "ratesAfter" if native_rates else "ratesAtLastAgentObservation"
            ]
        if all(pair[v]["usageComplete"] for v in ("baseline", "candidate")):
            pair["candidateMinusBaselineUsage"] = {
                key: pair["candidate"]["allRoleUsage"][key] - pair["baseline"]["allRoleUsage"][key]
                for key in pair["baseline"]["allRoleUsage"]
            }
        pairs.append(pair)
    return pairs


def cohort_summary(pairs):
    """Only complete matched pairs contribute to usage and outcome comparisons."""
    accounted = [pair for pair in pairs if all(pair[v]["usageComplete"] for v in ("baseline", "candidate"))]
    totals = {}
    for variant in ("baseline", "candidate"):
        totals[variant] = {
            **{
                key: sum(pair[variant]["allRoleUsage"][key] for pair in accounted)
                for key in (*TOKEN_KEYS, "uncached_input_tokens")
            },
            "modelRequests": sum(pair[variant]["modelRequests"] for pair in accounted),
            "trialSeconds": sum(pair[variant]["elapsedSeconds"] for pair in accounted),
        }
        totals[variant]["totalTokens"] = totals[variant]["input_tokens"] + totals[variant]["output_tokens"]

    def outcomes(row):
        data = row["outcomes"]
        before, after = data["ratesBefore"], row["comparisonRatesAfter"]
        return {
            "growthInRetainedCities": data["retainedCityPopulationDelta"],
            "populationChangeIncludingGainedAndLostCities": data["populationDelta"],
            "treasuryChange": data["goldDelta"],
            "newImprovementsOnRetainedLand": sum(
                change["before"] is None and change["after"] not in {None, "City center"}
                for change in row["improvementChanges"]["retainedLandChanges"]
            ),
            "improvedTileChangeIncludingGainedLand": sum(row["improvementsAfter"].values())
            - sum(row["improvementsBefore"].values()),
            "buildingsInRetainedCities": len(data["buildingsAddedInRetainedCities"]),
            "technologies": len(data["technologiesAcquired"]),
            "scienceRateChange": after["sciencePerTurn"] - before["sciencePerTurn"]
            if after["sciencePerTurn"] is not None and before["sciencePerTurn"] is not None
            else None,
            "productionRateChange": after["productionPerTurn"] - before["productionPerTurn"]
            if after["productionPerTurn"] is not None and before["productionPerTurn"] is not None
            else None,
            "militaryLossReports": data["confirmedObservedMilitaryLossReports"],
            "combatResults": len(data["confirmedCombatResults"]),
            "unitUpgrades": data["confirmedUnitUpgrades"],
            "cities": len(data["newCities"]) - len(data["startingCitiesNoLongerOwned"]),
        }

    differences = []

    def difference(old: int | float | None, new: int | float | None):
        return new - old if old is not None and new is not None else None

    for pair in pairs:
        before, after = outcomes(pair["baseline"]), outcomes(pair["candidate"])
        differences.append(
            {
                "case": pair["case"],
                "repetition": pair["repetition"],
                "baseline": before,
                "candidate": after,
                "candidateMinusBaseline": {key: difference(before[key], after[key]) for key in before},
            }
        )
    return {
        "completePairs": len(pairs),
        "pairsWithCompleteUsage": len(accounted),
        "usageTotals": totals,
        "candidateUsageChangePercent": {
            key: round(100 * (totals["candidate"][key] / value - 1), 2) if value else None
            for key, value in totals["baseline"].items()
            if key != "trialSeconds"
        },
        "outcomeComparisons": differences,
        "medianLiveStartupCharacters": {
            variant: {
                role: median(values)
                for role in ("gameplay", "economy", "military", "review")
                if (
                    values := [
                        value for pair in pairs for value in pair[variant]["roleStartupCharacters"].get(role, [])
                    ]
                )
            }
            for variant in ("baseline", "candidate")
        },
        "interpretation": "Positive usage changes cost more. Token differences include different decisions and action volumes; this is whole-gameplay screening, not a pure serialization benchmark. Outcome differences are descriptive; they do not establish causes, equal quality or win rate. Technology and building counts are unweighted; names and dated rates remain in the detailed evidence. Elapsed time is operational only and is excluded from performance conclusions because provider compute varies. Trial seconds sum independent runs and include startup/teardown, not concurrent suite wall time.",
    }


def live_report(root, paired=None) -> JsonObject:
    paired = paired or root / "paired"
    setup = read(paired / "commands.json")
    models = {}
    for model in setup["commands"]:
        suite = read(paired / model / "suite.json")
        results_path = paired / model / "results.json"
        previous_failures = []
        selected = {}
        for previous in setup.get("completedSuites", []):
            prior = Path(previous)
            for row in read(prior / model / "results.json"):
                if row["completedEvaluation"]:
                    selected[row["id"]] = (row, prior)
                elif not row.get("notStarted"):
                    previous_failures.append(
                        {
                            "sourceSuite": str(prior),
                            **row,
                            "allRoleAccounting": job_accounting(prior / model / row["id"]),
                        }
                    )
        for row in read(results_path) if results_path.exists() else []:
            selected[row["id"]] = (row, paired)
        rows = [row for row, _ in selected.values()]
        trials: list[JsonObject] = []
        for row, source_suite in selected.values():
            trial = {"sourceSuite": str(source_suite), **row}
            if not row.get("notStarted"):
                path = source_suite / model / row["id"]
                trial["allRoleAccounting"] = job_accounting(path)
                trial["outcomes"] = None
                run = read(path / "live-run.json")
                trial["frozenSourceMatches"] = run["pythonSourceSha256"] == setup["sources"][row["variant"]]["sha256"]
                trial["engineRevision"] = run["engineRevision"]
                trial["checkpointSha256"] = run["checkpointSha256"]
                if row["completedEvaluation"]:
                    details = analyze(path, source_suite / "codex/sessions")
                    (path / "evaluation.json").write_text(json.dumps(details, indent=2) + "\n")
                    trial["toolErrors"] = dict(Counter(tool_error_code(error) for error in details["errors"]))
                    civ = next(case["civilization"] for case in suite["positions"] if case["id"] == row["case"])
                    trial["outcomes"] = {
                        "summary": observed_outcomes(path, run, details),
                        "positions": details["outcomes"],
                        "improvementsBefore": improvements(path / "initial-game.json", civ),
                        "improvementsAfter": improvements(path / "exploration.json", civ),
                        "improvementChanges": improvement_changes(path, civ),
                        "productionAndScienceObservations": [
                            {key: r[key] for key in ("job", "turn", "economy", "outputSummary")}
                            for r in details["lastObservedStates"]
                        ],
                        "militaryEvidence": "See evaluation.json commands, native roster changes and reports; missing units alone are not casualties.",
                    }
            trials.append(trial)
        complete = {(r["case"], r["repetition"], r["variant"]) for r in rows if r["completedEvaluation"]}
        comparisons = paired_comparisons(trials)
        models[model] = {
            "scheduledTrials": len(suite["positions"]) * suite["repetitions"] * len(suite["sources"]),
            "attemptedTrials": sum(not r.get("notStarted") for r in rows),
            "completedTrials": sum(r["completedEvaluation"] for r in rows),
            "verifiedEndpointObservations": sum(
                bool((trial.get("outcomes") or {}).get("summary", {}).get("finalNativeObservation")) for trial in trials
            ),
            "rejectedEndpointObservations": [
                {"id": trial["id"], **rejection}
                for trial in trials
                if (rejection := (trial.get("outcomes") or {}).get("summary", {}).get("rejectedFinalNativeObservation"))
            ],
            "completedRounds": sum(r.get("completedRounds", 0) for r in rows),
            "completedPairs": sum(
                (case, repetition, "candidate") in complete
                for case, repetition, variant in complete
                if variant == "baseline"
            ),
            "trials": trials,
            "priorInterruptedAttempts": previous_failures,
            "pairedComparisons": comparisons,
            "comparisonSummary": cohort_summary(comparisons),
        }
    return {
        "setup": setup,
        "frozenSourcesUnchanged": all(
            tree_hash(Path(source["path"]) / "civ_agent") == source["sha256"] for source in setup["sources"].values()
        ),
        "models": models,
        "interpretation": "Screening only, not win-rate evidence. Compare only completed paired trials. Incomplete usage counters do not imply free or zero-token execution; gameplay and efficiency remain unvalidated without completed pairs.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--paired", type=Path, help="Select a later live attempt without replacing earlier evidence")
    parser.add_argument("--output", type=Path, help="Report JSON path; a text summary is written beside it")
    args = parser.parse_args()
    root = args.root.resolve()
    paired = args.paired.resolve() if args.paired else root / "paired"
    output = args.output.resolve() if args.output else root / "evaluation-report.json"
    replay, live = replay_report(root), live_report(root, paired)
    baseline = read(root / "baseline-manifest.json")
    report = {
        "status": "SCREENING_INCOMPLETE"
        if any(m["completedTrials"] != m["scheduledTrials"] for m in live["models"].values())
        else "SCREENING",
        "baseline": {
            "hostHead": baseline["hostHead"],
            "engineHead": baseline["engineHead"],
            "preservedFiles": len(baseline["files"]),
            "archive": "baseline-source.tar.gz",
            "uncommittedChanges": "baseline-status.txt and baseline.diff; included in the source archive",
        },
        "replay": replay,
        "live": live,
        "verification": read(root / "verification.json") if (root / "verification.json").exists() else None,
        "rollout": "Existing benchmark runtimes remain pinned. Gameplay validation is required before using this candidate in new benchmarks. Worker ownership and sequential execution remain separate follow-up experiments.",
    }
    output.write_text(json.dumps(report, indent=2) + "\n")
    lines = ["Role-context screening — 2026-10-29", "", report["status"], "", "Median startup characters:"]
    for role, row in replay["roles"].items():
        counts = row["medianStartupCharacters"]
        lines.append(
            f"  {role}: {counts['baseline']:,} -> {counts['candidate']:,} ({row['reductionPercent']}% smaller)"
        )
    lines += ["", "Candidate replay audit:", "  " + json.dumps(replay["audit"]["candidate"]), "", "Live trials:"]
    for model, row in live["models"].items():
        lines.append(
            f"  {model}: {row['attemptedTrials']}/{row['scheduledTrials']} attempted; "
            f"{row['completedTrials']} completed trials; {row['completedRounds']} completed rounds; "
            f"{row['completedPairs']} completed pairs; {row['verifiedEndpointObservations']} verified endpoint reads."
        )
        comparison = row["comparisonSummary"]
        if row["rejectedEndpointObservations"]:
            lines.append(
                "    Reload changed native state; use dated in-run rates for: "
                + ", ".join(item["id"] for item in row["rejectedEndpointObservations"])
                + ". Original-save outcomes are retained."
            )
        if comparison["pairsWithCompleteUsage"]:
            changes = comparison["candidateUsageChangePercent"]
            lines.append(
                f"    Complete-accounting pairs: {comparison['pairsWithCompleteUsage']}; candidate change: "
                f"{changes['totalTokens']}% total tokens, {changes['uncached_input_tokens']}% uncached input, "
                f"{changes['modelRequests']}% requests."
            )
        for pair in comparison["outcomeComparisons"]:
            before, after = pair["baseline"], pair["candidate"]
            lines.append(
                f"    {pair['case']} repetition {pair['repetition']}, baseline -> candidate: "
                + "; ".join(f"{key} {before[key]} -> {after[key]}" for key in before)
            )
    if any(trial.get("providerFailure") for model in live["models"].values() for trial in model["trials"]):
        lines += ["", "Provider usageLimitExceeded interrupted the screening. Remaining trials were skipped."]
    if not any(model["completedPairs"] for model in live["models"].values()):
        lines += [
            "No gameplay, growth, improvement, production, science, military or total-token comparison is established."
        ]
    lines += [
        "Reported counters and elapsed time for every attempted role are retained in " + output.name + ".",
        "Elapsed time is operational only; gameplay outcomes and model usage determine the comparison.",
        "Zero reported tokens with incomplete accounting are not zero-cost measurements.",
        "Science and production are dated rates, not cumulative yields. Each pair uses verified native endpoints only when both are available; otherwise both variants use their last agent observations. Per-pair evidence and dates are retained in the JSON.",
        "Growth counts retained cities only; population from gained or lost cities is reported separately. Unit upgrades and great-person consumption are not classified as casualties. Buildings and technologies are unweighted counts.",
        "",
        "Configuration: four frozen positions, ten rounds, two repetitions, both models at medium effort,",
        "both guides enabled, identical empty private memory, review interval 10, specialist intervals 5,",
        "native advice and adaptive effort disabled in both variants, opponents controlled by native AI.",
        "Source hashes, engine revision, commands, omissions and detailed section/payload counts are in the JSON report.",
        "China's last captured player observation was turn 121; its saved position was turn 122.",
        "These are screening measurements, not win-rate evidence.",
        "",
        report["rollout"],
    ]
    if verification := report["verification"]:
        lines += ["", "Verification:", "  " + verification["fullPythonSuite"]]
        lines += ["  " + check for check in verification["checks"]]
    lines += [
        "",
        "Preserved comparison:",
        f"  {len(baseline['files'])} files, including initial uncommitted changes; baseline hashes verified.",
        "  Host HEAD: " + baseline["hostHead"],
        "  Engine: " + baseline["engineHead"],
        *[f"  {name} Python tree: {source['sha256']}" for name, source in live["setup"]["sources"].items()],
        "  Artifacts: " + str(root),
        "  Live attempt: " + str(paired),
        "  Full setup, per-role usage completeness, elapsed time and omissions: " + output.name,
        "",
        f"Reproduce the report: uv run --locked python -m tests.report_role_contexts {root} --paired {paired} --output {output}",
        "Read a finished trial's endpoint with ./gradlew prepareStrategyPositions -PstrategyMode=observe -PstrategySource=<trial>/exploration.json -PstrategyOutput=<trial>/final-observation -PstrategyCivilization=<civilization> (absolute paths, fresh output directory). This reads an isolated copy without advancing a turn or invoking a model.",
    ]
    if report["status"] == "SCREENING_INCOMPLETE":
        lines += [
            "Continue incomplete gameplay with tests.live_role_contexts, the preserved prepared positions and source variants, a fresh state directory, and --completed-suite pointing to this live attempt; retain earlier attempts."
        ]
    output.with_suffix(".txt").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
