from __future__ import annotations

import argparse
import getpass
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.web.public_auth import (  # noqa: E402
    PUBLIC_PASSWORD_MIN_LENGTH,
    hash_public_password,
)


USERNAME_RE = re.compile(r"[A-Za-z0-9._-]{3,64}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a Finance Radar public Web credential environment block."
    )
    parser.add_argument("--username", default="admin")
    args = parser.parse_args()
    username = str(args.username).strip()
    if USERNAME_RE.fullmatch(username) is None:
        parser.error("username must be 3-64 ASCII letters, digits, dots, underscores or hyphens")

    password = getpass.getpass("Public Web password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        parser.error("password confirmation does not match")
    if len(password) < PUBLIC_PASSWORD_MIN_LENGTH:
        parser.error(
            f"password must contain at least {PUBLIC_PASSWORD_MIN_LENGTH} characters"
        )

    print(f"FINANCE_RADAR_PUBLIC_USERNAME={username}")
    print(f"FINANCE_RADAR_PUBLIC_PASSWORD_HASH={hash_public_password(password)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
