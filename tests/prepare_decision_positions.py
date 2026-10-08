"""Select raw saves from the frozen baseline for native saved-position preparation."""

import argparse
import hashlib
import json
from pathlib import Path

from civ_agent.tls import write_private_json
from tests.evaluation_sources import prepare_variants
from tests.preserve_benchmark_baseline import digest, private_bytes


def prepare(baseline, output, manifest):
    if output.exists():
        raise ValueError("Use a fresh directory; prepared sources are immutable")
    for case in manifest["positions"]:
        source = baseline / case["sourceGame"]
        if digest(source / "checkpoint.json") != case["sourceCheckpointSha256"]:
            raise ValueError(f"Baseline checkpoint changed: {case['id']}")
        content = (source / "game.json").read_text().strip().encode()
        if hashlib.sha256(content).hexdigest() != case["saveSha256"]:
            raise ValueError(f"Baseline native save changed: {case['id']}")
        private_bytes(output / (case["id"] + ".json"), content + b"\n")
    write_private_json(output / "manifest.json", manifest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--variants-output", type=Path)
    parser.add_argument("--facts-base", default="89a93c0", help="Preserved revision before the decision refactor")
    parser.add_argument(
        "--manifest", type=Path, default=Path(__file__).parent / "fixtures/decision_positions/manifest.json"
    )
    args = parser.parse_args()
    prepare(args.baseline, args.output, json.loads(args.manifest.read_text()))
    if args.variants_output:
        prepare_variants(Path(__file__).resolve().parents[1], args.variants_output, args.facts_base)


if __name__ == "__main__":
    main()
