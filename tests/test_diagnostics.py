import errno
import json
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import BaseModel, ValidationError

from civ_agent.diagnostics import Diagnostics, exception_details


def test_private_rotation_concurrent_records_and_restart_append(tmp_path):
    log = Diagnostics(tmp_path, "Rome", max_bytes=2500)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda i: log.emit("test", sequence=i), range(60)))
    log.close()
    log.close()
    files = list(tmp_path.glob("diagnostics.jsonl*"))
    assert len(files) == 4
    events = []
    for file in files:
        assert stat.S_IMODE(file.stat().st_mode) == 0o600
        events.extend(json.loads(line) for line in file.read_text().splitlines())
    assert all(e["civilization"] == "Rome" and e["time"] and e["serviceInstance"] for e in events)
    assert len({e["sequence"] for e in events if e["event"] == "test"}) == sum(e["event"] == "test" for e in events)
    prior = log.path.read_text()
    restarted = Diagnostics(tmp_path, "Rome")
    restarted.close()
    assert log.path.read_text().startswith(prior)
    assert restarted.context["serviceInstance"] != log.context["serviceInstance"]


def test_exception_causes_and_groups_keep_details_without_messages_inputs_or_locals():
    secret = "PRIVATE-INPUT-AND-MESSAGE"

    class Shape(BaseModel):
        count: int

    with pytest.raises(ValidationError) as original:
        Shape.model_validate({"count": secret})
    with pytest.raises(RuntimeError) as wrapped:
        raise RuntimeError(secret) from original.value
    group = ExceptionGroup(secret, [wrapped.value, OSError(errno.ECONNRESET, secret)])
    result = exception_details(group)
    assert secret not in json.dumps(result)
    children = result["exceptions"]
    assert children[0]["cause"]["validation"] == [{"type": "int_parsing", "depth": 1}]
    assert children[1]["errno"] == errno.ECONNRESET
    assert children[0]["stack"][-1]["function"].startswith("test_exception_causes")
    group.__cause__ = group
    assert exception_details(group)["cause"] == {"truncated": True}


def test_operator_diagnostics_keep_safe_rejection_metadata_without_response_messages():
    from civ_agent.operator_connection import OperatorError

    error = OperatorError("private response and credentials", "PLAYER_UNAVAILABLE", 409)
    result = exception_details(error)
    assert result["operatorCode"] == "PLAYER_UNAVAILABLE"
    assert result["httpStatus"] == 409
    assert "private" not in json.dumps(result)
    invalid = exception_details(OperatorError("private", "unsafe private response", "401 private"))
    assert "operatorCode" not in invalid and "httpStatus" not in invalid


@pytest.mark.parametrize("unsafe", ["symlink", "public-file"])
def test_diagnostics_refuse_unsafe_files(tmp_path, unsafe):
    target = tmp_path / "diagnostics.jsonl"
    if unsafe == "symlink":
        private = tmp_path / "credentials"
        private.write_text("DO-NOT-TOUCH")
        target.symlink_to(private)
    else:
        target.write_text("DO-NOT-TOUCH")
        target.chmod(0o644)
    with pytest.raises(OSError):
        Diagnostics(tmp_path, "Rome")
    assert target.read_text() == "DO-NOT-TOUCH"
