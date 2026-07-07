from __future__ import annotations

from typing import Any

from . import logger


class MeshNetError(RuntimeError):
    """Structured operational error shared by the CLI and Python API."""

    def __init__(
        self,
        code: str,
        stage: str,
        message: str,
        action: str,
        *,
        retryable: bool = False,
        attempts: int = 1,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.message = message
        self.action = action
        self.retryable = retryable
        self.attempts = attempts
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "stage": self.stage,
            "message": self.message,
            "action": self.action,
            "retryable": self.retryable,
            "attempts": self.attempts,
            "details": self.details,
        }

    def with_attempts(self, attempts: int) -> "MeshNetError":
        return MeshNetError(
            self.code,
            self.stage,
            self.message,
            self.action,
            retryable=self.retryable,
            attempts=attempts,
            details=self.details,
        )


def as_meshnet_error(
    exc: BaseException,
    stage: str,
    *,
    attempts: int = 1,
) -> MeshNetError:
    if isinstance(exc, MeshNetError):
        return exc.with_attempts(max(attempts, exc.attempts))

    message = str(exc).strip() or exc.__class__.__name__
    lowered = message.lower()

    if isinstance(exc, FileNotFoundError) and stage == "config":
        return MeshNetError(
            "CONFIG_NOT_FOUND",
            stage,
            message,
            "Create the YAML config or pass its correct path.",
            attempts=attempts,
        )
    if stage == "config":
        return MeshNetError(
            "CONFIG_INVALID",
            stage,
            message,
            "Fix the named YAML setting, then run the command again.",
            attempts=attempts,
        )
    if isinstance(exc, PermissionError) or "permission denied" in lowered:
        return MeshNetError(
            "RADIO_PERMISSION_DENIED",
            stage,
            message,
            "Add the user to the serial-access group, log out and back in, then retry.",
            attempts=attempts,
        )
    if "already in use" in lowered or "resource busy" in lowered:
        return MeshNetError(
            "RADIO_BUSY",
            stage,
            message,
            "Stop the other MeshNet, Meshtastic CLI, web, or serial process using this radio.",
            retryable=True,
            attempts=attempts,
        )
    if any(
        phrase in lowered
        for phrase in ("no serial radio", "no usb serial radio", "does not exist")
    ):
        return MeshNetError(
            "RADIO_NOT_FOUND",
            stage,
            message,
            "Attach the radio with a USB data cable and verify the configured serial port.",
            retryable=True,
            attempts=attempts,
        )
    if "not installed" in lowered or "not found in path" in lowered:
        return MeshNetError(
            "DEPENDENCY_MISSING",
            stage,
            message,
            "Run ./install.sh in the MeshNet checkout, then retry.",
            attempts=attempts,
        )
    if "timed out" in lowered or "timeout" in lowered or "did not return" in lowered:
        actions = {
            "connect": "Check local radio power, USB data cable, serial port, and permissions.",
            "setup": "Keep the local radio attached and powered, then retry setup.",
            "discovery": "Start the peer and check matching channel, PSK, region, and antennas.",
            "delivery": "Check the peer runtime, matching configs, antennas, and radio range.",
        }
        return MeshNetError(
            f"{stage.upper()}_TIMEOUT",
            stage,
            message,
            actions.get(
                stage,
                "Check radio power, USB, channel settings, antennas, and the receiving node.",
            ),
            retryable=True,
            attempts=attempts,
        )

    return MeshNetError(
        f"{stage.upper()}_FAILED",
        stage,
        message,
        "Review the preceding logs, correct the reported problem, and retry.",
        retryable=True,
        attempts=attempts,
    )


def log_meshnet_error(error: MeshNetError, scope: str = "error") -> None:
    logger.line(scope, f"{error.code}: {error.message}")
    logger.detail(f"stage: {error.stage}", indent=8)
    logger.detail(f"attempts: {error.attempts}", indent=8)
    logger.detail(f"action: {error.action}", indent=8)
    for name, value in error.details.items():
        if isinstance(value, list):
            for item in value:
                logger.detail(f"{name}: {item}", indent=8)
        else:
            logger.detail(f"{name}: {value}", indent=8)
