"""Opt-in live acceptance: real Rome/Greece models, scripted human-controller participants.

Start the mixed host and both agent services first. This operator harness never supplies
agent consent. It uses the human trade CLI for France and ordinary player commands for
France/Egypt. The native AI runs inside normal END_TURN processing.
"""

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

from civ_agent.commands.agent import player_credentials
from civ_agent.host_connection import HostConnection
from civ_agent.protocol import JsonObject
from civ_agent.scheduler import Agent, Operator, agent_services, operator_token, player_prompt
from civ_agent.tls import endpoint_configuration
from civ_agent.turn_finalization import finalize_turn


async def exercise(root: Path, model: str, reasoning: str, resume: bool = False):
    origin, ca = endpoint_configuration(root, None, None)
    operator = Operator(origin, operator_token(root / "operator.json"), ca)
    services = agent_services(root)
    assert set(services) == {"Rome", "Greece"}
    lease = str(uuid.uuid4())
    record: JsonObject = {
        "model": model,
        "reasoningEffort": reasoning,
        "humanControllers": "scripted France and Egypt",
        "activations": [],
        "humanTrades": [],
    }
    output = root / "mixed-live.json"
    if resume:
        record = json.loads(output.read_text())
        assert record["model"] == model and record["reasoningEffort"] == reasoning
        lease = record.get("leaseId") or record.get("final", {}).get("leaseId") or lease
        if error := record.pop("error", None):
            record.setdefault("driverInterruptions", []).append(error)
        record.pop("completed", None)
    else:
        assert not output.exists(), "Use --resume to continue the recorded exercise"
    record["leaseId"] = lease

    def save():
        output.write_text(json.dumps(record, indent=2) + "\n")

    def op(operation, **fields):
        return operator.call(operation, **({"leaseId": lease} if operation != "status" else {}), **fields)

    async def tool(host, name, fields):
        result = await host.tool(name, fields)
        if "error" in result:
            raise RuntimeError(f"{name} failed: {result['error']}")
        return result

    async def human_trade():
        _, token = player_credentials(root / "players.json", "France")
        async with HostConnection(origin + "/mcp", token, ca) as host:
            state = await tool(host, "player_turn", {})
            ledger = await tool(host, "player_communication", {"op": "state", "epoch": state["epoch"]})
            for summary in ledger.get("trades", []):
                if summary["status"] != "PENDING" or summary["author"] != "Rome":
                    continue
                result = await tool(
                    host,
                    "player_communication",
                    {"op": "trade_read", "epoch": state["epoch"], "proposal": summary["proposal"]},
                )
                trade = result["trade"]
                assert trade["mode"] == "SHARED" and trade["partner"] == "France"
                clauses = trade["clauses"]
                assert len(clauses) == 1 and clauses[0]["item"]["type"] == "Gold"
                assert {clauses[0]["from"], clauses[0]["to"]} == {"Rome", "France"}
                assert 5 <= clauses[0]["item"]["amount"] <= 10
                # France asks Rome for ten gold, including when Rome opens by asking
                # France for a gift. The model must choose whether to accept this counter.
                decision = "accept" if clauses[0]["from"] == "Rome" and clauses[0]["item"]["amount"] == 10 else "offer"
                command = [
                    str(Path(sys.executable).with_name("civ-agent")),
                    "trade",
                    decision,
                    "France",
                    "--state-dir",
                    str(root),
                    "--proposal",
                    trade["proposal"],
                ]
                if decision == "offer":
                    command += [
                        "--partner",
                        "Rome",
                        "--give-json",
                        "[]",
                        "--receive-json",
                        json.dumps([{"type": "Gold", "name": "Gold", "amount": 10, "duration": -1}]),
                    ]
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                transcript, _ = await process.communicate(b"y\n")
                assert process.returncode == 0, transcript.decode()
                record["humanTrades"].append(
                    {
                        "proposal": trade["proposal"],
                        "version": trade["version"],
                        "decision": decision,
                        "transcript": transcript.decode(),
                    }
                )
                save()
                print("France used human trade CLI:", decision, flush=True)

    async def human_turn(token):
        async with HostConnection(origin + "/mcp", token, ca) as host:
            for _ in range(60):
                turn = await tool(host, "player_turn", {})
                fields = {"epoch": turn["epoch"], "revision": turn["revision"], "requestId": str(uuid.uuid4())}
                alert = next(iter(turn["alerts"]), None)
                city = next((c for c in turn["cities"] if not c["puppet"] and not c["constructionQueue"]), None)
                if alert and alert.get("canAcknowledge"):
                    fields.update(kind="ACKNOWLEDGE_ALERT", target=alert["handle"])
                elif alert and alert.get("nativeDiplomacy"):
                    native = alert["nativeDiplomacy"]
                    choice = next((c for c in native["options"] if c["name"] == "Acknowledge"), None)
                    assert choice is not None, native
                    await tool(
                        host,
                        "player_communication",
                        {
                            "op": "send",
                            "epoch": turn["epoch"],
                            "requestId": str(uuid.uuid4()),
                            "to": native["civilizationId"],
                            "text": "Acknowledged.",
                            "nativeAction": {
                                "kind": "RESPOND_DIPLOMACY",
                                "target": alert["handle"],
                                "selection": choice["name"],
                                "revision": turn["revision"],
                            },
                        },
                    )
                    continue
                elif not turn["researchQueue"]:
                    fields.update(kind="SET_RESEARCH_QUEUE", queue=[turn["researchOptions"][0]])
                elif city:
                    fields.update(
                        kind="QUEUE_CONSTRUCTION", target=city["handle"], selection=city["constructionOptions"][0]
                    )
                elif any(p["kind"] == "Policy" for p in turn["pending"]):
                    fields.update(kind="ADOPT_POLICY", selection=turn["policyOptions"][0])
                elif turn["canEndTurn"]:
                    fields.update(
                        kind="END_TURN",
                        reviewId=turn["turnReview"]["id"],
                        deferReason="Hold units during this guided diplomacy acceptance exercise.",
                    )
                else:
                    raise RuntimeError(f"Unscripted human decision: {turn['pending']}")
                await tool(host, "player_command", fields)
                if fields["kind"] == "END_TURN":
                    return
            raise RuntimeError("Human turn command limit exceeded")

    acquired = op("acquire")["status"]
    epoch = acquired["epoch"]
    assert record.get("epoch", epoch) == epoch
    record["epoch"] = epoch
    op("job_limits", maxInvocations=24, maxConcurrent=4, maxPerCivilization=2)
    try:
        for previous in record["activations"]:
            service = services[previous["civilization"]]
            status = service.call("status", activationId=previous["activationId"])
            assert status["phase"] == "COMPLETED" and acquired["handoff"] > status["handoff"]
            # Repair a driver interruption after the already-confirmed native handoff.
            # Finalization archives the turn; it never advances or mutates the game.
            await asyncio.to_thread(
                finalize_turn,
                service,
                {
                    "activationId": previous["activationId"],
                    "epoch": epoch,
                    "gameTurn": previous["turn"],
                    "handoff": status["handoff"],
                    "confirmedHandoff": status["handoff"] + 1,
                },
            )
        for civ, service in services.items():
            token = op("connect", epoch=epoch, civilization=civ)["playerToken"]
            service.call(
                "connect", epoch=epoch, playerToken=token, model=model, reasoningEffort=reasoning, timeoutSeconds=600
            )
        op("unbarrier")
        deadline = time.monotonic() + 2400
        while True:
            before = op("status")["status"]
            if before["turn"] >= 2:
                break
            civ = before["activeCivilization"]
            activation = str(uuid.uuid4())
            active = op(
                "activate",
                epoch=epoch,
                serverInstance=before["serverInstance"],
                handoff=before["handoff"],
                civilization=civ,
                activationId=activation,
            )
            if civ in services:
                service = services[civ]
                status = service.call(
                    "start",
                    activationId=activation,
                    epoch=epoch,
                    civilization=civ,
                    gameTurn=before["turn"],
                    handoff=before["handoff"],
                    threadId=None,
                    mcpUrl=origin + "/mcp",
                    playerToken=active["playerToken"],
                    prompt=player_prompt(civ, before["turn"]),
                    model=model,
                    reasoningEffort=reasoning,
                    timeoutSeconds=600,
                    leaseSeconds=30,
                )
                print("Live model turn:", civ, before["turn"], flush=True)
                last_heartbeat = time.monotonic()
                while status["phase"] not in Agent.TERMINAL:
                    assert time.monotonic() < deadline, "Live exercise deadline reached"
                    await human_trade()
                    if time.monotonic() - last_heartbeat >= 5:
                        op("heartbeat")
                        service.call("heartbeat", activationId=activation)
                        last_heartbeat = time.monotonic()
                    await asyncio.sleep(1)
                    status = service.call("status", activationId=activation)
                record["activations"].append(
                    {"civilization": civ, "turn": before["turn"], "activationId": activation, "phase": status["phase"]}
                )
                save()
                assert status["phase"] == "COMPLETED", status["phase"]
                assert op("status")["status"]["handoff"] == before["handoff"] + 1, "No confirmed model handoff"
                finalized = await asyncio.to_thread(
                    finalize_turn,
                    service,
                    {
                        "activationId": activation,
                        "epoch": epoch,
                        "gameTurn": before["turn"],
                        "handoff": before["handoff"],
                        "confirmedHandoff": before["handoff"] + 1,
                    },
                )
                assert finalized["committedHandoff"] == before["handoff"] + 1
                cursor = 0
                with (root / f"{civ}-{before['turn']}-events.jsonl").open("w") as log:
                    while True:
                        page = service.events(activation, cursor)
                        for event in page["events"]:
                            log.write(json.dumps(event) + "\n")
                        cursor = page["nextCursor"]
                        if not page["hasMore"]:
                            break
            else:
                await human_turn(active["playerToken"])
                print("Scripted human handoff:", civ, flush=True)
            op("heartbeat")
        record["final"] = op("status")["status"]
        assert record["final"]["turn"] == 2 and record["final"]["activeCivilization"] == "Rome"
        record["completed"] = True
        save()
    except BaseException as error:
        record["error"] = f"{type(error).__name__}: {error}"
        record["final"] = op("status")["status"]
        raise
    finally:
        op("barrier")
        for service in services.values():
            service.call("disconnect")
        save()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--reasoning-effort", default="medium")
    parser.add_argument("--resume", action="store_true")
    options = parser.parse_args()
    os.umask(0o077)
    asyncio.run(exercise(options.state_dir.resolve(), options.model, options.reasoning_effort, options.resume))
