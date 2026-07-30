from __future__ import annotations

import importlib.util
import socket
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "flower_round_benchmark.py"
SPEC = importlib.util.spec_from_file_location("flower_round_benchmark", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
benchmark = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = benchmark
SPEC.loader.exec_module(benchmark)


class FlowerRoundBenchmarkPlanTests(unittest.TestCase):
    def test_current_model_transfer_plan(self) -> None:
        plan = benchmark.transfer_plan(48_712, payload_bytes=160, window_size=8)

        self.assertEqual(plan.data_frames, 305)
        self.assertEqual(plan.windows, 39)
        self.assertEqual(plan.ack_frames, 39)
        self.assertEqual(plan.poll_frames, 39)
        self.assertEqual(plan.poll_done_frames, 78)
        self.assertEqual(plan.approx_packets, 461)
        self.assertAlmostEqual(plan.payload_seconds_at_target, 69.589, places=3)
        self.assertAlmostEqual(plan.minimum_schedule_seconds, 203.9, places=3)

    def test_two_client_full_round_plan_with_evaluate(self) -> None:
        plan = benchmark.round_plan(
            48_712,
            rounds=1,
            logical_clients=2,
            include_evaluate=True,
            payload_bytes=160,
            window_size=8,
        )

        self.assertEqual(plan.full_model_transfers_per_round, 6)
        self.assertEqual(plan.bytes_per_round, 292_272)
        self.assertEqual(plan.data_frames_per_round, 1_830)
        self.assertEqual(plan.windows_per_round, 234)
        self.assertEqual(plan.approx_packets_per_round, 2_766)
        self.assertAlmostEqual(plan.payload_seconds_per_round_at_target, 417.531, places=3)
        self.assertAlmostEqual(plan.minimum_schedule_seconds_per_round, 1_223.4, places=3)
        self.assertAlmostEqual(plan.total_minimum_schedule_seconds, 1_223.4, places=3)

    def test_complete_three_round_plan_surfaces_hour_scale_schedule(self) -> None:
        plan = benchmark.round_plan(
            48_712,
            rounds=3,
            logical_clients=2,
            include_evaluate=True,
            payload_bytes=160,
            window_size=8,
            frame_interval_ms=400,
            poll_interval_ms=500,
        )

        self.assertAlmostEqual(plan.total_minimum_schedule_seconds, 3_670.2, places=3)

    def test_two_client_fit_only_round_plan(self) -> None:
        plan = benchmark.round_plan(
            48_712,
            rounds=1,
            logical_clients=2,
            include_evaluate=False,
            payload_bytes=160,
            window_size=8,
        )

        self.assertEqual(plan.full_model_transfers_per_round, 4)
        self.assertEqual(plan.bytes_per_round, 194_848)
        self.assertEqual(plan.approx_packets_per_round, 1_844)


class FlowerRoundBenchmarkProtocolTests(unittest.TestCase):
    def test_peer_compatibility_reports_git_and_firmware_mismatches(self) -> None:
        errors = benchmark.peer_compatibility_errors(
            local_git_commit="new",
            local_firmware_version="2.7.15",
            remote_git_commit="old",
            remote_firmware_version="2.5.20",
        )

        self.assertEqual(len(errors), 2)
        self.assertIn("git commit mismatch", errors[0])
        self.assertIn("firmware mismatch", errors[1])

    def test_peer_compatibility_allows_missing_probe_metadata(self) -> None:
        self.assertEqual(
            benchmark.peer_compatibility_errors(
                local_git_commit="same",
                local_firmware_version="",
                remote_git_commit="same",
                remote_firmware_version="2.5.20",
            ),
            [],
        )

    def test_message_frame_round_trips_with_binary_payload(self) -> None:
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)

        payload = benchmark.payload_for("frame-test", 4096)
        benchmark.send_message(left, {"kind": "model", "size": len(payload)}, payload)
        header, received, elapsed = benchmark.recv_message(right)

        self.assertEqual(header["kind"], "model")
        self.assertEqual(header["size"], len(payload))
        self.assertEqual(header["body_bytes"], len(payload))
        self.assertEqual(received, payload)
        self.assertGreaterEqual(elapsed, 0.0)

    def test_npz_tensor_byte_counter_sums_uncompressed_npy_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "weights.npz"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("arr_0.npy", b"x" * 128)
                archive.writestr("arr_1.npy", b"y" * 256)
                archive.writestr("metadata.json", b"ignored")

            self.assertEqual(benchmark.flower_tensor_bytes_from_npz(path), 384)


if __name__ == "__main__":
    unittest.main()
