import copy
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

import pytest

from civ_agent import decision_contracts as contracts
from civ_agent.decision_contracts import DecisionContract, compile_decision_contract
from civ_agent.schemas import BoundaryModel
from civ_agent.strict_schema import StrictSchema


@pytest.fixture(autouse=True)
def clear_compilation():
    contracts.clear_decision_contract_cache()
    yield
    contracts.clear_decision_contract_cache()


def catalogue(description="Read research"):
    return [{"name": "research_details", "description": description, "inputSchema": {"type": "object"}}]


def test_cached_compilation_is_immutable_and_matches_fresh_contract(monkeypatch):
    tools = catalogue()
    before = copy.deepcopy(tools)
    cached = compile_decision_contract("gameplay", tools)
    fresh = DecisionContract("gameplay", tools)
    decision = {"kind": "inspect", "operations": [{"tool": "research_details", "arguments": {"names": ["Writing"]}}]}
    assert cached.schema == fresh.schema
    assert cached.encode(decision) == fresh.encode(decision)
    assert cached.parse(cached.encode(decision)) == fresh.parse(fresh.encode(decision))
    wire = cached.schema
    wire.clear()
    original = cached.codec.original
    original.clear()
    definitions = cached.codec.definitions
    definitions.clear()
    with pytest.raises(AttributeError):
        cached.purpose = "military"
    with pytest.raises(TypeError):
        cast(Any, cached.models)["research_details"] = BoundaryModel
    with pytest.raises(AttributeError):
        cached.codec.compact = False
    assert cached.schema == fresh.schema and tools == before
    monkeypatch.setattr(StrictSchema, "_compile", lambda *_: pytest.fail("Conversion recompiled a schema"))
    assert cached.parse(cached.encode(decision)) == fresh.parse(fresh.encode(decision))
    assert compile_decision_contract("gameplay", tools) is cached


def test_full_catalogue_grants_role_policy_and_report_identity_separate_entries():
    base = compile_decision_contract("gameplay", catalogue())
    assert compile_decision_contract("gameplay", list(reversed(catalogue()))) is base
    assert compile_decision_contract("gameplay", catalogue("Different granted description")) is not base
    assert compile_decision_contract("economy", catalogue()) is not base
    assert compile_decision_contract("gameplay", catalogue(), compact=False) is not base
    assert compile_decision_contract("gameplay", catalogue(), policy_key={"version": 2}) is not base
    assert compile_decision_contract("gameplay", []) is not base

    class Report(BoundaryModel):
        assessment: str

    class AnotherReport(BoundaryModel):
        assessment: str

    report = compile_decision_contract("review", [], report_model=Report)
    assert compile_decision_contract("review", [], report_model=Report) is report
    assert compile_decision_contract("review", [], report_model=AnotherReport) is not report
    assert report.parse(report.encode({"kind": "submit_report", "report": {"assessment": "Evidence"}})) == {
        "kind": "submit_report",
        "report": {"assessment": "Evidence"},
    }


def test_concurrent_cache_and_parse_preserve_validator_and_canonical_semantics():
    decision = {"kind": "inspect", "operations": [{"tool": "research_details", "arguments": {"names": ["Pottery"]}}]}

    def use(_):
        contract = compile_decision_contract("gameplay", catalogue())
        return contract, contract.parse(contract.encode(decision))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(use, range(32)))
    assert all(result is results[0][0] for result, _ in results)
    assert all(value == decision for _, value in results)
    metrics = contracts.decision_contract_cache_metrics()
    assert metrics["misses"] == 1 and metrics["hits"] == 31


def test_cache_is_bounded_and_failed_compilation_is_not_saved(monkeypatch):
    monkeypatch.setattr(contracts, "_CONTRACT_CACHE_LIMIT", 2)
    first = compile_decision_contract("gameplay", catalogue("first"))
    compile_decision_contract("gameplay", catalogue("second"))
    compile_decision_contract("gameplay", catalogue("third"))
    assert contracts.decision_contract_cache_metrics()["entries"] == 2
    assert compile_decision_contract("gameplay", catalogue("first")) is not first
    before = contracts.decision_contract_cache_metrics()["entries"]
    with pytest.raises(KeyError):
        compile_decision_contract("gameplay", [{"name": "unknown_grant", "inputSchema": {}}])
    assert contracts.decision_contract_cache_metrics()["entries"] == before
    assert contracts.decision_contract_cache_metrics()["failures"] == 1
