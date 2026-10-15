"""Bind accepted advice to observed exposure, without inferring model adoption."""

import copy

from civ_agent.protocol import JsonObject

DISPOSITIONS = {"delivered", "no_notice", "not_selected", "stale", "not_accepted"}


def _reference(value):
    if isinstance(value, dict) and isinstance(value.get("key"), str) and type(value.get("version")) is int:
        return {"key": value["key"], "version": value["version"]}
    return None


def delivery_basis(evidence, result, cities=()):
    """Freeze only bindings, never source text; result must already be validated."""
    answers = {row["name"]: row for row in result.get("answers", [])}
    statuses = {row["name"]: row["status"] for row in result.get("questionResults", [])}
    objectives = [ref for item in evidence.get("objectives", []) if (ref := _reference(item)) is not None]
    rows = []
    for field, prefixes in (
        ("retrieval", ("retrieval_include", "retrieval_priority")),
        ("preservation", ("preservation",)),
        ("beliefs", ("beliefs",)),
    ):
        for index, source in enumerate(evidence.get(field, [])):
            for prefix in prefixes:
                name = f"{prefix}:{index}"
                row = _binding(result, name, answers, statuses)
                row["objectives"] = copy.deepcopy(objectives)
                row["source"] = _reference(source)
                row["dependencies"] = [
                    ref
                    for item in [
                        *(evidence.get("objectives", []) if field != "preservation" else []),
                        *(evidence.get("newEvidence", []) if field == "beliefs" else []),
                    ]
                    if (ref := _reference(item)) is not None
                ]
                if field == "retrieval":
                    row["includeChoice"] = answers.get(f"retrieval_include:{index}", {}).get("choice")
                rows.append(row)
    for index, city in enumerate(cities or evidence.get("cities", [])):
        for prefix in ("city_focus_change", "city_focus", "city_construction_change", "city_construction"):
            row = _binding(result, f"{prefix}:{index}", answers, statuses)
            field = "city_focus" if prefix.startswith("city_focus") else "city_construction"
            row["changeChoice"] = answers.get(f"{field}_change:{index}", {}).get("choice")
            row["objectives"] = copy.deepcopy(objectives)
            row.update(city=city["city"], cityIndex=index, cityBasis=copy.deepcopy(city["basis"]))
            row["dependencies"] = [ref for item in evidence.get("objectives", []) if (ref := _reference(item))]
            rows.append(row)
    if evidence.get("earlyReview") is not None:
        row = _binding(result, "early_review", answers, statuses)
        row["objectives"] = copy.deepcopy(objectives)
        row["source"] = _reference(evidence["earlyReview"].get("current", {}).get("source"))
        rows.append(row)
    return rows


def _binding(result, name, answers, statuses):
    answer = answers.get(name)
    return {
        "requestId": result.get("requestId"),
        "name": name,
        "choice": answer.get("choice") if answer else None,
        "status": "accepted" if answer else "not_accepted",
        "accepted": answer is not None,
        "questionStatus": statuses.get(name, "missing"),
    }


def _changed(reference, memory):
    if reference is None:
        return False
    record = memory.get("records", {}).get(reference["key"])
    return isinstance(record, dict) and record.get("version") != reference["version"]


def _notice_matches(reference, sources):
    return any(_reference(source) == reference for source in sources if isinstance(source, dict))


def _finish(row, memory):
    if not row.get("accepted", row["status"] == "accepted"):
        return "not_accepted", None
    name, choice = row["name"], row["choice"]
    prefix = name.split(":", 1)[0]
    if (prefix.startswith(("retrieval_", "city_")) or prefix == "beliefs") and memory.get(
        "advisoryObjectiveStatus", {}
    ).get(row["requestId"]) is False:
        return "stale", None
    source = row.get("source")
    if any(_changed(ref, memory) for ref in [source, *row.get("dependencies", [])]):
        return "stale", None
    notices = memory.get("advisories", {})
    if choice in {"skip", "retain", "omit", "wait"}:
        return "no_notice", None
    if prefix.startswith("retrieval_"):
        if source is None:
            return "not_selected", None
        if row.get("includeChoice") == "omit":
            return "no_notice", None
        delivered = memory.get("delivered", {}).get(source["key"])
        if delivered is not None and delivered != source["version"]:
            return "stale", None
        if source["key"] in memory.get("contextIds", []) and delivered == source["version"]:
            return "delivered", "context"
    elif prefix == "preservation":
        candidates = notices.get("preservationCandidates", [])
        if source is not None and _notice_matches(source, candidates):
            return "delivered", "notice"
        if any(item.get("key") == (source or {}).get("key") for item in candidates):
            return "stale", None
    elif prefix == "beliefs":
        for notice in notices.get("beliefNotices", []):
            belief = _reference(notice.get("belief"))
            if belief == source and source is not None:
                return "delivered", "notice"
            if belief and belief["key"] == (source or {}).get("key"):
                return "stale", None
    elif prefix.startswith("city_"):
        if str(notices.get("cityAdviceStatus", {}).get("fallback", "")).startswith("stale"):
            return "stale", None
        if not prefix.endswith("_change") and row.get("changeChoice") == "keep":
            return "no_notice", None
        if not prefix.endswith("_change") and row.get("changeChoice") != "change":
            return "not_selected", None
        field = "focus" if prefix.startswith("city_focus") else "construction"
        for notice in notices.get("cityRecommendations", []):
            if notice.get("city") != row["city"]:
                continue
            if notice.get("basis") != row["cityBasis"]:
                return "stale", None
            delivered = notice.get(field, {})
            if prefix.endswith("_change"):
                delivered = delivered.get("change", {})
            if delivered.get("choice") == choice:
                return "delivered", "advice"
    elif prefix == "early_review":
        # Scheduling is a separate path. Count it only with explicit evidence.
        scheduled = memory.get("earlyReviewDelivery", {})
        if scheduled.get("requestId") == row["requestId"] and scheduled.get("scheduled") is True:
            return "delivered", "scheduler"
    return "not_selected", None


def finish_delivery(basis, jobmemory):
    """Return exposure dispositions; delivered never means read, followed or acted."""
    rows = []
    for original in basis:
        row: JsonObject = copy.deepcopy(original)
        row.pop("exposure", None)
        row["status"], exposure = _finish(row, jobmemory)
        if exposure is not None:
            row["exposure"] = exposure
        rows.append(row)
    return rows
