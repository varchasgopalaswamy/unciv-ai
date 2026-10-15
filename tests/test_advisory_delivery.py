import copy
from pathlib import Path

from civ_agent.advisory_delivery import delivery_basis, finish_delivery
from civ_agent.advisory_replay import load_cases
from tests.test_advisory_replay import captured


def sample():
    case = next(
        case
        for case in load_cases(Path(__file__).parent / "fixtures/decisions-advisory-replay.json")
        if case["id"] == "mixed-independent-tasks"
    )
    evidence, result = case["evidence"], captured(case)
    basis = delivery_basis(evidence, result)
    city = evidence["cities"][0]
    memory = {
        "contextIds": ["note1"],
        "delivered": {"note1": 1},
        "records": {
            key: {"version": version}
            for key, version in (
                ("note1", 1),
                ("event:1", 1),
                ("belief1", 1),
                ("strategy1", 1),
                ("observation:overview", 2),
            )
        },
        "advisories": {
            "preservationCandidates": evidence["preservation"],
            "beliefNotices": [{"belief": evidence["beliefs"][0], "sources": evidence["newEvidence"]}],
            "cityRecommendations": [
                {
                    "city": city["city"],
                    "basis": city["basis"],
                    "focus": {"choice": "focus0", "change": {"choice": "change"}},
                    "construction": {"choice": "keep", "change": {"choice": "keep"}},
                }
            ],
        },
    }
    return evidence, result, basis, memory


def by_name(rows):
    return {row["name"]: row for row in rows}


def test_exact_exposure_is_recorded_without_claiming_read_adoption_or_action():
    evidence, result, basis, memory = sample()
    before = copy.deepcopy((evidence, result, basis, memory))
    finished = finish_delivery(basis, memory)
    rows = by_name(finished)
    assert sum(row["status"] == "delivered" for row in finished) == 7
    assert rows["retrieval_include:0"]["exposure"] == "context"
    assert rows["preservation:0"]["exposure"] == rows["beliefs:0"]["exposure"] == "notice"
    assert rows["city_construction_change:0"]["choice"] == "keep"
    assert rows["city_construction_change:0"]["exposure"] == "advice"
    assert rows["city_construction:0"]["status"] == "no_notice"
    assert rows["early_review"]["status"] == "not_selected"
    assert (evidence, result, basis, memory) == before
    assert finish_delivery(finished, memory) == finished
    memory["delivered"]["note1"] = 2
    stale = by_name(finish_delivery(finished, memory))["retrieval_include:0"]
    assert stale["status"] == "stale" and "exposure" not in stale
    memory["delivered"]["note1"] = 1
    memory["earlyReviewDelivery"] = {"requestId": result["requestId"], "scheduled": True}
    assert by_name(finish_delivery(basis, memory))["early_review"]["exposure"] == "scheduler"
    assert all("record" not in row and "text" not in row for row in finished)


def test_version_mismatch_is_stale_and_absent_selection_is_not_inferred_delivery():
    _, _, basis, memory = sample()
    memory["contextIds"] = []
    memory["advisories"]["preservationCandidates"] = []
    rows = by_name(finish_delivery(basis, memory))
    assert rows["retrieval_include:0"]["status"] == rows["preservation:0"]["status"] == "not_selected"
    memory["delivered"]["note1"] = 2
    memory["records"]["observation:overview"]["version"] = 3
    memory["advisories"]["cityRecommendations"][0]["basis"] = {"revision": "different"}
    rows = by_name(finish_delivery(basis, memory))
    assert rows["retrieval_include:0"]["status"] == rows["beliefs:0"]["status"] == "stale"
    assert rows["city_focus:0"]["status"] == rows["city_construction_change:0"]["status"] == "stale"
    assert rows["city_construction:0"]["status"] == "no_notice"


def test_negative_accepted_answers_and_rejections_do_not_become_positive_notices():
    evidence, result, _, memory = sample()
    choices = {
        "retrieval_include:0": "omit",
        "retrieval_priority:0": "normal",
        "preservation:0": "skip",
        "beliefs:0": "retain",
        "early_review": "wait",
        "city_focus:0": "defer",
    }
    for answer in result["answers"]:
        if answer["name"] in choices:
            answer["choice"] = choices[answer["name"]]
    result["answers"] = [answer for answer in result["answers"] if answer["name"] != "city_construction:0"]
    memory["advisories"]["cityRecommendations"][0]["focus"]["choice"] = "defer"
    rows = by_name(finish_delivery(delivery_basis(evidence, result), memory))
    assert all(rows[name]["status"] == "no_notice" for name in choices if name != "city_focus:0")
    assert rows["city_focus:0"]["status"] == "delivered" and rows["city_focus:0"]["exposure"] == "advice"
    assert rows["city_construction:0"]["status"] == "not_accepted"


def test_complete_objective_set_staleness_is_respected_without_suppressing_preservation():
    _, result, basis, memory = sample()
    assert all(row["objectives"] == [{"key": "strategy1", "version": 1}] for row in basis)
    memory["advisoryObjectiveStatus"] = {result["requestId"]: False}
    rows = by_name(finish_delivery(basis, memory))
    for name in ("retrieval_include:0", "retrieval_priority:0", "beliefs:0", "city_focus:0", "city_construction:0"):
        assert rows[name]["status"] == "stale"
    assert rows["preservation:0"]["status"] == "delivered"
