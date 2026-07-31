"""
Signature verifier — ``python -m agent.verify <envelope.json> [...]``.

Prints one ``OK`` / ``FAIL`` line per file and exits non-zero if any file
failed, so it drops straight into a shell pipeline or a CI step:

    python -m agent.verify results/*.json || echo "forged result in the bridge!"

By default the key comes from ``~/.iddo-harness/agent.pub``. A consumer that
only has the repo checkout should point ``--pubkey`` at ``docs/agent.pub``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from signing import SigningError, load_public_key, verify_document
except ImportError:  # pragma: no cover - packaged imports
    from agent.signing import SigningError, load_public_key, verify_document

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_ERROR = 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agent.verify",
        description="Verify the Ed25519 signature on one or more harness result documents.",
    )
    parser.add_argument("files", nargs="+", help="JSON document(s) to verify.")
    parser.add_argument(
        "--pubkey", default=None,
        help="Public key PEM (default: ~/.iddo-harness/agent.pub; use docs/agent.pub from a checkout).",
    )
    parser.add_argument("--quiet", "-q", action="store_true", help="Only print failures.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Verify each file; return 0 when all pass, 1 when any fails, 2 on setup error."""
    args = _build_parser().parse_args(argv)

    try:
        public_key = load_public_key(args.pubkey)
    except SigningError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT_ERROR

    # A file we could not even read is an operator error (exit 2), not a verdict
    # on its contents (exit 1) — a CI step watching for forged results should not
    # be tripped by a typo in a path.
    failures = 0
    errors = 0
    for name in args.files:
        path = Path(name)
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"FAIL {path}: unreadable ({e})")
            errors += 1
            continue
        if not isinstance(doc, dict):
            print(f"FAIL {path}: not a JSON object")
            errors += 1
            continue

        if verify_document(doc, public_key=public_key):
            if not args.quiet:
                print(f"OK   {path}")
        else:
            print(f"FAIL {path}: signature does not match document")
            failures += 1

    if errors:
        return EXIT_ERROR
    return EXIT_FAIL if failures else EXIT_OK


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
