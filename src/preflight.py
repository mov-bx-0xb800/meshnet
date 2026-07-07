from __future__ import annotations

import importlib.util
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import logger
from .config import MeshConfig
from .errors import MeshNetError, as_meshnet_error
from .radio import detect_serial_ports, find_meshtastic_cli


@dataclass
class PreflightResult:
    ok: bool = True
    port: str | None = None
    meshtastic_cli: str | None = None
    errors: list[str] = field(default_factory=list)

    def fail(self, message: str) -> None:
        self.ok = False
        self.errors.append(message)


class PreflightError(MeshNetError):
    def __init__(self, result: PreflightResult, attempts: int) -> None:
        message = "; ".join(result.errors) or "preflight failed"
        error = as_meshnet_error(RuntimeError(message), "preflight", attempts=attempts)
        super().__init__(
            error.code,
            error.stage,
            error.message,
            error.action,
            retryable=error.retryable,
            attempts=error.attempts,
            details={"errors": list(result.errors), "port": result.port},
        )


def check_meshtastic_python() -> bool:
    return (
        importlib.util.find_spec("meshtastic") is not None
        and importlib.util.find_spec("meshtastic.serial_interface") is not None
    )


def choose_port(cfg: MeshConfig, result: PreflightResult, scope: str) -> str | None:
    if cfg.radio.port and cfg.radio.port != "auto":
        port = cfg.radio.port
        result.port = port
        if Path(port).exists():
            logger.line(scope, f"Configured serial port: {port} OK")
            return port
        result.fail(f"configured serial port does not exist: {port}")
        logger.line(scope, f"Configured serial port: {port} MISSING")
        return None

    ports = detect_serial_ports()
    if not ports:
        result.fail("no USB serial radio found")
        logger.line(scope, "USB serial radio: MISSING")
        return None

    result.port = ports[0]
    logger.line(scope, f"USB serial radio: {ports[0]} OK")
    if len(ports) > 1:
        logger.line(scope, "Additional serial devices detected:")
        for port in ports[1:]:
            logger.detail(f"- {port}", indent=10)
    return ports[0]


def verify_radio_reachable(
    port: str,
    result: PreflightResult,
    scope: str,
    timeout: int = 20,
) -> None:
    try:
        import meshtastic.serial_interface
    except Exception as exc:
        result.fail("Meshtastic Python SerialInterface import failed")
        logger.line(scope, f"Radio API import: FAILED ({exc})")
        return

    try:
        interface = meshtastic.serial_interface.SerialInterface(
            devPath=port,
            noNodes=True,
            timeout=timeout,
        )
        interface.close()
        logger.line(scope, "Radio reachable: OK")
    except PermissionError:
        result.fail(f"permission denied opening {port}")
        logger.line(scope, f"Radio reachable: FAILED permission denied on {port}")
    except Exception as exc:
        result.fail(f"radio is not reachable on {port}: {exc}")
        logger.line(scope, f"Radio reachable: FAILED ({exc})")


def preflight_check(
    cfg: MeshConfig,
    *,
    require_radio: bool = True,
    verify_radio: bool = True,
    scope: str = "preflight",
) -> PreflightResult:
    result = PreflightResult()
    logger.line(scope, "Checking required Meshtastic environment...")

    if check_meshtastic_python():
        logger.line(scope, "Meshtastic Python package: OK")
    else:
        result.fail("Meshtastic Python package is not installed")
        logger.line(scope, "Meshtastic Python package: MISSING")

    result.meshtastic_cli = find_meshtastic_cli()
    if result.meshtastic_cli:
        logger.line(scope, f"Meshtastic CLI: {result.meshtastic_cli}")
    else:
        result.fail("meshtastic CLI executable is not installed or not on PATH")
        logger.line(scope, "Meshtastic CLI: MISSING")

    port: str | None = None
    if require_radio:
        port = choose_port(cfg, result, scope)
        if port and verify_radio and check_meshtastic_python():
            verify_radio_reachable(port, result, scope)

    if result.ok:
        logger.line(scope, "Preflight passed. Proceeding.")
    else:
        logger.line(scope, "BLOCKED - MeshNet will not start.")
        logger.line(scope, "Reasons:")
        for error in result.errors:
            logger.detail(f"- {error}", indent=10)
        logger.line(scope, "Required:")
        logger.detail("- Run ./install.sh so meshtastic[cli] is installed.", indent=10)
        logger.detail("- Attach a RAK/Meshtastic device over USB serial.", indent=10)
        logger.detail("- Use a USB data cable, not charge-only.", indent=10)
        logger.detail("- Make sure the user can access /dev/ttyACM* or /dev/ttyUSB*.", indent=10)
        logger.detail("- Close any other Meshtastic client using the same serial port.", indent=10)

    return result


def require_preflight(
    cfg: MeshConfig,
    *,
    verify_radio: bool = True,
    attempts: int | None = None,
) -> PreflightResult:
    max_attempts = attempts or cfg.runtime.connect_retries
    result = PreflightResult()
    for attempt in range(1, max_attempts + 1):
        if max_attempts > 1:
            logger.line("preflight", f"Attempt {attempt}/{max_attempts}.")
        result = preflight_check(cfg, verify_radio=verify_radio)
        if result.ok:
            return result
        error = PreflightError(result, attempt)
        if not error.retryable or attempt >= max_attempts:
            raise error
        logger.line(
            "preflight",
            f"Retrying in {cfg.runtime.retry_backoff_seconds} seconds "
            f"[{error.code}].",
        )
        time.sleep(cfg.runtime.retry_backoff_seconds)
    raise PreflightError(result, max_attempts)
