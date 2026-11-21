"""Prior Art — research agent entry point (ODSC × Nebius hackathon template layout).

    python src/agent.py "your idea in a sentence or a paragraph"
    python src/agent.py serve        # web UI at http://127.0.0.1:8000
    python src/agent.py --help
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from priorart.__main__ import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
