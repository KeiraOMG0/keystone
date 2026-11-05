"""
Entry point: `python -m setup_server [--check] [--dry-run]` (run from the repo root).

The real implementation lives in setup_server/main.py so it can be
unit-tested and imported in pieces.
"""

from setup_server.main import main

if __name__ == "__main__":
    main()
