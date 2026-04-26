#!/usr/bin/env python3
"""AWP gasless stake — lock AWP into veAWP via the AWP relayer.

Provider 在 KYA 上线前必须先把 AWP 锁进 veAWP 拿"未分配的可用容量"。
默认 provider 钱包没 ETH,所以走 relayer permit:

  1. awp-wallet 读 provider EOA(或 --provider 覆盖)。
  2. POST /api/relay/stake/prepare,得到 ERC20Permit typed-data + submitTo 描述。
  3. 校验返回的 typed-data:owner == provider 且 value == amountWei。
  4. awp-wallet sign-typed-data 对 prepare 给的 typed-data 签名。
  5. 把签名 POST 到 submitTo.url(必须落在 AWP_RELAY_BASE 域内)。
  6. (可选)轮询 /api/relay/status/:txHash 至 confirmed/failed。

Example::

  python3 relay-stake.py --amount 1000 --lock-days 90
"""

from __future__ import annotations

import argparse
import decimal
import json
import sys
from typing import Optional

from kya_lib import (
    SIG_RE,
    base_parser,
    die,
    get_wallet_address,
    info,
    relay_stake_prepare,
    relay_stake_submit,
    sign_typed_data,
    step,
    validate_address,
    wait_relay_confirmation,
)


def _to_wei(amount: str) -> int:
    """把 '1000' / '1000.5' 之类的人类可读 AWP 数量转成 18 位精度的 wei。

    用 decimal 而不是 float,避免 0.1 这种舍入误差。
    """
    try:
        d = decimal.Decimal(amount.strip())
    except (decimal.InvalidOperation, AttributeError):
        die(f"--amount must be a positive decimal (got: {amount!r})")
        return 0  # unreachable
    if d <= 0:
        die(f"--amount must be > 0 (got: {amount!r})")
    wei = (d * decimal.Decimal(10) ** 18).to_integral_value(rounding=decimal.ROUND_DOWN)
    return int(wei)


def _verify_prepare(prepared: dict, *, expected_owner: str, expected_amount_wei: int) -> dict:
    """校验 prepare 返回:owner == provider 且 value == amount,避免被诱导改 stake 目标。"""
    if not isinstance(prepared, dict):
        die("relay stake/prepare returned non-object payload")
    typed = prepared.get("typedData")
    submit_to = prepared.get("submitTo")
    if not isinstance(typed, dict) or not isinstance(submit_to, dict):
        die("relay stake/prepare missing typedData or submitTo")
    msg = typed.get("message")
    if not isinstance(msg, dict):
        die("relay stake/prepare typedData has no message")
    owner = str(msg.get("owner") or "").lower()
    if owner != expected_owner.lower():
        die(f"relay returned typedData for wrong owner: {owner} != {expected_owner.lower()}")
    value_str = str(msg.get("value") or "")
    if value_str != str(expected_amount_wei):
        die(f"relay returned typedData with wrong value: {value_str} != {expected_amount_wei}")
    return prepared


def _stringify_message(msg: dict) -> dict:
    """awp-wallet sign-typed-data 期望 uint256 字段是字符串。

    relayer 返回的 message 可能含 number 或字符串混排,这里统一成字符串。
    """
    out: dict = {}
    for k, v in msg.items():
        if isinstance(v, bool) or v is None:
            out[k] = v
        elif isinstance(v, int):
            out[k] = str(v)
        else:
            out[k] = v
    return out


def main(argv: Optional[list[str]] = None) -> int:
    parser = base_parser(__doc__ or "")
    parser.add_argument("--provider", help="Provider EOA. Default: awp-wallet receive.")
    parser.add_argument(
        "--amount",
        required=True,
        help="AWP amount to lock (human-readable, 18 decimals). Example: 1000 or 1000.5",
    )
    parser.add_argument(
        "--lock-days",
        type=int,
        default=90,
        help="veAWP lock duration in days (default: 90).",
    )
    parser.add_argument(
        "--no-poll",
        action="store_true",
        help="Do not poll relay status after submission.",
    )
    args = parser.parse_args(argv)

    provider = args.provider or get_wallet_address(token=args.token)
    provider = validate_address(provider, "provider")
    lock_seconds = max(1, int(args.lock_days)) * 86400
    amount_wei = _to_wei(args.amount)
    step("stake.prepare", provider=provider, amount_wei=amount_wei, lock_seconds=lock_seconds)

    prepared = relay_stake_prepare(
        user_address=provider,
        amount_wei=amount_wei,
        lock_seconds=lock_seconds,
        chain_id=args.chain_id,
    )
    _verify_prepare(prepared, expected_owner=provider, expected_amount_wei=amount_wei)
    typed = prepared["typedData"]
    typed_for_sign = {
        "domain": typed["domain"],
        "types": typed["types"],
        "primaryType": typed["primaryType"],
        "message": _stringify_message(typed["message"]),
    }
    step("eip712.built", primary_type=typed["primaryType"])

    signature = sign_typed_data(typed_for_sign, token=args.token)
    if not SIG_RE.match(signature):
        die(f"awp-wallet returned malformed signature: {signature!r}")
    step("eip712.signed")

    res = relay_stake_submit(prepared["submitTo"], signature)
    tx_hash = res.get("txHash") or res.get("tx_hash")
    step("relay.submitted", tx_hash=tx_hash, status=res.get("status"))

    final_status: Optional[dict] = None
    if tx_hash and not args.no_poll:
        final_status = wait_relay_confirmation(tx_hash)

    info("relay stake done", tx_hash=tx_hash)
    print(
        json.dumps(
            {
                "provider_address": provider,
                "amount_wei": str(amount_wei),
                "lock_seconds": lock_seconds,
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
