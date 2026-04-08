"""CLI trigger for the ontology sync pipeline."""

import logging
import sys
from pathlib import Path

from app.sync.orchestrator import run_sync

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def main() -> None:
    repo_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    success = run_sync(repo_dir)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
