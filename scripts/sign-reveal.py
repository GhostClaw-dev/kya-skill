#!/usr/bin/env python3
"""KYA Reveal — owner 用 agent 私钥换出未脱敏 metadata（email / kyc.country）。

为什么需要这个脚本：

  KYA 默认 GET /v1/agents/:address/attestations 会把 PII 字段脱敏：
    - email_claim.metadata.email → email_masked = "e***@gmail.com" + email_domain
    - kyc.metadata.country       → 整个字段删除
    - twitter_handle / telegram_channel_username 走 hash（仅批量/worknet 路径）

  这是有意为之——避免任何人输入 0x… 就能反查 owner 的全部联系方式 / 居住国家。
  但 owner 自己想看明文（比如校验邮箱是否绑对、回看 KYC 国家）也是合理诉求。
  解决办法：让 owner 用 agent 私钥签一个 EIP-712 Action(attestation_reveal)，
  KYA 验签后单次返回未脱敏 metadata（不上链、不写 DB，只消费 nonce 防重放）。

  本脚本封装这个流程：构造 typed-data → 走 awp-wallet 签 → POST reveal → 打印
  明文 metadata。和 sign-claim / sign-email 一样属于"agent 帮我搞定"档位。

Examples:
  # 默认拉所有类型的 attestation 明文（agent 自动从 awp-wallet 取）：
  python3 sign-reveal.py

  # 只看邮箱的明文：
  python3 sign-reveal.py --type email_claim

  # 只看 KYC（带 country）：
  python3 sign-reveal.py --type kyc

  # 显式指定 agent（默认从 awp-wallet 当前 profile 推）：
  python3 sign-reveal.py --agent 0xabc... --type email_claim
"""

from __future__ import annotations

import argparse
import json

from kya_lib import (
    apply_api_base,
    base_parser,
    build_action_typed_data,
    die,
    get_wallet_address,
    info,
    kya_reveal_attestations,
    new_signature_nonce,
    now_unix_seconds,
    sign_typed_data,
    step,
    validate_address,
)


# 与后端 querySchema 对齐：reveal 端点接受这几种 type filter。
_KNOWN_TYPES = (
    "twitter_claim",
    "telegram_claim",
    "email_claim",
    "staking",
    "kyc",
)


def _parse_args() -> argparse.Namespace:
    parser = base_parser(
        "Reveal unredacted metadata for an agent's attestations by signing a "
        "single EIP-712 Action(attestation_reveal) with the agent EOA."
    )
    parser.add_argument(
        "--agent",
        default="",
        help="Override the agent address (default: read from awp-wallet)",
    )
    parser.add_argument(
        "--type",
        dest="type_filter",
        default="",
        choices=("",) + _KNOWN_TYPES,
        help=(
            "Narrow the response to one attestation type. Empty = all types. "
            "Most commonly: 'email_claim' to see the full email, or 'kyc' to "
            "see the country code."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    apply_api_base(args)

    agent = (
        validate_address(args.agent, "--agent")
        if args.agent
        else get_wallet_address(args.token or None)
    )
    type_filter = args.type_filter or None
    info("agent resolved", agent=agent, chain_id=args.chain_id, type=type_filter or "all")

    # ── 签 Action(attestation_reveal) ────────────────────────────
    timestamp = now_unix_seconds()
    nonce = new_signature_nonce()
    typed = build_action_typed_data(
        action="attestation_reveal",
        agent_address=agent,
        timestamp=timestamp,
        nonce=nonce,
        chain_id=args.chain_id,
    )
    step(
        "sign.request",
        action="attestation_reveal",
        agent=agent,
        timestamp=timestamp,
        nonce=nonce,
    )
    signature = sign_typed_data(typed, token=args.token or None)
    step("sign.ok", action="attestation_reveal", signature_prefix=signature[:10] + "…")

    # ── POST /v1/agents/:address/attestations/reveal ─────────────
    payload = kya_reveal_attestations(
        agent_address=agent,
        signature=signature,
        timestamp=timestamp,
        nonce=nonce,
        type_filter=type_filter,
    )

    attestations = payload.get("attestations") if isinstance(payload, dict) else None
    if not isinstance(attestations, list):
        die(f"unexpected reveal response shape: {payload}")

    step(
        "reveal.ok",
        agent=agent,
        type=type_filter or "all",
        count=len(attestations),
    )

    # 主输出：未脱敏 metadata 的 JSON。包装成与 GET 同形（subject + attestations）
    # 方便下游 jq / 其它脚本处理；细节字段（id / status / type / metadata）原样透传。
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
