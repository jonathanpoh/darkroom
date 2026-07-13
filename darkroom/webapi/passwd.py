"""darkroom.webapi.passwd — generate a DARKROOM_UI_PASSWORD_HASH value.

Usage:
    python -m darkroom.webapi.passwd
        Interactive: prompts for the password twice (hidden input), exits 1
        on mismatch or empty input, otherwise prints
        `DARKROOM_UI_PASSWORD_HASH=scrypt$...` to stdout — paste that line
        into the server's environment file.

    python -m darkroom.webapi.passwd --hash PASSWORD
        Non-interactive: prints just the hash value (no env-var prefix),
        for use in scripts (e.g. scripts/dev-snapshot.sh).
"""

from __future__ import annotations

import argparse
import getpass
import sys

from darkroom.webapi.auth import hash_password


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="darkroom.webapi.passwd")
    parser.add_argument(
        "--hash",
        metavar="PASSWORD",
        help="Hash PASSWORD non-interactively and print just the hash value.",
    )
    args = parser.parse_args(argv)

    if args.hash is not None:
        print(hash_password(args.hash))
        return 0

    password = getpass.getpass("Password: ")
    confirm = getpass.getpass("Confirm password: ")
    if not password:
        print("error: password must not be empty", file=sys.stderr)
        return 1
    if password != confirm:
        print("error: passwords do not match", file=sys.stderr)
        return 1

    print(f"DARKROOM_UI_PASSWORD_HASH={hash_password(password)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
