"""Bounded current-policy routing evaluation, isolated from running games.

The key is read only in this process. A durable reservation precedes each API
call; an interrupted request is never dispatched again. Reuse the same trial
directory to recover cached results without resetting the budget or call cap.
"""

import argparse
import asyncio
import fcntl
import hashlib
import json
import math
import os
from decimal import Decimal
from functools import partial
from pathlib import Path

from civ_agent.decisions_backends import BACKENDS
from civ_agent.decisions_broker import read_decisions_key
from civ_agent.decisions_ledger import NANODOLLARS_PER_INPUT_TOKEN, RATE_VERSION, estimate_tokens, fingerprint
from civ_agent.diplomacy_decisions import (
    POLICY_VERSION,
    DecisionsClient,
)
from civ_agent.diplomacy_decisions import request_body as routing_request_body
from civ_agent.diplomacy_replay import evaluate, load_fixture
from civ_agent.protocol import JsonObject
from civ_agent.tls import private_directory, regular_file, write_private, write_private_json

# This is the explicitly paid historical Luna trial; normal runtime selection
# is owned by DecisionsSettings. Keep its frozen journals provider-specific.
MODEL = BACKENDS["luna"].model
request_body = partial(routing_request_body, backend="luna")


def reuse_snapshot(root, fixture, fixture_bytes):
    """Read a prior immutable journal once; never replace or replenish its accounting."""
    root = Path(root).resolve()
    for name in ("trial.json", "fixture.json"):
        regular_file(root / name, private=True)
    frozen = (root / "fixture.json").read_bytes()
    if frozen != fixture_bytes:
        raise ValueError("Reuse requires the exact same frozen fixture bytes and labels")
    journal_bytes = (root / "trial.json").read_bytes()
    journal = json.loads(journal_bytes)
    configuration = journal.get("configuration", {})
    if (
        configuration.get("version") != 2
        or configuration.get("policyVersion") != POLICY_VERSION
        or configuration.get("model") != MODEL
        or configuration.get("fixtureHash") != fingerprint(fixture)
    ):
        raise ValueError("Reusable trial policy, model or fixture does not match the current protocol")
    return {
        "source": {
            "trialDirectory": str(root),
            "journalSha256": hashlib.sha256(journal_bytes).hexdigest(),
            "fixtureSha256": hashlib.sha256(frozen).hexdigest(),
            "sourcePolicyVersion": POLICY_VERSION,
        },
        "requests": journal.get("requests", {}),
    }


def reusable_response(snapshot, case, body):
    expected = request_body(case["direction"], case["bundle"])
    if expected != body:
        raise ValueError("Routing request changed; prior classifier responses cannot be reused")
    identifier = fingerprint({"case": case, "policy": POLICY_VERSION, "model": MODEL, "request": expected})
    row = snapshot["requests"].get(identifier)
    if (
        not isinstance(row, dict)
        or row.get("caseId") != case["id"]
        or row.get("caseHash") != fingerprint(case)
        or row.get("model") != MODEL
        or row.get("policyVersion") != POLICY_VERSION
        or row.get("request") != body
        or not isinstance(row.get("response"), dict)
        or row["response"].get("policyVersion") != POLICY_VERSION
    ):
        raise ValueError("Prior trial has no completed exact current routing request for this case")
    return {"response": row["response"], "provenance": {**snapshot["source"], "sourceRequestId": identifier}}


class TrialStore:
    """An isolated write-before-dispatch journal, never a production game account."""

    def __init__(self, root, fixture, *, max_calls, budget_usd, reuse_source=None, fixture_bytes=None):
        if type(max_calls) is not int or max_calls < 0:
            raise ValueError("max_calls must be a nonnegative integer")
        budget = Decimal(str(budget_usd))
        if not budget.is_finite() or budget < 0:
            raise ValueError("budget_usd must be finite and nonnegative")
        if reuse_source is not None and not isinstance(fixture_bytes, bytes):
            raise ValueError("Reuse requires frozen fixture bytes")
        self.root = private_directory(root)
        self.state: JsonObject = {}
        self.path = self.root / "trial.json"
        self.lock = os.open(self.root / ".trial.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.lock)
            self.lock = None
            raise ValueError("This routing trial already has a running process") from None
        configuration = {
            "version": 2,
            "fixtureHash": fingerprint(fixture),
            "maxCalls": max_calls,
            "budgetNanodollars": int(budget * 1_000_000_000),
            "policyVersion": POLICY_VERSION,
            "model": MODEL,
            "promptHashes": {
                direction: fingerprint(request_body(direction, {})) for direction in ("incoming", "outgoing")
            },
            "rateVersion": RATE_VERSION,
        }
        if reuse_source is not None:
            configuration["reuseSource"] = reuse_source
            configuration["fixtureBytesSha256"] = hashlib.sha256(fixture_bytes or b"").hexdigest()
        try:
            if self.path.exists():
                regular_file(self.path, private=True)
                self.state = json.loads(self.path.read_text())
                if self.state.get("configuration") != configuration:
                    raise ValueError("Existing trial configuration is immutable; use its original fixture and limits")
                frozen = self.root / "fixture.json"
                regular_file(frozen, private=True)
                if fingerprint(json.loads(frozen.read_text())) != configuration["fixtureHash"]:
                    raise ValueError("Frozen trial fixture changed")
                if fixture_bytes is not None and frozen.read_bytes() != fixture_bytes:
                    raise ValueError("Frozen trial fixture bytes changed")
            else:
                if any(path.name != ".trial.lock" for path in self.root.iterdir()):
                    raise ValueError("A new routing trial requires an empty private directory")
                self.state = {"configuration": configuration, "requests": {}}
                if fixture_bytes is not None:
                    write_private(self.root / "fixture.json", fixture_bytes)
                else:
                    write_private_json(self.root / "fixture.json", fixture)
                self._save()
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None

    def _save(self):
        write_private_json(self.path, self.state)

    def status(self):
        requests = list(self.state["requests"].values())
        reused = list(self.state.get("reused", {}).values())
        return {
            "calls": len(requests),
            "maxCalls": self.state["configuration"]["maxCalls"],
            "budgetNanodollars": self.state["configuration"]["budgetNanodollars"],
            "spentNanodollars": sum(row["spentNanodollars"] for row in requests),
            "reservedNanodollars": sum(row["reservedNanodollars"] for row in requests),
            "unknownCompletions": sum(row.get("response") is None for row in requests),
            "reusedResponses": len(reused),
            "reusedFallbacks": {
                reason: sum(row["response"].get("fallback") == reason for row in reused)
                for reason in sorted({row["response"]["fallback"] for row in reused if row["response"].get("fallback")})
            },
            "rateVersion": RATE_VERSION,
            "limitKind": "local_estimated_spending_ceiling",
        }

    def reuse(self, case, body, saved):
        identifier = fingerprint({"case": case, "policy": POLICY_VERSION, "model": MODEL, "request": body})
        row = {
            "caseId": case["id"],
            "caseHash": fingerprint(case),
            "policyVersion": POLICY_VERSION,
            "model": MODEL,
            "request": body,
            **saved,
        }
        prior = self.state.setdefault("reused", {}).get(identifier)
        if prior is not None:
            if prior != row:
                raise ValueError("Previously reused response or provenance changed")
        else:
            self.state["reused"][identifier] = row
            self._save()
        return row["response"]

    def reserve(self, case, body):
        identifier = fingerprint({"case": case, "policy": POLICY_VERSION, "model": MODEL, "request": body})
        prior = self.state["requests"].get(identifier)
        if prior is not None:
            return (
                identifier,
                prior.get("response") or {"answers": [], "fallback": "completion_unknown", "usage": {}},
                False,
            )
        status = self.status()
        reservation = estimate_tokens(body) * NANODOLLARS_PER_INPUT_TOKEN
        reason = None
        if status["calls"] >= status["maxCalls"]:
            reason = "trial_call_cap"
        elif status["spentNanodollars"] + status["reservedNanodollars"] + reservation > status["budgetNanodollars"]:
            reason = "trial_budget_exhausted"
        if reason:
            return identifier, {"answers": [], "fallback": reason, "usage": {}, "rawUnavailable": True}, False
        self.state["requests"][identifier] = {
            "caseId": case["id"],
            "caseHash": fingerprint(case),
            "policyVersion": POLICY_VERSION,
            "model": MODEL,
            "request": body,
            "reservedNanodollars": reservation,
            "spentNanodollars": 0,
            "response": None,
        }
        self._save()
        return identifier, None, True

    def complete(self, identifier, response):
        row = self.state["requests"][identifier]
        if row["response"] is not None:
            raise ValueError("Routing trial request already completed")
        row["response"] = response
        usage = response.get("usage", {})
        tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
        if type(tokens) is int and 0 <= tokens <= 100_000_000:
            row["reservedNanodollars"] = 0
            row["spentNanodollars"] = tokens * NANODOLLARS_PER_INPUT_TOKEN
        self._save()


async def trial(
    root,
    key_file,
    fixtures,
    *,
    max_calls=400,
    budget_usd=0.10,
    client=None,
    reuse_trial=None,
):
    fixture = load_fixture(fixtures)
    reuse = reuse_snapshot(reuse_trial, fixture, Path(fixtures).read_bytes()) if reuse_trial is not None else None
    cached = {}
    if reuse is not None:
        # Validate every reuse entry before making any new paid request.
        for case in fixture["cases"]:
            if case.get("protected"):
                continue
            body = request_body(case["direction"], case["bundle"])
            cached[case["id"]] = reusable_response(reuse, case, body)
    store = TrialStore(
        root,
        fixture,
        max_calls=max_calls,
        budget_usd=budget_usd,
        reuse_source=reuse["source"] if reuse is not None else None,
        fixture_bytes=Path(fixtures).read_bytes() if reuse is not None else None,
    )
    try:
        if client is None:
            client = DecisionsClient(read_decisions_key(key_file))
        responses = {}
        for case in fixture["cases"]:
            if case.get("protected"):
                continue
            body = request_body(case["direction"], case["bundle"])
            if case["id"] in cached:
                responses[case["id"]] = store.reuse(case, body, cached[case["id"]])
                continue
            identifier, response, dispatch = store.reserve(case, body)
            if dispatch:
                response = await client.evaluate(body)
                response = {**response, "policyVersion": POLICY_VERSION, "model": body["model"]}
                store.complete(identifier, response)
            if not isinstance(response, dict):
                raise ValueError("Routing trial response envelope is unavailable")
            responses[case["id"]] = {**response, "policyVersion": POLICY_VERSION}
        summary = {
            "configuration": store.state["configuration"],
            "accounting": store.status(),
            "comparisonThreshold": 0.90,
            "policyVersion": POLICY_VERSION,
            "evaluation": evaluate(fixture["cases"], responses, threshold=0.90),
            "limitations": "Frozen-input classifier trial only. No gameplay treatment, ledger reset, threshold selection, or benchmark savings claim. Labels are provisional and source provenance is reported separately.",
        }
        write_private_json(store.root / "responses.json", responses)
        write_private_json(store.root / "summary.json", summary)
        return summary
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial-dir", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, default=Path("tests/fixtures/diplomacy-routing-calibration.json"))
    parser.add_argument("--max-calls", type=int, default=400)
    parser.add_argument("--budget-usd", type=float, default=0.10)
    parser.add_argument(
        "--reuse-trial", type=Path, help="Reuse exact current-policy responses from this private frozen trial"
    )
    args = parser.parse_args()
    if not math.isfinite(args.budget_usd):
        parser.error("Budget must be finite")
    result = asyncio.run(
        trial(
            args.trial_dir,
            args.key_file,
            args.fixtures,
            max_calls=args.max_calls,
            budget_usd=args.budget_usd,
            reuse_trial=args.reuse_trial,
        )
    )
    # Player text remains in private artifacts, never the shell summary.
    print(
        json.dumps(
            {
                "accounting": result["accounting"],
                "comparisonThreshold": result["comparisonThreshold"],
                "policyVersion": result["policyVersion"],
                "evaluation": {key: value for key, value in result["evaluation"].items() if key != "cases"},
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
