#!/usr/bin/env python3
"""sign-action.py CLI tests — 不依赖真实 awp-wallet。

测试通过临时 `PATH` 注入一个 fake `awp-wallet` 脚本，让 sign-action.py 的
子进程调用能在 CI / 本地零依赖跑起来。验证：

  - twitter_prepare / twitter_claim 两个 Action 的 typed-data 与 web 契约一致
  - kyc_init 需要 --owner，缺失时拒绝
  - nonce / timestamp / agent 严格校验
  - --action 不在白名单时由 argparse 直接拦截
  - 最终 stdout 只有 `0x...130hex`，便于 shell 管道

Run: python3 scripts/test_sign_action.py
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parent / "sign-action.py"
FAKE_SIG = "0x" + "ab" * 65  # 130 hex chars


def _write_fake_wallet(tmpdir: Path, dump_typed_to: Path) -> Path:
    """在 tmpdir 里造一个名叫 awp-wallet 的可执行脚本；受 PATH 前缀劫持后会被 subprocess 命中。

    行为：若参数是 `sign-typed-data --data <json>`，把 JSON dump 到 dump_typed_to 后
    打印一个合法签名 JSON；若是 `receive`，打印一个占位 eoaAddress。

    实现：两个平台都用 Python 本体写一个小脚本；Windows 走 `.cmd` 让 PATH 正常命中，
    *nix 走 shebang + chmod +x。同样的签名逻辑，避免 .cmd / .sh 双份实现。
    """
    interpreter = sys.executable
    # 被 fake 脚本内联调用的"逻辑"部分；dump_typed_to / FAKE_SIG 通过 f-string 硬编码，
    # 避免跨进程传参的 quoting 陷阱。
    body = textwrap.dedent(
        f"""\
        import json, sys
        from pathlib import Path
        args = sys.argv[1:]
        if args and args[0] == "sign-typed-data":
            if "--data" in args:
                idx = args.index("--data") + 1
                Path(r"{dump_typed_to}").write_text(args[idx], encoding="utf-8")
            print(json.dumps({{"signature": "{FAKE_SIG}"}}))
        elif args and args[0] == "receive":
            print(json.dumps({{"eoaAddress": "0x{'11' * 20}"}}))
        else:
            sys.exit(2)
        """
    )

    if sys.platform.startswith("win"):
        impl = tmpdir / "_awp_impl.py"
        impl.write_text(body, encoding="utf-8")
        fake = tmpdir / "awp-wallet.cmd"
        fake.write_text(
            f'@echo off\r\n"{interpreter}" "{impl}" %*\r\n',
            encoding="utf-8",
        )
    else:
        fake = tmpdir / "awp-wallet"
        fake.write_text(f"#!{interpreter}\n{body}", encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return fake


def _run(env_path: str, tmp: Path, extra_args: list[str]) -> subprocess.CompletedProcess:
    env = {**os.environ, "PATH": env_path, "KYA_CHAIN_ID": "8453"}
    return subprocess.run(
        [sys.executable, str(SCRIPT), *extra_args],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp,
        timeout=20,
    )


AGENT = "0x" + "aa" * 20
OWNER = "0x" + "bb" * 20
NONCE = "bfc412331f93ca46e9ab9eae9986d165"  # 32 hex
TS = "1776848920"


class SignActionCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self._make_tmp())
        self.dump = self.tmp / "typed.json"
        _write_fake_wallet(self.tmp, self.dump)
        # 优先命中 fake awp-wallet：把 tmp 放在 PATH 最前
        self.env_path = os.pathsep.join([str(self.tmp), os.environ.get("PATH", "")])

    def _make_tmp(self) -> str:
        import tempfile

        return tempfile.mkdtemp(prefix="sign-action-")

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_twitter_prepare_builds_correct_typed_data(self) -> None:
        """twitter_prepare 的 typed-data 必须与 web/lib/eip712.ts 完全一致的字段顺序与类型。"""
        r = _run(
            self.env_path,
            self.tmp,
            [
                "--action", "twitter_prepare",
                "--agent", AGENT,
                "--timestamp", TS,
                "--nonce", NONCE,
            ],
        )
        self.assertEqual(r.returncode, 0, msg=f"stderr={r.stderr}")
        self.assertEqual(r.stdout.strip(), FAKE_SIG)
        self.assertTrue(self.dump.is_file(), "fake awp-wallet should have dumped typed-data")
        td = json.loads(self.dump.read_text(encoding="utf-8"))
        self.assertEqual(td["primaryType"], "Action")
        self.assertEqual(td["domain"], {"name": "KYA", "version": "1", "chainId": 8453})
        # 字段顺序必须是 action / agent_address / timestamp / nonce —— 改顺序后端会 SIGNATURE_INVALID
        self.assertEqual(
            [f["name"] for f in td["types"]["Action"]],
            ["action", "agent_address", "timestamp", "nonce"],
        )
        self.assertEqual(td["message"]["action"], "twitter_prepare")
        self.assertEqual(td["message"]["agent_address"], AGENT)
        self.assertEqual(td["message"]["timestamp"], TS)
        self.assertEqual(td["message"]["nonce"], NONCE)

    def test_kyc_init_requires_owner(self) -> None:
        r = _run(
            self.env_path,
            self.tmp,
            [
                "--action", "kyc_init",
                "--agent", AGENT,
                "--timestamp", TS,
                "--nonce", NONCE,
            ],
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--owner is required", r.stderr)

    def test_kyc_init_with_owner_builds_typed_data(self) -> None:
        r = _run(
            self.env_path,
            self.tmp,
            [
                "--action", "kyc_init",
                "--agent", AGENT,
                "--owner", OWNER,
                "--timestamp", TS,
                "--nonce", NONCE,
            ],
        )
        self.assertEqual(r.returncode, 0, msg=f"stderr={r.stderr}")
        td = json.loads(self.dump.read_text(encoding="utf-8"))
        self.assertEqual(td["primaryType"], "KycInit")
        self.assertEqual(
            [f["name"] for f in td["types"]["KycInit"]],
            ["action", "agent_address", "owner_address", "timestamp", "nonce"],
        )
        self.assertEqual(td["message"]["owner_address"], OWNER)

    def test_unknown_action_rejected_by_argparse(self) -> None:
        r = _run(
            self.env_path,
            self.tmp,
            [
                "--action", "delete_everything",
                "--agent", AGENT,
                "--timestamp", TS,
                "--nonce", NONCE,
            ],
        )
        self.assertNotEqual(r.returncode, 0)

    def test_bad_agent_address_rejected(self) -> None:
        r = _run(
            self.env_path,
            self.tmp,
            [
                "--action", "twitter_prepare",
                "--agent", "not-an-address",
                "--timestamp", TS,
                "--nonce", NONCE,
            ],
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("agent must look like 0x", r.stderr)

    def test_bad_timestamp_rejected(self) -> None:
        r = _run(
            self.env_path,
            self.tmp,
            [
                "--action", "twitter_prepare",
                "--agent", AGENT,
                "--timestamp", "not-a-number",
                "--nonce", NONCE,
            ],
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("timestamp", r.stderr)

    def test_bad_nonce_rejected(self) -> None:
        r = _run(
            self.env_path,
            self.tmp,
            [
                "--action", "twitter_prepare",
                "--agent", AGENT,
                "--timestamp", TS,
                "--nonce", "zzzzz",  # 非 hex
            ],
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("nonce", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
