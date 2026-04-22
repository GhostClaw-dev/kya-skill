#!/usr/bin/env python3
"""kya_lib unit tests — stdlib unittest, no pip deps.

Run: python3 scripts/test_kya_lib.py
or:  python3 -m unittest scripts.test_kya_lib

Covers:
  - validate_address / validate_tweet_url 边界
  - typed-data 构造与后端契约的字段顺序、类型字符串、domain 一致
  - now_unix_seconds / new_signature_nonce 输出形态
  - kya_prepare_twitter / kya_claim_twitter 通过 monkeypatch HTTP 拿到正确 headers/body
  - kya_poll_attestation 命中 active 立刻返回；超时返回 None
  - sign_typed_data 通过 monkeypatch awp-wallet 拿到合法 0x...130 签名
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

# 让 import 命中本目录下的 kya_lib（不用 pip 安装）
sys.path.insert(0, str(Path(__file__).parent))

import kya_lib  # noqa: E402


SAMPLE_ADDR = "0x" + "ab" * 20  # 0xabab...ab (40 hex chars)
SAMPLE_OWNER = "0x" + "cd" * 20
SAMPLE_SIG = "0x" + "ef" * 65  # 130 hex chars after 0x

# 混合大小写版本 — 用来验证 lowercase 化（注意 0x 前缀本身保持小写，
# 因为 viem 的地址 regex 一律要求 0x 小写前缀）
SAMPLE_ADDR_MIXED = "0x" + "AbAb" * 10


class ValidationTests(unittest.TestCase):
    def test_validate_address_lowercases(self) -> None:
        v = kya_lib.validate_address(SAMPLE_ADDR_MIXED)
        self.assertEqual(v, SAMPLE_ADDR_MIXED.lower())

    def test_validate_address_rejects_garbage(self) -> None:
        with self.assertRaises(SystemExit):
            kya_lib.validate_address("not-an-address")

    def test_validate_tweet_url_accepts_x_and_twitter(self) -> None:
        self.assertEqual(
            kya_lib.validate_tweet_url("https://x.com/me/status/12345"),
            "https://x.com/me/status/12345",
        )
        self.assertEqual(
            kya_lib.validate_tweet_url("https://twitter.com/me/status/12345?s=20"),
            "https://twitter.com/me/status/12345?s=20",
        )

    def test_validate_tweet_url_rejects_unrelated_domain(self) -> None:
        with self.assertRaises(SystemExit):
            kya_lib.validate_tweet_url("https://example.com/me/status/12345")


class TypedDataTests(unittest.TestCase):
    def test_action_typed_data_matches_backend_schema(self) -> None:
        td = kya_lib.build_action_typed_data(
            action="twitter_prepare",
            agent_address=SAMPLE_ADDR,
            timestamp=1_700_000_000,
            nonce="abcdef0123456789",
            chain_id=8453,
        )
        self.assertEqual(td["primaryType"], "Action")
        self.assertEqual(
            td["domain"], {"name": "KYA", "version": "1", "chainId": 8453}
        )
        # 字段名/顺序/类型 — 与 api/src/crypto/eip712.ts 完全一致
        self.assertEqual(
            td["types"]["Action"],
            [
                {"name": "action", "type": "string"},
                {"name": "agent_address", "type": "address"},
                {"name": "timestamp", "type": "uint64"},
                {"name": "nonce", "type": "string"},
            ],
        )
        # EIP712Domain 必须存在，否则 awp-wallet / cast 会拒签
        self.assertIn("EIP712Domain", td["types"])
        # uint64 字段以字符串传出，避免 JSON 数值精度丢失
        self.assertEqual(td["message"]["timestamp"], "1700000000")
        self.assertEqual(td["message"]["agent_address"], SAMPLE_ADDR)

    def test_action_typed_data_rejects_unknown_action(self) -> None:
        with self.assertRaises(SystemExit):
            kya_lib.build_action_typed_data(
                action="unknown_action",  # type: ignore[arg-type]
                agent_address=SAMPLE_ADDR,
                timestamp=1,
                nonce="x" * 16,
            )

    def test_kyc_init_typed_data_includes_owner(self) -> None:
        td = kya_lib.build_kyc_init_typed_data(
            agent_address=SAMPLE_ADDR,
            owner_address=SAMPLE_OWNER,
            timestamp=1,
            nonce="y" * 16,
            chain_id=1,
        )
        self.assertEqual(td["primaryType"], "KycInit")
        names = [f["name"] for f in td["types"]["KycInit"]]
        self.assertEqual(
            names, ["action", "agent_address", "owner_address", "timestamp", "nonce"]
        )
        self.assertEqual(td["message"]["owner_address"], SAMPLE_OWNER)
        self.assertEqual(td["message"]["action"], "kyc_init")

    def test_new_nonce_is_32_hex(self) -> None:
        n = kya_lib.new_signature_nonce()
        self.assertEqual(len(n), 32)
        int(n, 16)  # 不抛即合法 hex


class WalletBridgeTests(unittest.TestCase):
    """直接 mock `_awp_wallet_exec` 来模拟 awp-wallet CLI 的 (returncode, stdout, stderr)。"""

    def setUp(self) -> None:
        os.environ.pop("AWP_WALLET_TOKEN", None)

    def test_sign_typed_data_returns_signature(self) -> None:
        with mock.patch.object(
            kya_lib,
            "_awp_wallet_exec",
            return_value=(0, json.dumps({"signature": SAMPLE_SIG}), ""),
        ):
            sig = kya_lib.sign_typed_data({"any": "json"})
        self.assertEqual(sig, SAMPLE_SIG)

    def test_sign_typed_data_dies_on_malformed_sig(self) -> None:
        with mock.patch.object(
            kya_lib,
            "_awp_wallet_exec",
            return_value=(0, json.dumps({"signature": "0xnotvalid"}), ""),
        ), self.assertRaises(SystemExit):
            kya_lib.sign_typed_data({"any": "json"})

    def test_get_wallet_address_uses_eoaAddress(self) -> None:
        with mock.patch.object(
            kya_lib,
            "_awp_wallet_exec",
            return_value=(0, json.dumps({"eoaAddress": SAMPLE_ADDR_MIXED}), ""),
        ):
            addr = kya_lib.get_wallet_address()
        self.assertEqual(addr, SAMPLE_ADDR_MIXED.lower())


class UnlockTests(unittest.TestCase):
    """unlock + auto-retry 语义。"""

    def setUp(self) -> None:
        # 每个用例独立环境，避免 unlock 成功后 token 泄漏给下一个用例
        os.environ.pop("AWP_WALLET_TOKEN", None)

    def tearDown(self) -> None:
        os.environ.pop("AWP_WALLET_TOKEN", None)

    def test_unlock_wallet_parses_token_and_persists_env(self) -> None:
        with mock.patch.object(
            kya_lib,
            "_awp_wallet_exec",
            return_value=(0, json.dumps({"token": "tok-123"}), ""),
        ) as patched:
            token = kya_lib.unlock_wallet()
        self.assertEqual(token, "tok-123")
        self.assertEqual(os.environ.get("AWP_WALLET_TOKEN"), "tok-123")
        # 确认传给 awp-wallet 的参数符合 awp-skill 约定
        args = patched.call_args.args[0]
        self.assertEqual(args[0], "unlock")
        self.assertIn("--scope", args)
        self.assertIn("transfer", args)
        self.assertIn("--duration", args)
        self.assertIn("3600", args)

    def test_unlock_wallet_dies_on_non_zero_exit(self) -> None:
        with mock.patch.object(
            kya_lib,
            "_awp_wallet_exec",
            return_value=(1, "", "wallet not initialized"),
        ), self.assertRaises(SystemExit):
            kya_lib.unlock_wallet()

    def test_sign_auto_unlocks_when_locked(self) -> None:
        """第一次 sign-typed-data 报 "wallet is locked"，应自动 unlock 后重试成功。"""
        call_log: list[list[str]] = []

        def fake_exec(args: list[str], timeout: int = 60) -> tuple[int, str, str]:
            call_log.append(list(args))
            cmd = args[0]
            if cmd == "sign-typed-data":
                if "--token" in args:
                    return (0, json.dumps({"signature": SAMPLE_SIG}), "")
                return (1, "", "Error: wallet is locked; pass --token")
            if cmd == "unlock":
                return (0, json.dumps({"token": "fresh-tok"}), "")
            return (1, "", f"unexpected cmd {cmd}")

        with mock.patch.object(kya_lib, "_awp_wallet_exec", side_effect=fake_exec):
            sig = kya_lib.sign_typed_data({"any": "json"})
        self.assertEqual(sig, SAMPLE_SIG)

        cmds = [c[0] for c in call_log]
        self.assertEqual(cmds, ["sign-typed-data", "unlock", "sign-typed-data"])
        # 第 3 次调用必须带上刚拿到的 token
        self.assertIn("--token", call_log[2])
        self.assertIn("fresh-tok", call_log[2])

    def test_sign_does_not_retry_on_unrelated_error(self) -> None:
        """非 token 问题（例如用户拒签）不应触发 unlock，避免掩盖真实错误。"""
        call_log: list[list[str]] = []

        def fake_exec(args: list[str], timeout: int = 60) -> tuple[int, str, str]:
            call_log.append(list(args))
            return (1, "", "user rejected signature request")

        with mock.patch.object(
            kya_lib, "_awp_wallet_exec", side_effect=fake_exec
        ), self.assertRaises(SystemExit):
            kya_lib.sign_typed_data({"any": "json"})
        # 只应尝试一次，不重试
        self.assertEqual(len(call_log), 1)

    def test_sign_fails_after_unlock_fails(self) -> None:
        """第一次 lock 错误 → unlock 又失败 → die（不要无限循环）。"""

        def fake_exec(args: list[str], timeout: int = 60) -> tuple[int, str, str]:
            cmd = args[0]
            if cmd == "sign-typed-data":
                return (1, "", "wallet is locked")
            if cmd == "unlock":
                return (1, "", "no wallet found")
            return (1, "", "nope")

        with mock.patch.object(
            kya_lib, "_awp_wallet_exec", side_effect=fake_exec
        ), self.assertRaises(SystemExit):
            kya_lib.sign_typed_data({"any": "json"})

    def test_sign_uses_existing_env_token_without_unlock(self) -> None:
        """AWP_WALLET_TOKEN 已存在且 awp-wallet 接受时，不应触发 unlock。"""
        os.environ["AWP_WALLET_TOKEN"] = "preset-tok"
        call_log: list[list[str]] = []

        def fake_exec(args: list[str], timeout: int = 60) -> tuple[int, str, str]:
            call_log.append(list(args))
            return (0, json.dumps({"signature": SAMPLE_SIG}), "")

        with mock.patch.object(kya_lib, "_awp_wallet_exec", side_effect=fake_exec):
            sig = kya_lib.sign_typed_data({"any": "json"})
        self.assertEqual(sig, SAMPLE_SIG)
        self.assertEqual(len(call_log), 1)
        self.assertIn("--token", call_log[0])
        self.assertIn("preset-tok", call_log[0])


class HttpClientTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["KYA_API_BASE"] = "https://kya.test"

    def tearDown(self) -> None:
        os.environ.pop("KYA_API_BASE", None)

    def test_prepare_twitter_sends_signed_headers_and_body(self) -> None:
        captured: dict = {}

        def fake_request(method, url, headers=None, body=None, timeout=20):
            captured.update(method=method, url=url, headers=headers, body=body)
            return 200, {"nonce": "KYA-1234", "claim_text": "..", "expires_at": "..."}

        with mock.patch.object(kya_lib, "_http_request", side_effect=fake_request):
            res = kya_lib.kya_prepare_twitter(
                agent_address=SAMPLE_ADDR,
                signature=SAMPLE_SIG,
                timestamp=42,
                nonce="cafebabecafebabe",
            )
        self.assertEqual(res["nonce"], "KYA-1234")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(
            captured["url"], "https://kya.test/v1/attestations/twitter/prepare"
        )
        self.assertEqual(captured["headers"]["X-Agent-Signature"], SAMPLE_SIG)
        self.assertEqual(captured["headers"]["X-Agent-Timestamp"], "42")
        self.assertEqual(captured["headers"]["X-Agent-Nonce"], "cafebabecafebabe")
        self.assertEqual(captured["body"], {"agent_address": SAMPLE_ADDR})

    def test_claim_twitter_uses_business_nonce_in_body(self) -> None:
        captured: dict = {}

        def fake_request(method, url, headers=None, body=None, timeout=20):
            captured.update(body=body)
            return 200, {"attestation_id": "att_x", "status": "pending"}

        with mock.patch.object(kya_lib, "_http_request", side_effect=fake_request):
            kya_lib.kya_claim_twitter(
                agent_address=SAMPLE_ADDR,
                tweet_url="https://x.com/me/status/1",
                claim_nonce="KYA-1234",
                signature=SAMPLE_SIG,
                timestamp=1,
                nonce="x" * 16,
            )
        # claim_nonce 必须以 "nonce" key 出现在 body，跟后端 zod schema 对齐
        self.assertEqual(captured["body"]["nonce"], "KYA-1234")
        self.assertEqual(captured["body"]["agent_address"], SAMPLE_ADDR)
        self.assertEqual(captured["body"]["tweet_url"], "https://x.com/me/status/1")

    def test_check_response_dies_on_error_payload(self) -> None:
        with self.assertRaises(SystemExit):
            kya_lib._check_response(
                400, {"error": {"code": "INVALID_INPUT", "message": "bad"}}, "test"
            )


class PollerTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["KYA_API_BASE"] = "https://kya.test"

    def tearDown(self) -> None:
        os.environ.pop("KYA_API_BASE", None)

    def test_poll_attestation_returns_when_active(self) -> None:
        attestation = {"id": "att_x", "status": "active"}

        def fake_list(*, agent_address, type_filter):  # noqa: ARG001
            return {"attestations": [attestation]}

        with mock.patch.object(kya_lib, "kya_list_attestations", side_effect=fake_list):
            result = kya_lib.kya_poll_attestation(
                agent_address=SAMPLE_ADDR,
                attestation_id="att_x",
                type_filter="twitter_claim",
                interval_sec=1,  # 不会真 sleep — 第一次 tick 就命中 active
                timeout_sec=2,
            )
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "active")

    def test_poll_attestation_times_out(self) -> None:
        # 永远 pending → 短超时返回 None；用 monkey patch time.sleep 避免真等
        def fake_list(*, agent_address, type_filter):  # noqa: ARG001
            return {"attestations": [{"id": "att_x", "status": "pending"}]}

        with mock.patch.object(
            kya_lib, "kya_list_attestations", side_effect=fake_list
        ), mock.patch.object(kya_lib.time, "sleep", lambda _s: None):
            result = kya_lib.kya_poll_attestation(
                agent_address=SAMPLE_ADDR,
                attestation_id="att_x",
                type_filter="twitter_claim",
                interval_sec=1,
                timeout_sec=0,  # 立刻超时
            )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
