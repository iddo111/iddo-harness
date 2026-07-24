"""
Shared pytest fixtures / sys.path setup.

The agent modules use flat, script-style imports (e.g. `from config import
load_config`) rather than `agent.config`, so tests need the `agent/`
directory on sys.path directly, in addition to the repo root (for the
`agent` package import used by pyproject's console-script entry point).
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = REPO_ROOT / "agent"

for p in (str(REPO_ROOT), str(AGENT_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)
