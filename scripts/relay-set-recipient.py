#!/usr/bin/env python3
"""AWPRegistry.setRecipient — gasless relay flow + KYA delegated-staking request.

Two-stage flow for KYA's economic / delegated-staking pipeline:

  Stage 1 — point AWP rewards at the KYA deposit address (gasless via AWP relayer):
    1. Read agent EOA from awp-wallet (or --agent override).
    2. (optional) GET /v1/agents/:address/deposit-address to pick the recipient.
    3. Read AWPRegistry.nonces(agent), build SetRecipient typed-data, sign.
    4. POST signature to AWP_RELAY_BASE/api/relay/set-recipient.
    5. Poll relay status until confirmed (skippable with --no-poll).

  Stage 2 — tell KYA "I want N AWP backing my agent" (only with --amount):
    6. Sign KYA Action(delegated_staking_request).
    7. POST /v1/services/staking/request — KYA queues the matching worker.
       Server gates on social|human attestation present (403 if neither).

Stage 2 is the new owner-driven step required after the 2026-04-27 product
refresh: the agent owner declares a target stake amount in the web UI, the
prompt embeds --amount, and this script forwards it to KYA after stage 1
lands. Without --amount stage 2 is skipped (back-compat).

Examples::

  # Full flow with amount (matches what the /services Delegated Staking
  # card generates as the prompt):
  KYA_API_BASE=https://kya.link \
    python3 relay-set-recipient.py --worknet 845300000012 --amount 1000

  # Stage 1 only (legacy):
  KYA_API_BASE=https://kya.link \
    python3 relay-set-recipient.py --worknet 845300000012

  # Already know the recipient address:
  python3 relay-set-recipient.py --recipient 0xdeposit... --no-poll
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Optional

from kya_lib import (
    DEFAULT_KYA_WORKNET_ID,
    SIG_RE,
    _http_request,
    _kya_base,
    apply_api_base,
    awp_get_registry_nonce,
    base_parser,
    build_action_typed_data,
    build_awp_set_recipient_typed_data,
    die,
    get_wallet_address,
    info,
    kya_request_delegated_staking,
    new_signature_nonce,
    now_unix_seconds,
    relay_set_recipient,
    sign_typed_data,
    step,
    validate_address,
    wait_relay_confirmation,
)


_AMOUNT_RE = re.compile(r"^\d+(?:\.\d{1,18})?$")


def _validate_amount(raw: str) -> str:
    """Owner-supplied AWP amount as decimal string. Server multiplies by 1e18."""
    s = raw.strip()
    if not _AMOUNT_RE.match(s):
        die(f"--amount must be a positive decimal, got {raw!r}")
    if float(s) <= 0:
        die(f"--amount must be > 0, got {raw!r}")
    return s


def _fetch_deposit_address(agent: str, worknet_id: str) -> str:
    """GET /v1/agents/:address/deposit-address?worknet_id=...

    Only invoked when --recipient is not provided; surfaces a clean error if
    the agent hasn't been onboarded so we don't leave half a setRecipient
    floating with no destination.
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


def _post_delegated_staking_request(
    *, agent: str, amount_awp: str, worknet_id: Optional[str], chain_id: int, token: Optional[str]
) -> dict:
    """Stage 2: signed KYA Action POST that tells matching worker how much.

    Returns the parsed response dict so the caller can print request_id.
    Errors die — caller should only invoke this after stage 1 succeeded so a
    failure here doesn't leave a totally unrecoverable state (recipient is
    set; owner can re-run with --amount once the gating issue clears).
    """
    timestamp = now_unix_seconds()
    msg_nonce = new_signature_nonce()
    typed = build_action_typed_data(
        action="delegated_staking_request",
        agent_address=agent,
        timestamp=timestamp,
        nonce=msg_nonce,
        chain_id=chain_id,
    )
    step(
        "kya.staking_request.signing",
        agent=agent,
        amount_awp=amount_awp,
        worknet_id=worknet_id,
    )
    signature = sign_typed_data(typed, token=token)
    if not SIG_RE.match(signature):
        die(f"awp-wallet returned malformed signature: {signature!r}")
    return kya_request_delegated_staking(
        agent_address=agent,
        amount_awp=amount_awp,
        worknet_id=worknet_id,
        signature=signature,
        timestamp=timestamp,
        nonce=msg_nonce,
    )


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
        "--amount",
        default="",
        help=(
            "AWP amount the owner wants matched on this agent (decimal, e.g. "
            "'1000'). When set, the script POSTs a delegated_staking_request "
            "to KYA after stage 1 confirms. Without --amount stage 2 is skipped."
        ),
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

    amount_awp: Optional[str] = None
    if args.amount:
        amount_awp = _validate_amount(args.amount)
        step("amount.resolved", amount_awp=amount_awp)

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

    # Stage 2: only when owner declared an amount. Stage 1 must be confirmed
    # (or polling skipped on user request) before we ask KYA to enqueue —
    # otherwise the matching worker could pick up a request whose recipient
    # tx is still pending or reverted.
    staking_request: Optional[dict] = None
    if amount_awp:
        if final_status is not None and final_status.get("status") not in (None, "confirmed"):
            die(
                "Skipping delegated-staking request because the relay tx didn't "
                f"confirm cleanly (status={final_status.get('status')!r})."
            )
        staking_request = _post_delegated_staking_request(
            agent=agent,
            amount_awp=amount_awp,
            worknet_id=args.worknet,
            chain_id=args.chain_id,
            token=args.token,
        )
        step(
            "kya.staking_request.queued",
            request_id=staking_request.get("request_id"),
            status=staking_request.get("status"),
        )

    print(
        json.dumps(
            {
                "agent_address": agent,
                "recipient": recipient,
                "tx_hash": tx_hash,
                "relay_response": res,
                "final_status": final_status,
                "amount_awp": amount_awp,
                "staking_request": staking_request,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
