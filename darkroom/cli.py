"""darkroom CLI entry point — dispatch to subcommands."""
from __future__ import annotations

import argparse

from darkroom import catalog_cli, finish, ingest, prep
from darkroom.triage import cli as triage_cli


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="darkroom",
        description="Astrophotography pipeline: catalog, archive ingestion, "
                    "WBPP session prep, and finishing.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    catalog_cli.add_subparser(sub)
    ingest.add_subparser(sub)
    prep.add_subparser(sub)
    finish.add_subparser(sub)
    triage_cli.add_subparser(sub)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
