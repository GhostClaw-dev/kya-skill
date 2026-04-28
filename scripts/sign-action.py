#!/usr/bin/env python3
"""KYA 单步签名器 —— 按 wizard 已经生成好的 nonce/timestamp 签一个 KYA action。

与 `sign-claim.py` / `sign-kyc.py` 不同，本脚本 **不** 驱动端到端流程，也不打 HTTP；
它只负责"按 KYA 契约构造 typed-data → 调 awp-wallet 签 → 打印 0x 签名"。

使用场景：KYA web 的 Manual Sign 弹窗发现当前 signer 是 `manual` 模式时，会把
已生成的 `timestamp` / `nonce` / `action` / `agent` 打包进 prompt；用户把 prompt
粘到 IDE 聊天，agent 找到本脚本执行，返回签名粘回弹窗即可。

这样用户**不再需要复制 typed-data JSON**，prompt 里就带了所有重建 typed-data 所需的字段，
而 typed-data 的具体 shape 由 skill 从 `kya_lib` 重建，契约与后端 `api/src/crypto/eip712.ts`
以及 web `web/lib/eip712.ts` 三方对齐，由 `test_kya_lib.py` + `test_sign_action.py` 共同守护。

Examples:
  python3 sign-action.py \\
    --action twitter_prepare \\
    --agent 0xabc... \\
    --timestamp 1731000000 \\
    --nonce bfc412331f93ca46e9ab9eae9986d165

  python3 sign-action.py \\
    --action kyc_init \\
    --agent 0xabc... \\
    --owner 0xdef... \\
    --timestamp 1731000000 \\
    --nonce bfc412331f93ca46e9ab9eae9986d165
"""

from __future__ import annotations

import argparse

from kya_lib import (
    SIG_RE,
    base_parser,
    build_action_typed_data,
    build_kyc_init_typed_data,
    die,
    info,
    sign_typed_data,
    step,
    validate_address,
)


KNOWN_ACTIONS = (
    "twitter_prepare",
    "twitter_claim",
    "telegram_prepare",
    "telegram_claim",
    "email_prepare",
    "email_confirm",
    "kyc_init",
)


def _parse_timestamp(raw: str) -> int:
    try:
        ts = int(raw)
    except (TypeError, ValueError):
        die(f"--timestamp must be an integer unix seconds (got: {raw!r})")
    if ts <= 0:
        die(f"--timestamp must be positive (got: {ts})")
    return ts


def _parse_nonce(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        die("--nonce is required (16-byte hex, same value the wizard displayed)")
    # nonce 长度限制与 web/lib/eip712.ts `newSignatureNonce()` 一致：32 hex chars。
    # 这里稍微放宽（8-128 hex），兼容未来可能的调整，但拦截明显乱输入。
    if not (8 <= len(s) <= 128) or any(c not in "0123456789abcdefABCDEF" for c in s):
        die(f"--nonce must be hex (8-128 chars); got length={len(s)}: {s!r}")
    return s


def main() -> None:
    parser: argparse.ArgumentParser = base_parser(
        "Sign one KYA EIP-712 action (twitter_prepare / twitter_claim / kyc_init) "
        "with nonce + timestamp provided by the wizard."
    )
    parser.add_argument(
        "--action",
        required=True,
        choices=KNOWN_ACTIONS,
        help="KYA action to sign: twitter_prepare | twitter_claim | kyc_init",
    )
    parser.add_argument(
        "--agent",
        required=True,
        help="Agent EOA address (0x...40hex) — must match the wizard's signer",
    )
    parser.add_argument(
        "--owner",
        default="",
        help="Owner EOA address (only required for --action kyc_init)",
    )
    parser.add_argument(
        "--timestamp",
        required=True,
        help="Unix seconds, as shown by the wizard",
    )
    parser.add_argument(
        "--nonce",
        required=True,
        help="16-byte hex nonce shown by the wizard",
    )
    parser.add_argument(
        "--write-file",
        default="",
        help="Also write the signature to this file in addition to stdout",
    )
    args = parser.parse_args()

    agent = validate_address(args.agent, "agent")
    timestamp = _parse_timestamp(args.timestamp)
    nonce = _parse_nonce(args.nonce)

    if args.action == "kyc_init":
        if not args.owner:
            die("--owner is required when --action kyc_init")
        owner = validate_address(args.owner, "owner")
        typed = build_kyc_init_typed_data(
            agent_address=agent,
            owner_address=owner,
            timestamp=timestamp,
            nonce=nonce,
            chain_id=args.chain_id,
        )
    else:
        if args.owner:
            info("warning: --owner is ignored for action", action=args.action)
        typed = build_action_typed_data(
            action=args.action,
            agent_address=agent,
            timestamp=timestamp,
            nonce=nonce,
            chain_id=args.chain_id,
        )

    step(
        "sign.request",
        action=args.action,
        agent=agent,
        chain_id=args.chain_id,
        timestamp=timestamp,
        nonce=nonce,
    )

    signature = sign_typed_data(typed, token=args.token or None)
    if not SIG_RE.match(signature):
        die(f"awp-wallet returned malformed signature: {signature!r}")

    if args.write_file:
        from pathlib import Path

        Path(args.write_file).write_text(signature, encoding="utf-8")
        info("signature written", path=args.write_file)

    print(signature)


if __name__ == "__main__":
    main()
