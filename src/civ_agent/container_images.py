"""Build immutable runtime images from the selected source and engine checkout."""

import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from civ_agent.checkout import repository_root
from civ_agent.container_support import ContainerError, command, host_environment, podman
from civ_agent.orchestration.contracts import digest


def source_revision(root):
    return {
        "revision": command(["git", "-C", str(root), "rev-parse", "HEAD"]).stdout.strip(),
        "dirty": bool(command(["git", "-C", str(root), "status", "--porcelain"]).stdout.strip()),
    }


def build_images(*, repo_dir=None, unciv_dir=None, tag="localhost/civ-agent"):
    root = repository_root(repo_dir)
    engine = Path(unciv_dir).expanduser() if unciv_dir else root / "vendor/Unciv"
    if not engine.is_absolute():
        engine = root / engine
    engine = engine.resolve()
    uv = shutil.which("uv")
    if not uv:
        raise ContainerError("Install uv before building runtime images")
    subprocess.run(
        ["./gradlew", f"-PuncivDir={engine}", "installDist", "prepareDesktopAssets", "--console=plain"],
        cwd=root,
        check=True,
    )

    def checksum(path):
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    provenance = {
        "source": source_revision(root),
        "engine": source_revision(engine),
        "runtime": "pydantic-local-v1",
        "uv": command([uv, "--version"]).stdout.strip(),
    }
    # A curated context excludes credentials, games, unrelated files and the host venv.
    with tempfile.TemporaryDirectory(prefix="civ-image-") as temporary:
        context = Path(temporary)
        shutil.copy2(root / "containers/Containerfile", context / "Containerfile")
        shutil.copy2(uv, context / "uv")
        shutil.copytree(root / "build/install/unciv-ai/lib", context / "lib")
        shutil.copytree(root / "build/desktop-engine", context / "engine")
        app = context / "app"
        app.mkdir()
        for name in ("pyproject.toml", "uv.lock", "README.md"):
            shutil.copy2(root / name, app / name)
        shutil.copytree(
            root / "src/civ_agent", app / "src/civ_agent", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
        )
        # Strategy context is a package-relative document in the existing service.
        shutil.copytree(
            root / "docs", app / "docs", ignore=shutil.ignore_patterns("archive", "handoffs", "plans", "results")
        )
        hashes = {
            str(path.relative_to(context)): checksum(path) for path in sorted(context.rglob("*")) if path.is_file()
        }
        provenance.update(
            uvSha256=hashes["uv"],
            lockSha256=hashes["app/uv.lock"],
            sourceHash=digest({name: value for name, value in hashes.items() if name.startswith("app/")}),
            engineHash=digest({name: value for name, value in hashes.items() if name.startswith(("lib/", "engine/"))}),
            contentHash=digest(hashes),
        )
        (context / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
        for target in ("runtime", "desktop"):
            subprocess.run(
                [
                    "podman",
                    # Build RUN steps need no systemd scope. Avoid sending
                    # cgroup requests to a system bus from non-login sessions.
                    "--cgroup-manager=cgroupfs",
                    "build",
                    "--target",
                    target,
                    "--tag",
                    f"{tag}:{target}",
                    "--file",
                    str(context / "Containerfile"),
                    str(context),
                ],
                check=True,
                env=host_environment(),
            )
    return {
        target: json.loads(podman("image", "inspect", f"{tag}:{target}").stdout)[0]["Id"]
        for target in ("runtime", "desktop")
    }
