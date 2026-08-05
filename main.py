from __future__ import annotations

import sys


def main(argv: list[str] | None = None):
    from src.core.env import load_repo_dotenv

    load_repo_dotenv()


if __name__ == "__main__":
    sys.exit(main())
