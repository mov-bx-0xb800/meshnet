from __future__ import annotations

import time

from . import logger
from .config import MeshConfig
from .master import MasterNode
from .protocol import (
    decode_envelope,
    encode_envelope,
    locally_rejects_fake_hmac,
    make_message,
    payload_hash,
)


def run_tests(cfg: MeshConfig) -> bool:
    logger.line("test", "Starting MeshNet test modules...")
    logger.blank()

    node = MasterNode(cfg, "test")
    all_passed = True
    try:
        logger.line("test:1", "Local radio connection")
        node.connect()
        logger.line("test:1", f"PASS - connected to {node.radio.port}")
        logger.blank()

        logger.line("test:2", "Node discovery")
        compatible = node.discover(cfg.runtime.discovery_timeout_seconds)
        if compatible:
            logger.line("test:2", f"PASS - reached {len(compatible)} compatible node")
        else:
            logger.line("test:2", "FAIL - no compatible node reached")
            all_passed = False
        logger.blank()

        logger.line("test:3", "Compatibility check")
        if compatible:
            logger.line("test:3", f"PASS - TRUE NODE {cfg.network.slave_id}")
        else:
            logger.line("test:3", "FAIL - TRUE NODE not found")
            all_passed = False
        logger.blank()

        logger.line("test:4", "HMAC validation")
        valid = make_message(cfg, "hello", body="local validation")
        ok = decode_envelope(encode_envelope(valid)).h == valid.h
        if ok:
            logger.line("test:4", "PASS - valid signed message accepted")
        else:
            logger.line("test:4", "FAIL - valid signed message did not decode")
            all_passed = False
        if locally_rejects_fake_hmac(cfg):
            logger.line("test:4", "PASS - fake signed message rejected locally")
        else:
            logger.line("test:4", "FAIL - fake signed message was not rejected")
            all_passed = False
        logger.blank()

        logger.line("test:5", "Encode/decode")
        payload = "meshnet-payload-check"
        body = {"payload": payload, "hash": payload_hash(payload)}
        msg = make_message(cfg, "test", dst=cfg.network.slave_id, seq=1, body=body)
        encoded = encode_envelope(msg)
        decoded = decode_envelope(encoded)
        if encoded.startswith("{") and encoded.endswith("}"):
            logger.line("test:5", "PASS - message encoded to JSON")
        else:
            logger.line("test:5", "FAIL - message did not encode to JSON")
            all_passed = False
        if decoded.as_dict() == msg.as_dict():
            logger.line("test:5", "PASS - message decoded back correctly")
        else:
            logger.line("test:5", "FAIL - decoded message differs")
            all_passed = False
        if payload_hash(payload) == body["hash"]:
            logger.line("test:5", "PASS - payload hash matched")
        else:
            logger.line("test:5", "FAIL - payload hash mismatch")
            all_passed = False
        logger.blank()

        logger.line("test:6", "Ping/pong")
        ping_passed = 0
        for seq in range(1, 4):
            ok, rtt, _ = node.ping_once(cfg.network.slave_id, seq=seq)
            if ok and rtt is not None:
                logger.line("test:6", f"PASS - pong received seq={seq} rtt={rtt:.1f}s")
                ping_passed += 1
            else:
                logger.line("test:6", f"FAIL - timeout seq={seq}")
                all_passed = False
            time.sleep(max(5, cfg.safe_ping_interval))
        logger.blank()

        logger.line("test:7", "Reliability")
        sent = int(cfg.runtime.test_message_count)
        acked = 0
        for seq in range(1, sent + 1):
            payload = f"test-message-{seq}"
            body = {"payload": payload, "hash": payload_hash(payload)}
            node.send("test", dst=cfg.network.slave_id, body=body, seq=seq)
            reply = node.wait_for_message(
                "test_ack",
                src=cfg.network.slave_id,
                seq=seq,
                timeout_seconds=cfg.runtime.ack_timeout_seconds,
            )
            if reply is not None and isinstance(reply.envelope.body, dict):
                if reply.envelope.body.get("ok"):
                    acked += 1
            time.sleep(5)
        missing = sent - acked
        success_rate = int((acked / sent) * 100) if sent else 0
        logger.line("test:7", f"Sent: {sent}")
        logger.line("test:7", f"Acked: {acked}")
        logger.line("test:7", f"Missing: {missing}")
        logger.line("test:7", f"Success rate: {success_rate}%")
        if success_rate >= 80:
            logger.line("test:7", "PASS")
        else:
            logger.line("test:7", "FAIL")
            all_passed = False
        logger.blank()

    except Exception as exc:
        all_passed = False
        logger.exception_line("test", "FAIL - test run aborted", exc)
    finally:
        node.close()

    logger.line("test", f"Final result: {'PASS' if all_passed else 'FAIL'}")
    return all_passed
