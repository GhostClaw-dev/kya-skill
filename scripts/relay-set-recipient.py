#!/usr/bin/env python3
"""AWPRegistry.setRecipient — gasless relay flow for KYA matchmaking.

KYA 的 economic verification 需要 agent 把 AWP reward recipient 指向
KYA 派生的 deposit address。整个流程默认 agent 钱包没有 ETH,所以这里
完全走 AWP relayer 代付 gas:

  1. 从 awp-wallet 读 agent EOA(或 --agent 覆盖)。
  2. (可选)从 KYA 后端拿 deposit address —— 如果用户没提供 --recipient,
     脚本就 GET /v1/agents/:address/deposit-address?worknet_id=...。
  3. 读 AWPRegistry.nonces(agent),组 SetRecipient typed-data,1 小时 deadline。
  4. awp-wallet sign-typed-data → 0x...130hex 签名(锁了自动 unlock)。
  5. POST <AWP_RELAY_BASE>/api/relay/set-recipient,relayer 负责 gas + 上链。
  6. (可选)轮询 /api/relay/status/:txHash 至 confirmed/failed。

只有 deposit-address 查询走 KYA_API_BASE,relayer 调用走 AWP_RELAY_BASE,
互不依赖。

Examples::

  # 完整路径:让 skill 自己问 KYA 拿 deposit
  KYA_API_BASE=https://kya.link \
    python3 relay-set-recipient.py --worknet 845300000012

  # 已经知道 recipient(deposit address):
  python3 relay-set-recipient.py --recipient 0xdeposit... --no-poll
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from kya_lib import (
    DEFAULT_KYA_WORKNET_ID,
    SIG_RE,
    _http_request,  # 内部:复用 kya_lib 的 urllib 包装
    apply_api_base,
    awp_get_registry_nonce,
    base_parser,
    build_awp_set_recipient_typed_data,
    die,
    get_wallet_address,
    info,
    now_unix_seconds,
    relay_set_recipient,
    sign_typed_data,
    step,
    validate_address,
    wait_relay_confirmation,
    _kya_base,
)


def _fetch_deposit_address(agent: str, worknet_id: str) -> str:
    """GET /v1/agents/:address/deposit-address?worknet_id=...

    只在用户没显式给 --recipient 时调用;失败友好 die,不留半成品状态。
    """
    base = _kya_base()
    qs = f"?worknet_id={worknet_id}" if worknet_id else ""
    status, payload = _http_request(
        "GET", f"{base}/v1/agents/{agent}/deposit-address{qs}"
    )
    if status >= 400 or not isinstance(payload, dict):
        die(
            f"KYA deposit-address lookup failed (status={status}). "
            "Either the agent has not been onboarded yet, or pass --recipient explicitly."
        )
    deposit = payload.get("deposit_address")
    if not isinstance(deposit, str):
        die(f"KYA returned no deposit_address: {payload!r}")
    return deposit


def main(argv: Optional[list[str]] = None) -> int:
    parser = base_parser(__doc__ or "")
    parser.add_argument("--agent", help="Agent EOA. Default: read from awp-wallet receive.")
    parser.add_argument("--recipient", help="Reward recipient. Default: KYA deposit address from API.")
    parser.add_argument(
        "--worknet",
        default=DEFAULT_KYA_WORKNET_ID,
        help=f"Worknet ID for KYA deposit lookup (default: {DEFAULT_KYA_WORKNET_ID}).",
    )
    parser.add_argument(
        "--deadline-seconds",
        type=int,
        default=3600,
        help="Signature deadline relative to now (default: 3600s = 1h).",
    )
    parser.add_argument(
        "--no-poll",
        action="store_true",
        help="Do not poll relay status after submission.",
    )
    args = parser.parse_args(argv)
    apply_api_base(args)

    agent = args.agent or get_wallet_address(token=args.token)
    agent = validate_address(agent, "agent")
    step("agent.resolved", agent=agent)

    if args.recipient:
        recipient = validate_address(args.recipient, "recipient")
    else:
        recipient = validate_address(
            _fetch_deposit_address(agent, args.worknet), "recipient"
        )
    step("recipient.resolved", recipient=recipient, source=("flag" if args.recipient else "kya"))

    nonce = awp_get_registry_nonce(agent)
    deadline = now_unix_seconds() + max(60, int(args.deadline_seconds))

    typed = build_awp_set_recipient_typed_data(
        user_address=agent,
        recipient_address=recipient,
        nonce=nonce,
        deadline=deadline,
        chain_id=args.chain_id,
    )
    step("eip712.built", primary_type=typed["primaryType"], deadline=deadline)

    signature = sign_typed_data(typed, token=args.token)
    if not SIG_RE.match(signature):
        die(f"awp-wallet returned malformed signature: {signature!r}")
    step("eip712.signed")

    res = relay_set_recipient(
        user_address=agent,
        recipient_address=recipient,
        deadline=deadline,
        signature=signature,
        chain_id=args.chain_id,
    )
    tx_hash = res.get("txHash") or res.get("tx_hash")
    step("relay.submitted", tx_hash=tx_hash, status=res.get("status"))

    final_status: Optional[dict] = None
    if tx_hash and not args.no_poll:
        final_status = wait_relay_confirmation(tx_hash)

    info("relay set-recipient done", tx_hash=tx_hash)
    print(
        json.dumps(
            {
                "agent_address": agent,
                "recipient": recipient,
                "tx_hash": tx_hash,
                "relay_response": res,
                "final_status": final_status,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
