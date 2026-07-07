from __future__ import annotations

import threading
import unittest
from collections import OrderedDict
from dataclasses import replace
from unittest.mock import Mock, patch

from meshnet_api import MeshNetClient
from src.config import PeerConfig, channel_psk_for_cli, load_config
from src.errors import MeshNetError
from src.master import MasterNode
from src.node import MeshNode
from src.protocol import make_message
from src.radio import RadioClient, radio_config_mismatches, setup_radio_reliably
from src.slave import run_slave
from src.state import SEEN_TTL_SECONDS


class ReliabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = load_config("config.master.yaml")

    @patch("src.node.time.sleep", return_value=None)
    def test_connection_retries_transient_failure(self, _sleep: Mock) -> None:
        node = object.__new__(MeshNode)
        node.cfg = self.cfg
        node.scope = "test"
        node.radio = Mock()
        node.radio.connect.side_effect = [RuntimeError("no serial radio found"), None]

        MeshNode.connect(node)

        self.assertEqual(node.radio.connect.call_count, 2)
        node.radio.close.assert_called_once()

    @patch("src.master.time.sleep", return_value=None)
    def test_discovery_retries_until_peer_found(self, _sleep: Mock) -> None:
        node = object.__new__(MasterNode)
        node.cfg = self.cfg
        node.last_discovery_attempts = 0
        expected = [Mock(src="slave-001")]
        node._discover_once = Mock(side_effect=[[], expected])

        found = MasterNode.discover(node, 1)

        self.assertEqual(found, expected)
        self.assertEqual(node.last_discovery_attempts, 2)

    @patch("src.radio.time.sleep", return_value=None)
    @patch("src.radio.verify_radio_configuration", return_value={"verified": True})
    @patch("src.radio.setup_radio")
    @patch("src.preflight.require_preflight", return_value=Mock(ok=True))
    def test_setup_retries_and_verifies(
        self,
        _preflight: Mock,
        apply_setup: Mock,
        verify: Mock,
        _sleep: Mock,
    ) -> None:
        apply_setup.side_effect = [RuntimeError("radio did not return"), None]

        attempts = setup_radio_reliably(self.cfg)

        self.assertEqual(attempts, 2)
        self.assertEqual(apply_setup.call_count, 2)
        verify.assert_called_once_with(self.cfg)

    def test_setup_verification_reports_exact_mismatch(self) -> None:
        psk = channel_psk_for_cli(self.cfg).removeprefix("base64:")
        actual = {
            "owner.long_name": self.cfg.app.node_name,
            "owner.short_name": self.cfg.app.node_short_name,
            "lora.region": self.cfg.radio.region,
            "lora.modem_preset": self.cfg.radio.modem_preset,
            "lora.hop_limit": self.cfg.radio.hop_limit,
            "lora.tx_enabled": self.cfg.radio.transmit_enabled,
            "lora.channel_num": self.cfg.radio.frequency_slot,
            "lora.tx_power": self.cfg.radio.tx_power,
            "lora.ignore_mqtt": self.cfg.radio.ignore_mqtt,
            "lora.config_ok_to_mqtt": self.cfg.radio.ok_to_mqtt,
            "device.role": self.cfg.device.role,
            "device.rebroadcast_mode": self.cfg.device.rebroadcast_mode,
            "device.node_info_broadcast_secs": self.cfg.device.node_info_broadcast_secs,
            "device.serial_enabled": self.cfg.device.serial_enabled,
            "power.is_power_saving": self.cfg.device.is_power_saving,
            "channel.name": "WRONG",
            "channel.psk_base64": psk,
        }

        mismatches = radio_config_mismatches(self.cfg, actual)

        self.assertEqual(len(mismatches), 1)
        self.assertIn("channel.name", mismatches[0])

    def test_binary_radio_path_preserves_non_utf8_payload(self) -> None:
        radio = RadioClient(self.cfg, "test")
        radio.interface = Mock()
        radio.interface.sendData.return_value = {"id": 123}
        binary_handler = Mock()
        text_handler = Mock()
        radio.add_binary_handler(binary_handler)
        radio.add_handler(text_handler)

        sent = radio.send_bytes(b"\xff\x00\x80", destination_id="!00000001")
        radio._on_receive(
            {"fromId": "!00000001", "decoded": {"payload": b"\xff\x00\x80"}}
        )

        self.assertEqual(sent.packet_id, "123")
        self.assertEqual(radio.interface.sendData.call_args.args[0], b"\xff\x00\x80")
        binary_handler.assert_called_once()
        text_handler.assert_not_called()

    def test_configured_peer_mesh_id_is_used_before_discovery_state(self) -> None:
        node = object.__new__(MeshNode)
        node.cfg = replace(
            self.cfg,
            network=replace(
                self.cfg.network,
                peers=(PeerConfig("slave-001", "!a1b2c3d4"),),
            ),
        )
        node.state = Mock()

        destination = MeshNode.radio_destination_for(node, "slave-001")

        self.assertEqual(destination, "!a1b2c3d4")
        node.state.get_mesh_id.assert_not_called()

    def test_response_cache_has_size_limit_and_ttl(self) -> None:
        node = object.__new__(MeshNode)
        node._response_cache = OrderedDict()
        node._response_cache_lock = threading.Lock()
        responses = [
            make_message(self.cfg, "text_ack", dst="slave-001", body=f"reply-{index}")
            for index in range(3)
        ]

        with (
            patch("src.node.MAX_RESPONSE_CACHE_ENTRIES", 2),
            patch("src.node.time.monotonic", side_effect=[100.0, 101.0, 102.0]),
        ):
            for index, response in enumerate(responses):
                node._cache_response("slave-001", f"request-{index}", response)

        self.assertEqual(len(node._response_cache), 2)
        self.assertNotIn(("slave-001", "request-0"), node._response_cache)
        with patch("src.node.time.monotonic", return_value=102.0 + SEEN_TTL_SECONDS + 1):
            cached = node._cached_response("slave-001", "request-2")
        self.assertIsNone(cached)
        self.assertFalse(node._response_cache)

    @patch("src.node.time.sleep", return_value=None)
    def test_delivery_timeout_contains_helpful_error(self, _sleep: Mock) -> None:
        node = object.__new__(MeshNode)
        node.cfg = self.cfg
        node.scope = "test"
        node._seq = 0
        node.state = Mock()
        node.radio_destination_for = Mock(return_value="^all")
        node.wait_for_message = Mock(return_value=None)
        envelope = make_message(self.cfg, "text", dst="slave-001", body="hello")

        def send_with_radio_timeout(*_args: object, **_kwargs: object):
            node._last_transport_error = "radio acknowledgement timed out"
            node._last_transport_code = "RADIO_ACK_TIMEOUT"
            node._last_transport_action = "Check antennas and range."
            return envelope

        node.send = Mock(side_effect=send_with_radio_timeout)

        result = MeshNode.send_reliable(
            node,
            "text",
            "slave-001",
            "hello",
            expect_reply_type="text_ack",
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.attempts, self.cfg.runtime.send_retries)
        self.assertEqual(result.error_code, "RADIO_ACK_TIMEOUT")
        self.assertIn("text_ack was not received", result.last_error)
        self.assertTrue(result.action)

    @patch("meshnet_api.client.setup_radio_reliably")
    def test_setup_api_returns_structured_failure(self, setup: Mock) -> None:
        setup.side_effect = MeshNetError(
            "SETUP_VERIFY_FAILED",
            "setup",
            "radio settings differ",
            "Rerun setup.",
            retryable=True,
            attempts=3,
        )
        client = MeshNetClient("config.master.yaml")

        result = client.setup_radio()

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "SETUP_VERIFY_FAILED")
        self.assertEqual(result.attempts, 3)
        self.assertEqual(result.action, "Rerun setup.")

    @patch("meshnet_api.client.MasterNode")
    @patch("meshnet_api.client.require_preflight")
    def test_send_api_returns_structured_preflight_failure(
        self,
        preflight: Mock,
        node_class: Mock,
    ) -> None:
        preflight.side_effect = MeshNetError(
            "RADIO_NOT_FOUND",
            "preflight",
            "no USB radio found",
            "Attach the radio.",
            retryable=True,
            attempts=3,
        )
        client = MeshNetClient("config.master.yaml")

        result = client.send_message("hello")

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "RADIO_NOT_FOUND")
        self.assertEqual(result.attempts, 3)
        self.assertEqual(result.action, "Attach the radio.")
        node_class.return_value.close.assert_called_once()

    @patch("src.slave.time.sleep", return_value=None)
    @patch("src.slave.SlaveNode")
    def test_slave_runtime_reconnects_after_transient_failure(
        self,
        node_class: Mock,
        _sleep: Mock,
    ) -> None:
        failed_node = Mock()
        failed_node.connect.side_effect = MeshNetError(
            "RADIO_DISCONNECTED",
            "connect",
            "radio disconnected",
            "Reconnect USB.",
            retryable=True,
        )
        recovered_node = Mock()
        recovered_node.run_forever.return_value = None
        node_class.side_effect = [failed_node, recovered_node]

        run_slave(load_config("config.slave.yaml"))

        self.assertEqual(node_class.call_count, 2)
        failed_node.close.assert_called_once()
        recovered_node.connect.assert_called_once()
        recovered_node.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
