from __future__ import annotations

import argparse
import signal
import time

from .controlled_page import ControlledPageServer


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve the controlled browser test page.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    server = ControlledPageServer(host=args.host, port=args.port)
    server.start()

    def _shutdown(signum, frame) -> None:  # type: ignore[no-untyped-def]
        server.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
