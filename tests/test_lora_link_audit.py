from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "lora_link_audit.py"
SPEC = importlib.util.spec_from_file_location("lora_link_audit", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
audit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = audit
SPEC.loader.exec_module(audit)


class LoraLinkAuditFrameTests(unittest.TestCase):
    def test_payload_is_exact_requested_size(self) -> None:
        payload = audit.make_payload(audit.TYPE_PROBE, 7, 64, 123)

        self.assertEqual(len(payload), 64)
        frame = audit.decode_frame(payload)
        self.assertIsNotNone(frame)
        assert frame is not None
        self.assertEqual(frame.kind, audit.TYPE_PROBE)
        self.assertEqual(frame.seq, 7)
        self.assertEqual(frame.token, 123)
        self.assertEqual(frame.body_len, 64 - audit.HEADER.size)

    def test_invalid_payload_returns_none(self) -> None:
        self.assertIsNone(audit.decode_frame(b"not-an-audit-frame"))


if __name__ == "__main__":
    unittest.main()
