"""Combined native runtime images need only uv and the selected engine inputs."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from civ_agent import container_images as images
from civ_agent.commands import runtime
from civ_agent.container_support import ContainerError
from civ_agent.orchestration.contracts import digest


@pytest.mark.parametrize("selection", ["default", "relative", "absolute"])
def test_combined_build_has_native_provenance_and_no_codex_executable_requirement(tmp_path, monkeypatch, selection):
    root = tmp_path / "repo"
    (root / "containers").mkdir(parents=True)
    containerfile = Path(__file__).resolve().parents[1] / "containers/Containerfile"
    (root / "containers/Containerfile").write_bytes(containerfile.read_bytes())
    (root / "src/civ_agent/__pycache__").mkdir(parents=True)
    (root / "src/civ_agent/__init__.py").write_text("# native source\n")
    (root / "src/civ_agent/__pycache__/unpackaged.pyc").write_bytes(b"private cache")
    local_docs = ("archive", "handoffs", "plans", "results")
    for directory in local_docs:
        (root / "docs" / directory).mkdir(parents=True)
        (root / "docs" / directory / "private.md").write_text("Not packaged")
    (root / "docs/guide.md").write_text("Packaged guide")
    (root / "operator.json").write_text("PRIVATE_OPERATOR_TOKEN")
    for name in ("pyproject.toml", "uv.lock", "README.md"):
        (root / name).write_text(name)
    default_engine = root / "vendor/Unciv"
    engine = (
        default_engine
        if selection == "default"
        else root / "selected-engine"
        if selection == "relative"
        else tmp_path / "sibling-engine"
    )
    (engine / "android/assets").mkdir(parents=True)
    (engine / "android/assets/selected.txt").write_text(selection)
    uv = tmp_path / "uv"
    uv.write_bytes(b"synthetic uv tool")
    looked_up, built, inspected = [], [], []
    monkeypatch.setattr(images, "repository_root", lambda *_: root)

    def which(name):
        looked_up.append(name)
        return str(uv) if name == "uv" else None

    monkeypatch.setattr(images.shutil, "which", which)
    monkeypatch.setattr(
        images,
        "source_revision",
        lambda path: {"revision": "source" if path == root else "selected-engine", "dirty": False},
    )

    def command(arguments):
        assert arguments == [str(uv), "--version"]
        return SimpleNamespace(stdout="uv synthetic\n")

    monkeypatch.setattr(images, "command", command)

    def podman(*arguments):
        inspected.append(arguments)
        target = arguments[-1].rsplit(":", 1)[1]
        return SimpleNamespace(stdout=json.dumps([{"Id": "sha256:" + target}]))

    monkeypatch.setattr(images, "podman", podman)

    def build(arguments, **options):
        if arguments[0] == "./gradlew":
            assert options == {"cwd": root, "check": True}
            assert arguments == [
                "./gradlew",
                f"-PuncivDir={engine.resolve()}",
                "installDist",
                "prepareDesktopAssets",
                "--console=plain",
            ]
            lib = root / "build/install/unciv-ai/lib"
            lib.mkdir(parents=True)
            (lib / "selected.jar").write_bytes(b"selected engine JVM library")
            staged = root / "build/desktop-engine/android/assets"
            staged.mkdir(parents=True)
            (staged / "selected.txt").write_bytes((engine / "android/assets/selected.txt").read_bytes())
            return
        assert arguments[:3] == ["podman", "--cgroup-manager=cgroupfs", "build"]
        context = Path(arguments[-1])
        target = arguments[arguments.index("--target") + 1]
        built.append(target)
        assert sorted(path.name for path in context.iterdir()) == [
            "Containerfile",
            "app",
            "engine",
            "lib",
            "provenance.json",
            "uv",
        ]
        assert (context / "engine/android/assets/selected.txt").read_text() == selection
        assert (context / "lib/selected.jar").read_bytes() == b"selected engine JVM library"
        assert (context / "app/docs/guide.md").read_text() == "Packaged guide"
        assert all(not (context / "app/docs" / directory).exists() for directory in local_docs)
        assert not (context / "app/operator.json").exists()
        assert not (context / "app/src/civ_agent/__pycache__").exists()
        provenance = json.loads((context / "provenance.json").read_text())
        hashes = {
            str(path.relative_to(context)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(context.rglob("*"))
            if path.is_file() and path != context / "provenance.json"
        }
        assert provenance == {
            "source": {"revision": "source", "dirty": False},
            "engine": {"revision": "selected-engine", "dirty": False},
            "runtime": "pydantic-local-v1",
            "uv": "uv synthetic",
            "uvSha256": hashes["uv"],
            "lockSha256": hashes["app/uv.lock"],
            "sourceHash": digest({name: value for name, value in hashes.items() if name.startswith("app/")}),
            "engineHash": digest(
                {name: value for name, value in hashes.items() if name.startswith(("lib/", "engine/"))}
            ),
            "contentHash": digest(hashes),
        }
        assert "codex" not in (context / "Containerfile").read_text().lower()
        assert "musl" not in (context / "Containerfile").read_text()
        assert "uv sync --locked --no-dev" in (context / "Containerfile").read_text()
        assert "openjdk-21-jre-headless" in (context / "Containerfile").read_text()

    monkeypatch.setattr(images.subprocess, "run", build)
    override = None if selection == "default" else "selected-engine" if selection == "relative" else engine
    assert images.build_images(repo_dir=root, unciv_dir=override, tag="private/native") == {
        "runtime": "sha256:runtime",
        "desktop": "sha256:desktop",
    }
    assert looked_up == ["uv"] and built == ["runtime", "desktop"]
    assert inspected == [("image", "inspect", "private/native:runtime"), ("image", "inspect", "private/native:desktop")]


def test_missing_uv_fails_before_any_engine_or_container_build(tmp_path, monkeypatch):
    monkeypatch.setattr(images, "repository_root", lambda *_: tmp_path)
    monkeypatch.setattr(images.shutil, "which", lambda *_: None)
    build = Mock(side_effect=AssertionError("Missing uv must fail before starting a build"))
    monkeypatch.setattr(images.subprocess, "run", build)
    with pytest.raises(ContainerError, match="Install uv"):
        images.build_images(repo_dir=tmp_path)
    build.assert_not_called()


def test_image_cli_preserves_source_engine_and_tag_without_a_codex_option(monkeypatch, capsys):
    parser = runtime.parser(group="image", action="build")
    assert "--codex" not in parser.format_help()
    with pytest.raises(SystemExit):
        parser.parse_args(["--codex", "unused"])
    capsys.readouterr()
    build = Mock(return_value={"runtime": "sha256:runtime", "desktop": "sha256:desktop"})
    monkeypatch.setattr(runtime, "build_images", build)
    args = parser.parse_args(["--repo-dir", "/source", "--unciv-dir", "/selected-engine", "--tag", "private/native"])
    assert runtime.run(args) == 0
    build.assert_called_once_with(repo_dir=Path("/source"), unciv_dir="/selected-engine", tag="private/native")
    assert json.loads(capsys.readouterr().out) == {"runtime": "sha256:runtime", "desktop": "sha256:desktop"}
