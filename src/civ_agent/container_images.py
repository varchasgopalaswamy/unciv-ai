"""Build immutable runtime images from the selected source and engine checkout."""

import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from civ_agent.checkout import repository_root
from civ_agent.container_support import ContainerError, command, podman


def source_revision(root):
    return {
        "revision": command(["git", "-C", str(root), "rev-parse", "HEAD"]).stdout.strip(),
        "dirty": bool(command(["git", "-C", str(root), "status", "--porcelain"]).stdout.strip()),
    }


def build_images(*, repo_dir=None, unciv_dir=None, codex="codex", tag="localhost/civ-agent"):
    root = repository_root(repo_dir)
    engine = Path(unciv_dir).expanduser() if unciv_dir else root / "vendor/Unciv"
    if not engine.is_absolute():
        engine = root / engine
    engine = engine.resolve()
    logger = shutil.which("codex-log")
    if not logger:
        raise ContainerError("codex-log is required for the Gradle build")
    subprocess.run(
        [logger, "./gradlew", f"-PuncivDir={engine}", "installDist", "prepareDesktopAssets", "--console=plain"],
        cwd=root,
        check=True,
    )
    executable = shutil.which(codex)
    uv = shutil.which("uv")
    if not executable or not uv:
        raise ContainerError("Install Codex and uv before building runtime images")
    executable = Path(executable).resolve()
    with executable.open("rb") as stream:
        is_elf = stream.read(4) == b"\x7fELF"
    if not is_elf:
        raise ContainerError("--codex must select the standalone Linux Codex binary")
    code_host = executable.with_name("codex-code-mode-host")
    if not code_host.is_file():
        raise ContainerError("The standalone Codex release must include its codex-code-mode-host companion")

    def checksum(path):
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    provenance = {
        "source": source_revision(root),
        "engine": source_revision(engine),
        "codex": command([str(executable), "--version"]).stdout.strip(),
        "codexSha256": checksum(executable),
        "codeModeHostSha256": checksum(code_host),
        "uv": command([uv, "--version"]).stdout.strip(),
    }
    # A curated context excludes credentials, games, unrelated files and the host venv.
    with tempfile.TemporaryDirectory(prefix="civ-image-") as temporary:
        context = Path(temporary)
        shutil.copy2(root / "containers/Containerfile", context / "Containerfile")
        shutil.copy2(executable, context / "codex")
        shutil.copy2(code_host, context / "codex-code-mode-host")
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
        shutil.copytree(root / "docs", app / "docs", ignore=shutil.ignore_patterns("archive"))
        (context / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
        for target in ("runtime", "desktop"):
            subprocess.run(
                [
                    "podman",
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
            )
    return {
        target: json.loads(podman("image", "inspect", f"{tag}:{target}").stdout)[0]["Id"]
        for target in ("runtime", "desktop")
    }
