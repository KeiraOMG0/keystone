"""
Entry point shim: `python setup_server.py [--check] [--dry-run]`

The real implementation lives in the setup_server/ package so it can be
unit-tested and imported in pieces. This file just makes the CLI invocation
in the project README/spec work from the repo root.
"""

from setup_server.main import main

if __name__ == "__main__":
    main()
