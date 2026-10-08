"""Fingerprint the Python implementation actually selected for an evaluation."""

import hashlib
import io
import shutil
import subprocess
import tarfile

from civ_agent.tls import write_private_json


def tree_hash(root):
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def prepare_variants(repository, output, base_revision):
    """Isolate facts from the new decision loop without production feature flags."""
    revision = subprocess.check_output(
        ["git", "rev-parse", "--verify", base_revision + "^{commit}"], cwd=repository, text=True
    ).strip()
    output.mkdir(parents=True, exist_ok=False)
    archive = subprocess.check_output(["git", "archive", revision, "src"], cwd=repository)
    with tarfile.open(fileobj=io.BytesIO(archive)) as saved:
        saved.extractall(output / "facts", filter="data")
    files = ("construction_estimates.py", "observations.py", "review_context.py", "proposal_gateway.py")
    for name in files:
        shutil.copy2(repository / "src/civ_agent" / name, output / "facts/src/civ_agent" / name)
    shutil.copytree(repository / "src", output / "full/src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    write_private_json(
        output / "manifest.json",
        {
            "factsBase": revision,
            "factsOverlay": list(files),
            "sourceHashes": {name: tree_hash(output / name / "src/civ_agent") for name in ("facts", "full")},
            "limitations": "Fresh stores for both variants. Facts preserves the selected revision's prompts, review and memory format; full includes current prompt and decision changes. This is not the old benchmark runtime.",
        },
    )
