"""Find the JVM checkout without depending on a package's installation location."""

import os
from pathlib import Path


def repository_root(explicit: Path | None = None) -> Path:
    """Prefer an explicit checkout, then the working tree or editable source tree."""
    selected = explicit or os.environ.get("CIV_AGENT_REPO")

    def is_checkout(path: Path) -> bool:
        return (path / "gradlew").is_file() and (path / "src/main/kotlin/uncivai").is_dir()

    if selected is not None:
        root = Path(selected).expanduser().resolve()
        if is_checkout(root):
            return root
        raise ValueError(f"No game host checkout at {root}. Select its root with --repo-dir.")
    for start in (Path.cwd(), Path(__file__).resolve().parent):
        for root in (start, *start.parents):
            if is_checkout(root):
                return root
    raise ValueError("Game host checkout not found. Pass --repo-dir or set CIV_AGENT_REPO.")
