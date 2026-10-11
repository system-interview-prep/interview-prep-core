"""Score Vietnamese STT predictions against a versioned audio manifest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.modules.interviews.adapters.voice.evaluation import (  # noqa: E402
    evaluate_manifest,
    validate_corpus_manifest,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--predictions", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--expected-samples", type=int)
    args = parser.parse_args()
    if args.predictions:
        report = evaluate_manifest(args.manifest, args.predictions)
    else:
        report = {"corpus": validate_corpus_manifest(args.manifest, expected_samples=args.expected_samples)}
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
