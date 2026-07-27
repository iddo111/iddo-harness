"""
Key generator — ``python -m installer.gen_keys``.

Creates the harness's Ed25519 signing keypair:

    ~/.iddo-harness/agent.key    private, PKCS#8 PEM, chmod 0600 — never leaves the machine
    ~/.iddo-harness/agent.pub    public, SubjectPublicKeyInfo PEM

and (by default, when run from a checkout) copies the public half to
``docs/agent.pub`` so it can be committed and any consumer of the bridge repo
can verify results without ever touching the machine.

Run once per machine at install time. Re-running is refused unless ``--force``,
because a new key invalidates every signature already published.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PUBLISHED_PUBKEY = REPO_ROOT / "docs" / "agent.pub"

EXIT_OK = 0
EXIT_ERROR = 1


def _import_signing():
    """Import agent.signing whether we are run from a checkout or an install."""
    try:
        from agent import signing  # noqa: PLC0415
    except ImportError:  # pragma: no cover - flat layout / installed agent dir
        sys.path.insert(0, str(REPO_ROOT / "agent"))
        import signing  # type: ignore[no-redef]  # noqa: PLC0415
    return signing


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m installer.gen_keys",
        description="Generate the Iddo Harness Ed25519 result-signing keypair.",
    )
    parser.add_argument(
        "--dir", default=None,
        help="Key directory (default: ~/.iddo-harness).",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Replace an existing key. Invalidates every signature published so far.",
    )
    parser.add_argument(
        "--no-publish", dest="publish", action="store_false", default=True,
        help="Do not copy the public key into docs/agent.pub.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Generate the keypair, publish the public half, and print both paths."""
    args = _build_parser().parse_args(argv)
    signing = _import_signing()

    try:
        priv, pub = signing.generate_keypair(args.dir, force=args.force)
    except signing.SigningError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT_ERROR

    print(f"private key: {priv}  (keep this on the machine, mode 0600)")
    print(f"public key:  {pub}")

    if args.publish and PUBLISHED_PUBKEY.parent.is_dir():
        shutil.copyfile(pub, PUBLISHED_PUBKEY)
        print(f"published:   {PUBLISHED_PUBKEY}  (commit this so consumers can verify)")

    print()
    print(pub.read_text(encoding="utf-8").strip())
    print()
    print("Verify a result with:  python -m agent.verify results/<id>.json")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
