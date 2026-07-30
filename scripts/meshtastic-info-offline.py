#!/usr/bin/env python3
from __future__ import annotations

import sys

import meshtastic.util
import requests


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1].startswith("/dev/"):
        print("usage: meshtastic-info-offline.py /dev/ttyACM0", file=sys.stderr)
        return 2

    # Meshtastic's --info command normally queries PyPI for an update. The
    # Flower radio benchmark must remain strictly offline.
    meshtastic.util.check_if_newer_version = lambda: None

    def block_http(*_args, **_kwargs):
        raise RuntimeError("HTTP is disabled during the LoRa benchmark")

    requests.sessions.Session.request = block_http

    from meshtastic import __main__ as meshtastic_cli

    port = sys.argv[1]
    sys.argv = [
        "meshtastic",
        "--port",
        port,
        "--no-nodes",
        "--info",
    ]
    meshtastic_cli.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
