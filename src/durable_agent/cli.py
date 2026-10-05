"""Command-line entrypoint. Subcommands (start, approve, status, demo-crash) land in M3."""

from __future__ import annotations

import argparse
import sys

from durable_agent import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="durable-agent",
        description="Claude text-to-SQL analyst on Temporal.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
