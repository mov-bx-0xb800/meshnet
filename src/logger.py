from __future__ import annotations

import logging
import sys
from typing import Iterable


def configure_logging(level: str) -> None:
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(level=numeric, format="%(levelname)s %(name)s: %(message)s")


def line(scope: str, message: str = "") -> None:
    if message:
        print(f"[{scope}] {message}", flush=True)
    else:
        print(f"[{scope}]", flush=True)


def blank() -> None:
    print("", flush=True)


def detail(message: str, indent: int = 10) -> None:
    print(f"{' ' * indent}{message}", flush=True)


def bullet_lines(scope: str, title: str, items: Iterable[str], indent: int = 8) -> None:
    line(scope, title)
    for item in items:
        print(f"{' ' * indent}- {item}", flush=True)


def die(scope: str, message: str, exit_code: int = 1) -> None:
    line(scope, message)
    raise SystemExit(exit_code)


def exception_line(scope: str, message: str, exc: BaseException) -> None:
    line(scope, f"{message}: {exc}")


def flush() -> None:
    sys.stdout.flush()
