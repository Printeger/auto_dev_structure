"""Fail-closed placeholder for the V4 MCP console entry point.

The working stdio transport and tools are implemented in TASK-011. Keeping this
module importable makes the package metadata honest during the contract slice
without exposing a partial protocol server.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from autodev import __version__


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autodev-mcp")
    parser.add_argument("--stdio", action="store_true", help="serve MCP over standard I/O")
    parser.add_argument("--version", action="store_true", help="print the package version")
    args = parser.parse_args(argv)

    if args.version:
        print(__version__)
        return 0
    if args.stdio:
        print("autodev-mcp stdio is not available until the V4 MCP slice is installed", file=sys.stderr)
        return 2
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
