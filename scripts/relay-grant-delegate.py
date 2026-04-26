#!/usr/bin/env python3
"""AWPRegistry.grantDelegate(KyaAllocatorProxy) — gasless relay for KYA providers.

Provider 上线流程的关键一步:授权 KyaAllocatorProxy 代为调用 allocate。
没有 AWP 被转移,只是把"管理 allocation 的权利"挂到 KYA 的 Proxy。
项目默认 provider 钱包没有 ETH,所以走 AWP relayer 代付 gas:

  1. awp-wallet 读 provider EOA(或 --provider 覆盖)。
  2. 读 AWPRegistry.nonces(provider) → 组 GrantDelegate typed-data,1 小时 deadline。
  3. awp-wallet sign-typed-data 拿签名(必要时自动 unlock)。
  4. POST <AWP_RELAY_BASE>/api/relay/grant-delegate。
  5. (可选)轮询 relay 状态至 confirmed/failed。

delegate 默认就是 KYA_ALLOCATOR_PROXY_ADDRESS,几乎不需要传 --delegate;
仅 KYA 替换 Proxy 时才有人会用到 --delegate 这个口子。

Example::

  python3 relay-grant-delegate.py
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from kya_lib import (
    KYA_ALLOCATOR_PROXY_ADDRESS,
    SIG_RE,
    awp_get_registry_nonce,
    base_parser,
    build_awp_grant_delegate_typed_data,
    die,
    get_wallet_address,
    info,
    now_unix_seconds,
    relay_grant_delegate,
    sign_typed_data,
    step,
    validate_address,
    wait_relay_confirmation,
)


def main(argv: Optional[list[str]] = None) -> int:
    parser = base_parser(__doc__ or "")
    parser.add_argument("--provider", help="Provider EOA. Default: awp-wallet receive.")
    parser.add_argument(
        "--delegate",
        default=KYA_ALLOCATOR_PROXY_ADDRESS,
        help=f"Delegate address (default: KyaAllocatorProxy {KYA_ALLOCATOR_PROXY_ADDRESS}).",
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

    provider = args.provider or get_wallet_address(token=args.token)
    provider = validate_address(provider, "provider")
    delegate = validate_address(args.delegate, "delegate")
    step("provider.resolved", provider=provider, delegate=delegate)

    nonce = awp_get_registry_nonce(provider)
    deadline = now_unix_seconds() + max(60, int(args.deadline_seconds))

    typed = build_awp_grant_delegate_typed_data(
        user_address=provider,
        delegate_address=delegate,
        nonce=nonce,
        deadline=deadline,
        chain_id=args.chain_id,
    )
    step("eip712.built", primary_type=typed["primaryType"], deadline=deadline)

    signature = sign_typed_data(typed, token=args.token)
    if not SIG_RE.match(signature):
        die(f"awp-wallet returned malformed signature: {signature!r}")
    step("eip712.signed")

    res = relay_grant_delegate(
        user_address=provider,
        delegate_address=delegate,
        deadline=deadline,
        signature=signature,
        chain_id=args.chain_id,
    )
    tx_hash = res.get("txHash") or res.get("tx_hash")
    step("relay.submitted", tx_hash=tx_hash, status=res.get("status"))

    final_status: Optional[dict] = None
    if tx_hash and not args.no_poll:
        final_status = wait_relay_confirmation(tx_hash)

    info("relay grant-delegate done", tx_hash=tx_hash)
    print(
        json.dumps(
            {
                "provider_address": provider,
                "delegate": delegate,
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
