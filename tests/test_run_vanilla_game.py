import json
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts import run_vanilla_game as launcher


def saved_run(root, **changes):
    (root / "runner").mkdir(parents=True, exist_ok=True)
    (root / "exploration.json").write_text("{}")
    state = {
        "model": "gpt-5.6-luna",
        "reasoningEffort": "medium",
        "startedAt": time.time(),
        "limits": {"maxTurns": 3, "maxInvocations": 36, "maxSeconds": 3600, "activationSeconds": 600},
        "status": "paused",
        "usage": {"input_tokens": 12345},
        **changes,
    }
    (root / "runner/run.json").write_text(json.dumps(state))
    return state


def test_full_game_defaults_and_existing_match_protection(tmp_path):
    args = launcher.arguments(["--state-dir", str(tmp_path)])
    command = launcher.launch_command(args, Path(sys.executable))
    for name, expected in {
        "max-turns": "331",
        "max-invocations": "10000",
        "max-seconds": "604800",
        "turn-timeout": "3600",
        "startup-timeout": "900",
    }.items():
        assert command[command.index("--" + name) + 1] == expected
    saved = saved_run(tmp_path)
    with pytest.raises(ValueError, match="already exists"):
        launcher.launch_command(args, Path(sys.executable))
    assert launcher.read_json(tmp_path / "runner/run.json") == saved


def test_resume_preserves_larger_limits_usage_and_original_clock(tmp_path):
    saved = saved_run(
        tmp_path, limits={"maxTurns": 500, "maxInvocations": 20000, "maxSeconds": 900000, "activationSeconds": 4000}
    )
    args = launcher.arguments(["--state-dir", str(tmp_path), "--resume"])
    command = launcher.launch_command(args, Path(sys.executable))
    assert "--resume" in command and "--seed" not in command
    for option, (key, _) in launcher.LIMITS.items():
        assert command[command.index("--" + option.replace("_", "-")) + 1] == str(saved["limits"][key])
    assert launcher.read_json(tmp_path / "runner/run.json") == saved
    args.max_seconds = 100
    with pytest.raises(ValueError, match="cannot decrease"):
        launcher.launch_command(args, Path(sys.executable))
    args.max_seconds = None
    saved_run(tmp_path, startedAt=time.time() - 604801)
    with pytest.raises(ValueError, match="original wall-time budget has expired"):
        launcher.launch_command(args, Path(sys.executable))


def test_resume_requires_saved_state_and_same_model(tmp_path):
    args = launcher.arguments(["--state-dir", str(tmp_path), "--resume"])
    with pytest.raises(ValueError, match="requires exploration.json"):
        launcher.launch_command(args, Path(sys.executable))
    saved_run(tmp_path, reasoningEffort="high")
    with pytest.raises(ValueError, match="must use gpt-5.6-luna"):
        launcher.launch_command(args, Path(sys.executable))


@pytest.mark.parametrize(
    ("status", "result", "child_code", "interrupted", "expected"),
    [
        ("complete", {"winner": "Egypt", "victoryType": "Time"}, 0, False, 0),
        ("complete", None, 0, False, 2),
        ("paused", None, 0, False, 2),
        ("needs_inspection", None, 0, False, 2),
        ("budget_exhausted", None, 0, False, 3),
        ("paused", None, 0, True, 130),
        ("paused", None, 7, False, 7),
    ],
)
def test_exit_status_distinguishes_blockers_from_native_completion(
    tmp_path, status, result, child_code, interrupted, expected
):
    saved_run(tmp_path, status=status, reason="Saved reason")
    (tmp_path / "telemetry").mkdir()
    launcher.write_json(
        tmp_path / "telemetry/game-result.json", {"status": "completed" if result else "incomplete", "result": result}
    )
    summary = launcher.stopped_state(tmp_path, child_code, interrupted)
    assert summary["exitCode"] == expected
    assert summary["result"] == result and summary["reason"] == "Saved reason"


def test_launcher_captures_output_and_stop_report_without_restarting(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("NO_COLOR", raising=False)
    root = tmp_path / "match with spaces"
    worker = tmp_path / "fake_worker.py"
    worker.write_text(
        "import json, sys\nfrom pathlib import Path\n"
        "root = Path(sys.argv[1]); (root/'runner').mkdir()\n"
        "(root/'runner/run.json').write_text(json.dumps({'status':'paused','reason':'Turn did not hand off'}))\n"
        "print('\\x1b[36mstdout diagnostic\\x1b[0m', flush=True)\n"
        "print('stderr diagnostic', file=sys.stderr, end='', flush=True)\n"
    )
    monkeypatch.setattr(
        launcher, "launch_command", lambda args, python: [str(python), "-u", str(worker), str(args.state_dir)]
    )
    assert launcher.main(["--state-dir", str(root), "--color", "always"]) == 2
    output = capsys.readouterr().out
    assert "\x1b[" in output.split("\x1b[36mstdout diagnostic")[0]
    assert "\x1b[36mstdout diagnostic\x1b[0m" in output
    assert "\x1b[" in output.split("stderr diagnostic")[1]
    launches = list((root / "launches").iterdir())
    assert len(launches) == 1
    console = (launches[0] / "console.log").read_text()
    assert "stdout diagnostic" in console and "stderr diagnostic" in console
    assert "\x1b" not in console
    assert "[Rome] Player: gpt-5.6-luna" in console and "[Egypt] Player: Built-in AI" in console
    assert "stderr diagnostic\n  Error: Stopped: paused; exit 2." in console
    stopped = launcher.read_json(launches[0] / "stop.json")
    assert stopped is not None
    assert stopped["childExitCode"] == 0 and stopped["exitCode"] == 2
    assert stopped["reason"] == "Turn did not hand off"
    assert (launches[0] / "stop.json").stat().st_mode & 0o777 == 0o600


def test_launcher_resolves_color_before_piping_child_output(tmp_path, monkeypatch):
    from io import StringIO

    class Terminal(StringIO):
        def isatty(self):
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(sys, "stdout", Terminal())
    args = launcher.arguments(["--state-dir", str(tmp_path)])
    command = launcher.launch_command(args, Path(sys.executable))
    assert command[command.index("--color") + 1] == "always"
    monkeypatch.setenv("NO_COLOR", "1")
    args.color = "always"
    command = launcher.launch_command(args, Path(sys.executable))
    assert command[command.index("--color") + 1] == "never"


def test_term_signal_allows_child_to_flush_and_exit(tmp_path):
    ready = tmp_path / "ready"
    worker = tmp_path / "fake_worker.py"
    worker.write_text(
        "import os, signal, sys, time\nfrom pathlib import Path\n"
        "def stop(*args):\n    print('flushed checkpoint', flush=True)\n    sys.exit(0)\n"
        "signal.signal(signal.SIGINT, stop)\n"
        "Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "while True: time.sleep(0.1)\n"
    )
    driver = (
        "import json, sys\nfrom pathlib import Path\n"
        "from scripts.run_vanilla_game import run_logged\n"
        "def announce(message, end='\\n'): print(message, end=end, flush=True)\n"
        "result = run_logged([sys.executable, '-u', sys.argv[1], sys.argv[2]], Path.cwd(), announce)\n"
        "print(json.dumps(result), flush=True)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", driver, str(worker), str(ready)],
        cwd=launcher.REPOSITORY,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "The fake service did not start"
        process.send_signal(signal.SIGTERM)
        output, errors = process.communicate(timeout=10)
        assert process.returncode == 0, errors
        assert "flushed checkpoint" in output
        assert json.loads(output.splitlines()[-1]) == [0, True]
        assert not Path("/proc", ready.read_text()).exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
