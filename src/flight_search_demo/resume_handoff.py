from __future__ import annotations

import argparse
import getpass
from pathlib import Path

from .spike import complete_handoff_file


def main() -> None:
    parser = argparse.ArgumentParser(description="Explicitly complete a waiting browser handoff.")
    parser.add_argument("--handoff-file", default=".artifacts/controlled-page/handoff.json")
    args = parser.parse_args()
    token = getpass.getpass("Resume token: ")
    complete_handoff_file(Path(args.handoff_file), token)
    print("Handoff completed. Automation may resume.")


if __name__ == "__main__":
    main()