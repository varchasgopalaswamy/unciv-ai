"""Start the local game host, agent services and scheduler together."""

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import secrets
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy.exc import SQLAlchemyError

from civ_agent import scheduler
from civ_agent.agent_settings import add_settings_arguments, resolve_run_settings, setting_argument
from civ_agent.checkout import repository_root
from civ_agent.commands.host import port_number, seed_number
from civ_agent.monitoring import path_for, read_record
from civ_agent.terminal_output import TerminalOutput, color_enabled, visible_text
from civ_agent.tls import write_private_json
from civ_agent.turn_archive import civilization_directory_name

DEFAULT_STATE = Path.home() / ".local/state/unciv-host/vanilla-full"
LIMITS = {
    "max_turns": ("maxTurns", 331),
    "max_invocations": ("maxInvocations", 10000),
    "max_seconds": ("maxSeconds", 7 * 24 * 60 * 60),
    "turn_timeout": ("activationSeconds", 3600),
}
SAVED_OPTIONS = (
    "port",
    "startup_timeout",
    "fast",
    "strategy_guide",
    "review_interval",
    "review_attempts",
    "model",
    "reasoning_effort",
    "agent_models",
    "agent_reasoning_efforts",
    "repo_dir",
    "unciv_dir",
)


def read_json(path):
    return json.loads(path.read_text()) if path.exists() else None


class AgentSetting(argparse.Action):
    """Collect repeatable CIV=VALUE options without silently overwriting a civ."""

    def __call__(self, parser, namespace, values, option_string=None):
        civilization, separator, value = values.partition("=")
        civilization = civilization.strip()
        if not separator or not civilization:
            raise argparse.ArgumentError(self, "expected CIV=VALUE, for example Rome=gpt-6-luna")
        try:
            value = setting_argument(value)
        except argparse.ArgumentTypeError as error:
            raise argparse.ArgumentError(self, str(error)) from error
        choices = dict(getattr(namespace, self.dest) or {})
        if civilization in choices:
            raise argparse.ArgumentError(self, f"{civilization} was specified more than once")
        choices[civilization] = value
        setattr(namespace, self.dest, choices)


def parser(*, add_help=True, action="start"):
    result = argparse.ArgumentParser(description=__doc__, add_help=add_help)
    result.set_defaults(action=action)
    result.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    result.add_argument(
        "--verbose", action="store_true", help="Print preload context and tool activity; full history is always saved"
    )
    result.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    add_settings_arguments(
        result,
        model_help="Default model for agents without a per-civ override (new-game default: gpt-6-luna)",
        reasoning_help="Default reasoning effort without a per-civ override (new-game default: medium)",
    )
    result.add_argument(
        "--agent-model",
        dest="agent_models",
        action=AgentSetting,
        metavar="CIV=MODEL",
        help="Override one agent's model; repeat for other civilizations; retained on resume",
    )
    result.add_argument(
        "--agent-reasoning-effort",
        dest="agent_reasoning_efforts",
        action=AgentSetting,
        metavar="CIV=LEVEL",
        help="Override one agent's reasoning effort; repeat for other civilizations; retained on resume",
    )
    result.add_argument("--port", type=port_number, help="Host port; consecutive ports serve agents (default: 8971)")
    result.add_argument("--startup-timeout", type=scheduler.positive, help="Seconds per service startup (default: 900)")
    result.add_argument(
        "--fast", action=argparse.BooleanOptionalAction, default=None, help="Request fast priority; retained on resume"
    )
    result.add_argument(
        "--strategy-guide",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Provide optional strategy suggestions; retained on resume",
    )
    result.add_argument(
        "--review-interval",
        type=scheduler.positive,
        help="Review before the first move, then every k turns (default: 5; retained on resume)",
    )
    result.add_argument(
        "--review-attempts",
        type=scheduler.positive,
        help="Maximum attempts per background review (default: 3; retained on resume)",
    )
    result.add_argument("--repo-dir", type=Path, help="Host source checkout")
    result.add_argument("--unciv-dir", help="Engine checkout for both code and assets")
    if action == "start":
        result.add_argument("--seed", type=seed_number, help="Map seed (default: random)")
        result.add_argument(
            "--agents",
            nargs="+",
            default=["Rome", "Greece", "France"],
            metavar="CIV",
            help="Agent civilizations (default: Rome Greece France)",
        )
        result.add_argument(
            "--built-in-ai",
            nargs="*",
            default=["Egypt"],
            metavar="CIV",
            help="Built-in AI civilizations (default: Egypt; supply no names for none)",
        )
        result.add_argument("--city-states", type=int, choices=range(42), default=2, metavar="0-41")
        sizes = result.add_mutually_exclusive_group()
        sizes.add_argument(
            "--map-size",
            type=str.title,
            choices=("Tiny", "Small", "Medium", "Large", "Huge"),
            help="Map preset (default: Small)",
        )
        sizes.add_argument(
            "--map-radius", type=int, choices=range(2, 501), metavar="2-500", help="Custom hexagonal radius"
        )
    for option, (_, default) in LIMITS.items():
        result.add_argument(
            "--" + option.replace("_", "-"),
            type=scheduler.positive,
            help=f"{option.replace('_', ' ')} (new-game default: {default}; retained on resume)",
        )
    return result


def resolve_options(args):
    """Resolve once before starting processes; the scheduler owns persisted budgets."""
    root = args.state_dir = args.state_dir.expanduser().resolve()
    checkpoint = root / "exploration.json"
    runner = scheduler.RunStore(root / "runner")
    previous = runner.load() if runner.exists() else None
    saved = read_json(root / "launch-settings.json") or {}
    if args.action == "start" and (checkpoint.exists() or previous):
        raise ValueError("A saved match already exists. Use game resume or choose a new --state-dir.")
    if args.action == "resume":
        if not checkpoint.is_file():
            raise ValueError("Resume requires exploration.json.")
        grants = read_json(root / "players.json")
        if not grants:
            raise ValueError("Resume requires the original players.json credentials.")
        args.agents = [
            entry["civilizationId"] for entry in grants["players"] if entry.get("controller", "AGENT") == "AGENT"
        ]
    if not args.agents or len(set(args.agents)) != len(args.agents):
        raise ValueError("Choose at least one agent; civilizations must be distinct.")
    if args.action == "start":
        nations = args.agents + args.built_in_ai
        if len(set(nations)) != len(nations):
            raise ValueError("Agent and built-in AI civilizations must be distinct.")
        args.seed = secrets.randbits(63) if args.seed is None else args.seed
    endpoints = read_json(root / "endpoints.json") or {}
    defaults = {
        "port": urlsplit(endpoints.get("hostUrl", "")).port or 8971,
        "startup_timeout": 900,
        "fast": False,
        "strategy_guide": False,
        "review_interval": 5,
        "review_attempts": 3,
        "model": None,
        "reasoning_effort": None,
        "agent_models": {},
        "agent_reasoning_efforts": {},
        "repo_dir": None,
        "unciv_dir": None,
    }
    for key in SAVED_OPTIONS:
        if getattr(args, key) is None:
            setattr(args, key, saved.get(key, defaults[key]))
    pinned = None
    if args.action == "resume":
        pinned = dict(previous or {})
        for key, field in (("model", "model"), ("reasoning_effort", "reasoningEffort")):
            if key in saved:
                pinned[field] = saved[key]
    args.model, args.reasoning_effort = resolve_run_settings(
        args.model, args.reasoning_effort, defaults=("gpt-6-luna", "medium"), saved=pinned or None
    )
    for key, flag in (("agent_models", "--agent-model"), ("agent_reasoning_efforts", "--agent-reasoning-effort")):
        unknown = set(getattr(args, key)) - set(args.agents)
        if unknown:
            raise ValueError(f"{flag} requires an agent civilization; unknown: {', '.join(sorted(unknown))}.")
        if args.action == "resume":
            # Repeating one saved override must retain overrides for other civs.
            setattr(args, key, {**saved.get(key, {}), **getattr(args, key)})
    if pinned:
        for civilization in args.agents:
            prior = {
                "model": saved.get("agent_models", {}).get(civilization, pinned["model"]),
                "reasoningEffort": saved.get("agent_reasoning_efforts", {}).get(
                    civilization, pinned["reasoningEffort"]
                ),
            }
            if civilization_settings(args, civilization) != prior:
                raise ValueError(
                    f"Resume retains {civilization}'s model {prior['model']} with {prior['reasoningEffort']} reasoning. "
                    "Choose a new --state-dir to use different settings."
                )
    if args.port + len(args.agents) > 65535:
        raise ValueError("--port must leave one consecutive port per agent.")
    for option, (saved_key, default) in LIMITS.items():
        value = getattr(args, option)
        prior = previous["limits"][saved_key] if previous else None
        if value is None:
            value = prior if prior is not None else default
        if prior is not None and value < prior:
            raise ValueError(f"--{option.replace('_', '-')} cannot decrease the saved limit of {prior}.")
        setattr(args, option, value)
    if args.turn_timeout > 86400:
        raise ValueError("--turn-timeout cannot exceed 86400 seconds.")
    if previous and previous["startedAt"] + args.max_seconds <= time.time():
        raise ValueError("The original wall-time budget has expired. Increase --max-seconds; pauses count toward it.")
    args.repo_dir = repository_root(Path(args.repo_dir) if args.repo_dir else None)
    # A failed initial service startup may have saved the board before scheduling began.
    args.scheduler_action = "resume" if previous else "start"


def civilization_settings(args, civilization):
    return {
        "model": args.agent_models.get(civilization, args.model),
        "reasoningEffort": args.agent_reasoning_efforts.get(civilization, args.reasoning_effort),
    }


@contextlib.contextmanager
def services(root, startup_timeout, announce):
    """Own only the processes started here; agents close before the host."""

    def stop(process):
        if process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                process.wait(timeout=60)
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)

    with contextlib.ExitStack() as logs:

        def start(arguments, label, ready):
            path = root / f"{civilization_directory_name(label)}.log"
            log = logs.enter_context(path.open("a"))
            offset = log.tell()
            process = subprocess.Popen(
                [sys.executable, "-u", "-m", "civ_agent", *arguments, "--state-dir", str(root)],
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
            logs.callback(stop, process)
            deadline = time.monotonic() + startup_timeout
            with path.open() as reader:
                reader.seek(offset)
                while True:
                    if process.poll() is not None:
                        raise RuntimeError(f"{label} exited with {process.returncode}; inspect {path}")
                    if any(line.startswith(ready) for line in reader.readlines()):
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError(f"{label} startup timed out after {startup_timeout}s; inspect {path}")
                    time.sleep(0.25)
            announce("Ready", label)

        yield start


def run_services(args, output):
    host = ["host", "start", "--port", str(args.port), "--repo-dir", str(args.repo_dir)]
    if args.unciv_dir:
        host += ["--unciv-dir", args.unciv_dir]
    if args.action == "start":
        host += ["--new", "--seed", str(args.seed), "--city-states", str(args.city_states)]
        host += [item for civ in args.agents for item in ("--agent-player", civ)]
        host += [item for civ in args.built_in_ai for item in ("--built-in-ai", civ)]
        host += ["--map-radius", str(args.map_radius)] if args.map_radius else ["--map-size", args.map_size or "Small"]
    with services(args.state_dir, args.startup_timeout, output.line) as start:
        start(host, "host", "MCP-READY:")
        for offset, civilization in enumerate(args.agents, 1):
            selected = civilization_settings(args, civilization)
            start(
                [
                    "agent-server",
                    "start",
                    civilization,
                    "--port",
                    str(args.port + offset),
                    "--model",
                    selected["model"],
                    "--reasoning-effort",
                    selected["reasoningEffort"],
                    "--review-interval",
                    str(args.review_interval),
                    "--review-attempts",
                    str(args.review_attempts),
                    *(["--strategy-guide"] if args.strategy_guide else []),
                    *(["--fast"] if args.fast else []),
                ],
                civilization,
                "AGENT-READY:",
            )
        options = scheduler.parser().parse_args([])
        vars(options).update(vars(args), action=args.scheduler_action)
        # Scheduler overrides take precedence over service settings. Leave each
        # setting to the services when civilizations have individual overrides.
        if args.agent_models:
            options.model = None
        if args.agent_reasoning_efforts:
            options.reasoning_effort = None
        return scheduler.run(options)


class ConsoleLog:
    """Tee the scheduler's output in this process, preserving terminal colors."""

    def __init__(self, terminal, log):
        self.terminal, self.log = terminal, log
        self.lock = threading.RLock()

    def write(self, value):
        with self.lock:
            self.log.write(visible_text(value))
            self.log.flush()
            try:
                self.terminal.write(value)
                self.terminal.flush()
            except BrokenPipeError:
                pass
        return len(value)

    def flush(self):
        self.log.flush()

    def isatty(self):
        return self.terminal.isatty()


def stopped_state(root, child_code, interrupted):
    state = read_record(path_for(root), "state", "run") or {}
    result = read_record(path_for(root), "state", "result") or {}
    complete = state.get("status") == "complete" and result.get("status") == "completed" and result.get("result")
    code = (
        130
        if interrupted
        else child_code
        if child_code
        else 0
        if complete
        else 3
        if state.get("status") == "budget_exhausted"
        else 2
    )
    return {
        "exitCode": code,
        "interrupted": interrupted,
        "runnerStatus": state.get("status"),
        **{key: state.get(key) for key in ("reason", "inFlight", "invocations", "handoffs")},
        "gameStatus": result.get("status"),
        **{key: result.get(key) for key in ("result", "scores", "usage")},
    }


def provenance(repository):
    try:

        def git(*args):
            return subprocess.check_output(
                ["git", *args], cwd=repository, text=True, stderr=subprocess.DEVNULL, timeout=5
            ).strip()

        return {"revision": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain"))}
    except (OSError, subprocess.SubprocessError):
        return None


def launch(args):
    root = args.state_dir
    launch_dir = root / "launches" / (dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ") + f"-{os.getpid()}")
    launch_dir.mkdir(parents=True, mode=0o700)
    write_private_json(
        root / "launch-settings.json",
        {
            key: str(getattr(args, key)) if isinstance(getattr(args, key), Path) else getattr(args, key)
            for key in SAVED_OPTIONS
        },
    )
    started = time.time()
    engine = Path(args.unciv_dir).expanduser() if args.unciv_dir else Path("vendor/Unciv")
    if not engine.is_absolute():
        engine = args.repo_dir / engine
    write_private_json(
        launch_dir / "launch.json",
        {
            "startedAt": started,
            "options": {
                key: str(value) if isinstance(value, Path) else value
                for key, value in vars(args).items()
                if key != "handler"
            },
            "source": provenance(args.repo_dir),
            "engine": provenance(engine),
            "agentSettings": {civilization: civilization_settings(args, civilization) for civilization in args.agents},
            "strategyGuide": args.strategy_guide,
            "requestedServiceTier": "fast" if args.fast else None,
        },
    )
    # Resolve color against the real terminal before redirecting output to the log tee.
    args.color = "always" if color_enabled(args.color) else "never"
    interrupted = False

    def stop(_signum, _frame):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt

    handlers = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        with (launch_dir / "console.log").open("w", buffering=1) as log:
            tee = ConsoleLog(sys.stdout, log)
            with contextlib.redirect_stdout(tee), contextlib.redirect_stderr(tee):
                output = TerminalOutput(color=args.color)
                output.line("Status", "Resuming game." if args.action == "resume" else "Starting Vanilla game.")
                output.line("Match", root)
                output.line("Log", launch_dir / "console.log")
                output.line("Dashboard", shlex.join(["civ-agent", "match", "watch", "--state-dir", str(root)]))
                for civilization in args.agents:
                    selected = civilization_settings(args, civilization)
                    output.line(
                        "Player",
                        f"{selected['model']} · {selected['reasoningEffort']} reasoning",
                        civilization=civilization,
                    )
                if args.action == "start":
                    for civilization in args.built_in_ai:
                        output.line("Player", "Built-in AI", civilization=civilization)
                    output.line(
                        "Setup",
                        f"Pangaea · radius {args.map_radius}"
                        if args.map_radius
                        else f"Pangaea · {args.map_size or 'Small'}",
                    )
                    output.line("Setup", f"{args.city_states} city-states · seed {args.seed}")
                output.line("Strategy", "Optional guide enabled." if args.strategy_guide else "Guide disabled.")
                output.line(
                    "Reviews",
                    f"Concurrent opening plans on turn 0, then every {args.review_interval} turns; background advice.",
                )
                output.line("Priority", "Fast requested." if args.fast else "Codex default.")
                output.line("Status", "Ctrl-C pauses and flushes the run. A blocked turn stops for inspection.")
                launch_error = None
                try:
                    code = run_services(args, output)
                    interrupted = interrupted or code == 130
                except KeyboardInterrupt:
                    code, interrupted = 130, True
                except Exception as error:
                    launch_error = f"{type(error).__name__}: {error}"
                    output.line("Error", launch_error)
                    code = 2
                summary = {
                    **stopped_state(root, code, interrupted),
                    "startedAt": started,
                    "endedAt": time.time(),
                    "stateDirectory": str(root),
                }
                if launch_error:
                    summary["reason"] = launch_error
                write_private_json(launch_dir / "stop.json", summary)
                level = (
                    "Done" if summary["exitCode"] == 0 else "Warning" if summary["exitCode"] in {3, 130} else "Error"
                )
                output.line(
                    level, f"Stopped: {summary['runnerStatus'] or 'startup failure'}; exit {summary['exitCode']}."
                )
                if summary.get("reason"):
                    output.line(level, summary["reason"])
                if summary.get("result"):
                    output.line("Result", json.dumps(summary["result"]))
                output.line("Report", launch_dir / "stop.json")
                return summary["exitCode"]
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


def run(args):
    try:
        os.umask(0o077)
        args.state_dir = args.state_dir.expanduser().resolve()
        args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (args.state_dir / "launch.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("A launcher already owns this match directory.") from None
            resolve_options(args)
            return launch(args)
    except (OSError, ValueError, KeyError, TypeError, scheduler.RunnerError, SQLAlchemyError) as error:
        TerminalOutput(color=args.color, file=sys.stderr).line("Error", f"Cannot start: {error}")
        return 2
