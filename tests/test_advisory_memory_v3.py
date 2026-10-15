"""Production-shaped memory evidence and independent optional selection gates."""

import copy
from types import SimpleNamespace

import pytest

from civ_agent import memory_schema as tables
from civ_agent.advisory_delivery import finish_delivery
from civ_agent.advisory_store import AdvisoryStore, filter_pending_memory, pending_memory_requests, source_attempt_key
from civ_agent.civilization_state import CivilizationState
from civ_agent.decisions_ledger import canonical
from civ_agent.decisions_policy import DecisionsSettings
from civ_agent.decisions_tasks import parse_answers, request_body, request_id
from civ_agent.memory_records import public_record
from civ_agent.memory_store import RecordStore, encode, select_context


@pytest.fixture
def memory(tmp_path):
    value = CivilizationState(tmp_path / "memory", "Rome")
    value.bind("g1")
    yield value
    value.engine.dispose()


def write(memory, key, category="note", **fields):
    body = {"type": category, "state": "active", "createdTurn": 0, "updatedTurn": 0, **fields}
    with memory.transaction() as db:
        return RecordStore(db).write(key, category, body)


def start(memory, turn=20, name="job"):
    return SimpleNamespace(id=name, memory=memory.start_job(name, "gameplay", {}, game_turn=turn))


def freeze(memory, job, topics=(), **settings):
    return AdvisoryStore(memory).freeze(job, set(topics), DecisionsSettings(**settings))


def answer(name, choice):
    return {"name": name, "type": "choice", "choice": choice, "confidence": 0.99}


def index(evidence, key):
    return next(i for i, source in enumerate(evidence["retrieval"]) if source["key"] == key)


@pytest.mark.parametrize("turns", [(9, 10), (10, 10)])
def test_recorded_statement_chronology_and_exact_attribution(memory, turns):
    events = [
        {
            "id": sequence,
            "kind": "message",
            "turn": turn,
            "body": {
                "author": "Greece",
                "text": f"My claim {sequence}; do not turn this into verified knowledge.",
                "participants": ["Rome", "Greece"],
                "public": False,
                "turn": turn,
            },
        }
        for sequence, turn in zip((9, 10), turns, strict=True)
    ]
    memory.ingest("g1", events, 10)
    evidence = freeze(memory, start(memory), decisions_preservation_candidate_limit=1)
    source = evidence["preservation"][0]
    assert evidence["player"] == "Rome"
    assert source["key"] == "event:10"
    assert source["source"] == {
        "author": "Greece",
        "text": events[1]["body"]["text"],
        "participants": ["Rome", "Greece"],
        "public": False,
        "turn": turns[1],
        "kind": "message",
    }
    assert source["record"]["body"] == events[1]
    assert source["record"]["body"]["body"]["public"] is False
    assert evidence["coverage"]["statements"] == {"available": 2, "newEvidenceIncluded": 1}


@pytest.mark.parametrize("include", [None, "include", "omit"])
@pytest.mark.parametrize("priority", [None, "high", "normal"])
def test_independent_retrieval_answers_only_include_can_admit_old_note(memory, include, priority):
    write(memory, "old", text="Historical expedition report.")
    write(memory, "recent", text="Recent context.", updatedTurn=20)
    job = start(memory)
    evidence = freeze(memory, job)
    i = index(evidence, "old")
    answers = []
    if include is not None:
        answers.append(answer(f"retrieval_include:{i}", include))
    if priority is not None:
        answers.append(answer(f"retrieval_priority:{i}", priority))
    _, ranks = AdvisoryStore(memory).apply(job, evidence, {"answers": answers})
    selected, _ = select_context({**job.memory, "advisoryRanking": ranks}, memory.context_limit)
    assert ("old" in selected) == (include == "include")
    assert "recent" in selected
    if priority is not None:
        assert ranks["old"]["priority"] == priority


def test_raw_low_confidence_answers_do_not_grant_inclusion(memory):
    write(memory, "old", text="An old note.")
    job = start(memory)
    evidence = freeze(memory, job)
    raw = answer("retrieval_include:0", "include")
    raw["confidence"] = 0.1
    _, ranks = AdvisoryStore(memory).apply(
        job,
        evidence,
        {"answers": [], "rawAnswers": [raw], "questionResults": [{"name": raw["name"], "status": "low_confidence"}]},
    )
    assert ranks == {}
    selected, _ = select_context({**job.memory, "advisoryRanking": ranks}, memory.context_limit)
    assert "old" not in selected


def test_production_builder_parser_and_store_keep_priority_independent(memory):
    write(memory, "old", text="An old note.")
    job = start(memory)
    evidence = freeze(memory, job)
    body = request_body(evidence, DecisionsSettings())
    assert {question["name"] for question in body["questions"]} == {"retrieval_include:0", "retrieval_priority:0"}
    include = answer("retrieval_include:0", "include")
    include["confidence"] = 0.1
    result = parse_answers(body, {"answers": [include, answer("retrieval_priority:0", "high")]}, 0.9)
    assert [row["name"] for row in result["answers"]] == ["retrieval_priority:0"]
    _, ranks = AdvisoryStore(memory).apply(job, evidence, result)
    selected, _ = select_context({**job.memory, "advisoryRanking": ranks}, memory.context_limit)
    assert "old" not in selected


def test_maximum_unicode_sources_fit_request_budget_without_shortening_text(memory):
    text = "🙂" * 2000
    events = [
        {
            "id": i,
            "kind": "message",
            "turn": i,
            "body": {"author": "Greece", "text": text, "participants": ["Rome", "Greece"]},
        }
        for i in range(1, 9)
    ]
    memory.ingest("g1", events, 8)
    write(memory, "belief", "belief", claim="These attributed messages might matter.", uncertainty="high")
    evidence = freeze(memory, start(memory))
    assert len(canonical(request_body(evidence, DecisionsSettings())).encode("utf-8")) <= 65536
    assert 0 < len(evidence["preservation"]) < 8
    assert evidence["coverage"]["inputBudget"]["omitted"]["preservation"] > 0
    assert evidence["coverage"]["inputBudget"]["sourceTextTruncated"] is False
    assert all(source["source"]["text"] == text for source in evidence["preservation"])
    assert {source["key"] for source in evidence["preservation"]} == {
        source["key"] for source in evidence["newEvidence"] if "source" in source
    }
    with memory.transaction() as db:
        assert len(RecordStore(db).all(categories={"statement"})) == 8


def test_priority_changes_order_within_existing_character_budget(memory):
    write(memory, "a", text="Same sized source A.", updatedTurn=20)
    write(memory, "b", text="Same sized source B.", updatedTurn=20)
    job = start(memory)
    snapshot = {**job.memory, "advisoryRanking": {"b": {"priority": "high"}}}
    limit = 2 + len(encode(public_record("b", snapshot["records"]["b"])))
    chosen, _ = select_context(snapshot, limit)
    assert list(chosen) == ["b"]
    assert 2 + len(encode(public_record("b", chosen["b"]))) <= limit


def test_omit_does_not_suppress_explicit_review_notice(memory):
    write(memory, "old", text="Review this remembered report.")
    with memory.transaction() as db:
        RecordStore(db).write("notice:old", "notice", {"reasons": ["Referenced evidence changed."], "sources": []})
    job = start(memory)
    snapshot = copy.deepcopy(job.memory)
    snapshot["advisoryRanking"] = {"old": {"include": False, "priority": "normal"}}
    chosen, _ = select_context(snapshot, memory.context_limit)
    assert "old" in chosen


def test_classifier_priority_cannot_push_review_notice_out_of_budget(memory):
    write(memory, "review", text="Review this remembered report.")
    write(memory, "other", text="Other current context.", updatedTurn=20)
    with memory.transaction() as db:
        RecordStore(db).write("notice:review", "notice", {"reasons": ["Evidence changed."], "sources": []})
    job = start(memory)
    snapshot = {**job.memory, "advisoryRanking": {"other": {"include": True, "priority": "high"}}}
    limit = 2 + len(encode(public_record("review", snapshot["records"]["review"])))
    selected, _ = select_context(snapshot, limit)
    assert list(selected) == ["review"]


def test_terminal_attempts_are_context_specific_and_belief_evidence_survives_preservation(memory):
    memory.ingest(
        "g1",
        [{"id": 1, "kind": "message", "turn": 20, "body": {"author": "Greece", "text": "We will withdraw."}}],
        1,
    )
    write(memory, "belief", "belief", claim="Greece intends to remain.", uncertainty="high", sources=["event:1"])
    job = start(memory)
    store = AdvisoryStore(memory)
    original = freeze(memory, job)
    source = original["preservation"][0]
    context = {key: original[key] for key in ("player", "objectives", "topics")}
    key = source_attempt_key("preservation", source, **context)
    store.save(sourceAttempts={key: {"status": "low_confidence"}}, seenSources={"event:1": source["version"]})
    unchanged = freeze(memory, job)
    assert unchanged["preservation"] == []
    assert unchanged["newEvidence"][0] == source
    assert unchanged["beliefs"][0]["record"]["claim"] == "Greece intends to remain."
    changed = freeze(memory, job, topics={"Greece"})
    assert changed["preservation"] == [source]
    store.save(sourceAttempts={key: {"status": "completion_unknown"}})
    assert freeze(memory, job)["preservation"] == [source]


def test_cached_rankings_survive_terminal_filter_but_not_context_or_source_change(memory):
    write(memory, "old", text="Historical expedition report.")
    job = start(memory)
    store = AdvisoryStore(memory)
    evidence = freeze(memory, job)
    include = answer("retrieval_include:0", "include")
    priority = answer("retrieval_priority:0", "high")
    _, ranks = store.apply(job, evidence, {"answers": [include, priority]})
    context = {key: evidence[key] for key in ("player", "objectives", "topics")}
    store.save(
        sourceAttempts={
            source_attempt_key(field, evidence["retrieval"][0], **context): {"status": "accepted"}
            for field in ("retrieval_include", "retrieval_priority")
        }
    )
    memory.finish_job(job.id, True)
    job = start(memory, turn=21, name="next-job")
    filtered = freeze(memory, job)
    assert filtered["retrieval"] == []
    assert store.apply(job, filtered, {"answers": []})[1] == ranks
    changed_context = freeze(memory, job, topics={"new objective"})
    assert store.apply(job, changed_context, {"answers": []})[1] == {}
    write(memory, "old", text="A different revision.")
    assert store.apply(job, filtered, {"answers": []})[1] == {}


def test_attempt_keys_bind_belief_news_without_changing_retrieval_context(memory):
    write(memory, "old", text="Historical expedition report.")
    evidence = freeze(memory, start(memory))
    context = {key: evidence[key] for key in ("player", "objectives", "topics")}
    source = evidence["retrieval"][0]
    news = [{"key": "event:2", "version": 1, "record": {"body": {"text": "New claim."}}}]
    assert source_attempt_key("retrieval_include", source, **context) == source_attempt_key(
        "retrieval_include", source, **context, new_evidence=news
    )
    assert source_attempt_key("beliefs", source, **context) != source_attempt_key(
        "beliefs", source, **context, new_evidence=news
    )


def test_changed_objective_invalidates_both_cached_and_fresh_retrieval_answers(memory):
    write(memory, "old", text="Historical expedition report.")
    write(memory, "plan", "strategy", assessment="Explore the coast.")
    job = start(memory)
    store = AdvisoryStore(memory)
    evidence = freeze(memory, job)
    answers = [answer("retrieval_include:0", "include"), answer("retrieval_priority:0", "high")]
    assert store.apply(job, evidence, {"answers": answers})[1]["old"]["include"] is True
    write(memory, "plan", "strategy", assessment="Prepare for a defensive war.")
    assert store.apply(job, evidence, {"answers": []})[1] == {}
    assert store.apply(job, evidence, {"answers": answers})[1] == {}


def test_new_active_objective_invalidates_empty_captured_objective_set(memory):
    write(memory, "old", text="Historical expedition report.")
    job = start(memory)
    store = AdvisoryStore(memory)
    evidence = freeze(memory, job)
    answers = [answer("retrieval_include:0", "include"), answer("retrieval_priority:0", "high")]
    assert evidence["objectives"] == []
    assert store.objectives_current([], 20)
    assert store.apply(job, evidence, {"answers": answers})[1]["old"]["include"] is True
    write(memory, "plan", "strategy", assessment="Prepare for a defensive war.")
    assert not store.objectives_current([], 20)
    assert store.apply(job, evidence, {"answers": []})[1] == {}
    assert store.apply(job, evidence, {"answers": answers})[1] == {}


def test_objective_set_includes_only_current_active_unexpired_versions(memory):
    write(memory, "old-plan", "strategy", assessment="An expired objective.", expiresTurn=20)
    write(memory, "retired-plan", "strategy", assessment="A retired objective.", state="retired")
    store = AdvisoryStore(memory)
    assert not store.objectives_current([], 19)
    job = start(memory)
    assert freeze(memory, job)["objectives"] == []
    assert store.objectives_current([], 20)


def test_real_belief_and_recorded_prior_current_evidence_are_bounded_and_version_checked(memory):
    write(memory, "belief", "belief", claim="Our scout is healthy at its old position.", uncertainty="low")
    memory.observe(
        "overview",
        {"turn": 19, "units": [{"handle": "u1", "name": "Scout", "health": 100, "position": {"x": 0, "y": 0}}]},
    )
    memory.observe(
        "overview",
        {"turn": 20, "units": [{"handle": "u1", "name": "Scout", "health": 40, "position": {"x": 1, "y": 0}}]},
    )
    job = start(memory)
    evidence = freeze(memory, job)
    assert evidence["beliefs"][0]["record"]["claim"] == "Our scout is healthy at its old position."
    source = evidence["newEvidence"][0]
    assert source["previousSource"] == {"key": "observation:overview", "version": 1}
    change = next(
        row for row in source["record"]["body"]["comparison"]["fields"] if row["field"] == "recordedUnitFacts"
    )
    assert change["removed"][0]["health"] == 100
    assert change["added"][0]["health"] == 40
    assert evidence["coverage"]["beliefEvidence"]["detailObservationsIncluded"] is False
    store = AdvisoryStore(memory)
    notices, _ = store.apply(job, evidence, {"answers": [answer("beliefs:0", "reconsider")]})
    assert len(notices["beliefNotices"]) == 1
    memory.observe("overview", {"turn": 21, "units": []})
    notices, _ = store.apply(job, evidence, {"answers": [answer("beliefs:0", "reconsider")]})
    assert notices["beliefNotices"] == []


def test_truncated_recorded_evidence_does_not_claim_absence(memory):
    write(memory, "belief", "belief", claim="A scout remains at this location.", uncertainty="high")
    units = [{"handle": f"u{i}", "name": "Scout", "health": 100} for i in range(20)]
    memory.observe("overview", {"turn": 19, "units": units})
    latest = copy.deepcopy(units)
    latest[0]["health"] = 20
    memory.observe("overview", {"turn": 20, "units": latest})
    evidence = freeze(memory, start(memory))
    source = evidence["newEvidence"][0]
    assert source["coverage"]["omitted"]["recordedUnitFacts"] == 4
    change = next(
        row for row in source["record"]["body"]["comparison"]["fields"] if row["field"] == "recordedUnitFacts"
    )
    assert change["complete"] is False
    assert "removed" not in change
    assert len(change["beforeExcerpt"]) == len(change["currentExcerpt"]) == 16


def test_missing_current_unit_evidence_is_unknown_instead_of_a_disappearance(memory):
    write(memory, "belief", "belief", claim="A scout remains at this location.", uncertainty="high")
    memory.observe("overview", {"turn": 19, "units": [{"handle": "u1", "name": "Scout", "health": 100}]})
    memory.observe("overview", {"turn": 20, "researchQueue": ["Writing"]})
    evidence = freeze(memory, start(memory))
    source = evidence["newEvidence"][0]
    assert "recordedUnitFacts" in source["coverage"]["unknownFields"]
    change = next(
        row for row in source["record"]["body"]["comparison"]["fields"] if row["field"] == "recordedUnitFacts"
    )
    assert change["complete"] is False
    assert "removed" not in change


def test_beliefs_require_actual_new_evidence_and_source_version(memory):
    write(memory, "belief", "belief", claim="Greece will remain neutral.", uncertainty="high")
    job = start(memory)
    assert freeze(memory, job)["beliefs"] == []
    memory.observe("overview", {"turn": 20, "units": []})
    evidence = freeze(memory, job)
    write(memory, "belief", "belief", claim="Greece may join the war.", uncertainty="medium")
    notices, _ = AdvisoryStore(memory).apply(job, evidence, {"answers": [answer("beliefs:0", "reconsider")]})
    assert notices["beliefNotices"] == []


def test_current_unchanged_recorded_facts_do_not_trigger_belief_questions(memory):
    write(memory, "belief", "belief", claim="The current research is unchanged.", uncertainty="medium")
    memory.observe("overview", {"turn": 19, "researchQueue": ["Writing"]})
    first = start(memory, turn=19)
    evidence = freeze(memory, first)
    context = {key: evidence[key] for key in ("player", "objectives", "topics")}
    key = source_attempt_key("beliefs", evidence["beliefs"][0], **context, new_evidence=evidence["newEvidence"])
    AdvisoryStore(memory).save(sourceAttempts={key: {"status": "accepted"}})
    memory.finish_job(first.id, True)
    memory.observe("overview", {"turn": 20, "researchQueue": ["Writing"]})
    later = freeze(memory, start(memory, name="later"))
    assert later["newEvidence"][0]["version"] == 2
    assert later["newEvidence"][0]["record"]["body"]["comparison"]["baselineAvailable"] is True
    assert later["beliefs"] == []


def test_belief_context_ignores_transport_and_comparison_churn_but_tracks_facts_and_objectives(memory):
    write(memory, "belief", "belief", claim="Our current research is suitable.", uncertainty="medium")
    memory.ingest("g1", [{"id": 1, "kind": "message", "turn": 20, "body": {"author": "Greece", "text": "A claim."}}], 1)
    memory.observe("overview", {"turn": 20, "revision": "r1", "researchQueue": ["Writing"]})
    first = start(memory)
    store = AdvisoryStore(memory)
    initial = freeze(memory, first)
    context = {key: initial[key] for key in ("player", "objectives", "topics")}
    source = initial["beliefs"][0]
    key = source_attempt_key("beliefs", source, **context, new_evidence=initial["newEvidence"])
    store.save(sourceAttempts={key: {"status": "accepted"}})
    memory.finish_job(first.id, True)
    memory.observe("overview", {"turn": 25, "revision": "transport-only", "researchQueue": ["Writing"]})
    second = start(memory, turn=25, name="second")
    same = freeze(memory, second)
    assert same["beliefs"] == []
    assert source_attempt_key("beliefs", source, **context, new_evidence=same["newEvidence"]) == key
    assert initial["newEvidence"][-1]["version"] != same["newEvidence"][-1]["version"]
    assert initial["newEvidence"][-1]["previousSource"] is None
    assert same["newEvidence"][-1]["previousSource"]["version"] == 1
    memory.finish_job(second.id, True)
    memory.observe("overview", {"turn": 30, "researchQueue": ["Bronze Working"]})
    third = start(memory, turn=30, name="third")
    changed = freeze(memory, third)
    assert changed["beliefs"]
    changed_key = source_attempt_key("beliefs", source, **context, new_evidence=changed["newEvidence"])
    assert changed_key != key
    store.save(sourceAttempts={key: {"status": "accepted"}, changed_key: {"status": "accepted"}})
    assert freeze(memory, third)["beliefs"] == []
    memory.finish_job(third.id, True)
    write(memory, "plan", "strategy", assessment="Prioritize a defensive technology.")
    fourth = start(memory, turn=35, name="fourth")
    assert freeze(memory, fourth)["beliefs"]


def test_belief_previous_observation_follows_parent_and_never_discarded_future(memory):
    write(memory, "belief", "belief", claim="Research remains Pottery.", uncertainty="low")
    memory.observe("overview", {"turn": 19, "researchQueue": ["Pottery"]})
    memory.observe("overview", {"turn": 20, "researchQueue": ["DISCARDED_FUTURE_SECRET"]})
    # Recovery restores the old reachable head while immutable discarded rows
    # remain available only to audit/recovery, never current player context.
    with memory.transaction() as db:
        db.execute(tables.heads.update().where(tables.heads.c.key == "observation:overview").values(version=1))
    memory.observe("overview", {"turn": 20, "researchQueue": ["Writing"]})
    evidence = freeze(memory, start(memory))
    source = evidence["newEvidence"][0]
    assert source["version"] == 3
    assert source["previousSource"] == {"key": "observation:overview", "version": 1}
    research = next(row for row in source["record"]["body"]["comparison"]["fields"] if row["field"] == "researchQueue")
    assert research["removed"] == ["Pottery"]
    assert research["added"] == ["Writing"]
    assert "DISCARDED_FUTURE_SECRET" not in canonical(evidence)


def test_pending_original_receipt_survives_transport_churn_and_filters_retrieval_pair(memory):
    write(memory, "belief", "belief", claim="Our research is suitable.", uncertainty="medium")
    memory.observe("overview", {"turn": 20, "researchQueue": ["Writing"]})
    first = start(memory)
    original = freeze(memory, first)
    identifier = request_id(original)
    context = {key: original[key] for key in ("player", "objectives", "topics")}
    attempts = {}
    for prefix in ("retrieval_include", "retrieval_priority", "beliefs"):
        source = original["beliefs"][0]
        key = source_attempt_key(prefix, source, **context, new_evidence=original["newEvidence"])
        attempts[key] = {
            "status": "accepted" if prefix == "retrieval_include" else "completion_unknown",
            "requestId": identifier,
        }
    state = {"sourceAttempts": attempts}
    AdvisoryStore(memory).save(**state)
    memory.finish_job(first.id, True)
    memory.observe("overview", {"turn": 25, "revision": "transport-only", "researchQueue": ["Writing"]})
    later = freeze(memory, start(memory, turn=25, name="later"))
    assert request_id(later) != identifier
    assert pending_memory_requests(later, state) == [identifier]
    before = canonical(later)
    filtered = filter_pending_memory(later, state)
    assert filtered["retrieval"] == []
    assert filtered["beliefs"] == []
    assert filtered["newEvidence"] == later["newEvidence"]
    assert filtered["coverage"]["pendingMemory"]["omitted"] == {"retrieval": 1, "preservation": 0, "beliefs": 1}
    assert canonical(later) == before


def test_pending_semantic_context_does_not_hide_genuinely_new_belief_evidence(memory):
    write(memory, "belief", "belief", claim="Our research is suitable.", uncertainty="medium")
    memory.observe("overview", {"turn": 20, "researchQueue": ["Writing"]})
    job = start(memory)
    original = freeze(memory, job)
    context = {key: original[key] for key in ("player", "objectives", "topics")}
    key = source_attempt_key("beliefs", original["beliefs"][0], **context, new_evidence=original["newEvidence"])
    state = {"sourceAttempts": {key: {"status": "broker_unavailable", "requestId": request_id(original)}}}
    memory.observe("overview", {"turn": 21, "researchQueue": ["Bronze Working"]})
    changed = freeze(memory, job)
    assert pending_memory_requests(changed, state) == []
    assert filter_pending_memory(changed, state)["beliefs"] == changed["beliefs"]


def test_cached_retrieval_bindings_preserve_original_indices_and_later_exposure(memory):
    write(memory, "a", text="An old note A.")
    write(memory, "z", text="An old note Z.")
    first = start(memory)
    store = AdvisoryStore(memory)
    evidence = freeze(memory, first)
    body = request_body(evidence, DecisionsSettings())
    rows = [
        answer(question["name"], "include" if question["name"].startswith("retrieval_include") else "high")
        for question in body["questions"]
    ]
    result = {**parse_answers(body, {"answers": rows}, 0.9), "requestId": "original-request"}
    store.apply(first, evidence, result)
    memory.finish_job(first.id, True)
    second = start(memory, turn=25, name="later")
    later = freeze(memory, second)
    bindings = store.cached_retrieval_bindings(second, later)
    assert {row["name"] for row in bindings} == {question["name"] for question in body["questions"]}
    assert {row["requestId"] for row in bindings} == {"original-request"}
    source = next(source for source in evidence["retrieval"] if source["key"] == "z")
    exposed = {**second.memory, "contextIds": ["z"], "delivered": {"z": source["version"]}, "advisories": {}}
    statuses = finish_delivery(bindings, exposed)
    assert all(row["status"] == "delivered" for row in statuses if row["source"]["key"] == "z")
    assert all(row["status"] == "not_selected" for row in statuses if row["source"]["key"] == "a")
    assert "An old note" not in canonical(bindings)
    assert store.cached_retrieval_bindings(second, {**later, "topics": ["different context"]}) == []
    write(memory, "z", text="A changed revision.")
    assert all(row["source"]["key"] != "z" for row in store.cached_retrieval_bindings(second, later))


def test_cached_bindings_keep_accepted_include_when_priority_arrives_separately(memory):
    write(memory, "old", text="An old note.")
    job = start(memory)
    store = AdvisoryStore(memory)
    evidence = freeze(memory, job)
    store.apply(job, evidence, {"requestId": "include-request", "answers": [answer("retrieval_include:0", "include")]})
    store.apply(job, evidence, {"requestId": "priority-request", "answers": [answer("retrieval_priority:0", "high")]})
    rows = store.cached_retrieval_bindings(job, evidence)
    assert {(row["name"], row["requestId"]) for row in rows} == {
        ("retrieval_include:0", "include-request"),
        ("retrieval_priority:0", "priority-request"),
    }
    write(memory, "plan", "strategy", assessment="A newly active objective.")
    assert store.cached_retrieval_bindings(job, evidence) == []
