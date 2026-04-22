#!/usr/bin/env python3
"""KYA KYC initiation — sign + create session + open Didit + poll.

This handles only the *initiation* part of the KYC flow that absolutely needs
the agent's signature; the actual selfie/document verification still happens
inside Didit's hosted UI (because that's where the user's biometrics are
captured and validated). After this script prints the verification URL, the
user opens it in any browser, completes the Didit flow, and the script polls
until the session reaches a terminal status.

Examples:
  KYA_KYC_BASE=https://kya.link python3 sign-kyc.py
  python3 sign-kyc.py --owner 0xowner... --agent 0xagent... --no-poll
"""

from __future__ import annotations

import argparse
import json

from kya_lib import (
    apply_api_base,
    base_parser,
    build_kyc_init_typed_data,
    die,
    get_wallet_address,
    info,
    kyc_create_session,
    kyc_poll_session,
    new_signature_nonce,
    now_unix_seconds,
    sign_typed_data,
    step,
    validate_address,
)


def _parse_args() -> argparse.Namespace:
    parser = base_parser(
        "Run the KYA KYC initiation flow (sign KycInit + create Didit session + poll)."
    )
    parser.add_argument(
        "--agent",
        default="",
        help="Override the agent address (default: read from awp-wallet)",
    )
    parser.add_argument(
        "--owner",
        default="",
        help=(
            "Owner address that legally controls this agent. Defaults to the "
            "agent address (self-owned)."
        ),
    )
    parser.add_argument(
        "--no-poll",
        action="store_true",
        help="Skip polling; just print the Didit verification URL and exit.",
    )
    parser.add_argument(
        "--poll-timeout",
        type=int,
        default=900,
        help="Seconds to wait for the Didit session to reach a terminal status (default 900).",
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
    owner = validate_address(args.owner, "--owner") if args.owner else agent
    info("addresses resolved", agent=agent, owner=owner, chain_id=args.chain_id)

    timestamp = now_unix_seconds()
    nonce = new_signature_nonce()
    typed = build_kyc_init_typed_data(
        agent_address=agent,
        owner_address=owner,
        timestamp=timestamp,
        nonce=nonce,
        chain_id=args.chain_id,
    )
    step(
        "sign.request",
        action="kyc_init",
        agent_address=agent,
        owner_address=owner,
        timestamp=timestamp,
        nonce=nonce,
    )
    signature = sign_typed_data(typed, token=args.token)
    step("sign.ok", action="kyc_init", signature_prefix=signature[:10] + "…")

    session = kyc_create_session(
        agent_address=agent,
        owner_address=owner,
        signature=signature,
        timestamp=timestamp,
        nonce=nonce,
    )
    session_id = session.get("session_id") or session.get("id") or ""
    verification_url = session.get("verification_url") or ""
    if not session_id or not verification_url:
        die(f"unexpected create_session response: {session}")
    step("kyc.session_created", session_id=session_id)

    print("\n────── KYC verification ──────", flush=True)
    print(f"Open this URL in any browser to complete Didit:", flush=True)
    print(verification_url, flush=True)
    print("──────────────────────────────", flush=True)

    if args.no_poll:
        print(
            json.dumps(
                {
                    "agent_address": agent,
                    "owner_address": owner,
                    "session_id": session_id,
                    "verification_url": verification_url,
                    "status": session.get("status", "Pending"),
                }
            )
        )
        return

    final = kyc_poll_session(
        session_id, interval_sec=5, timeout_sec=args.poll_timeout
    )
    if not final:
        info(
            "poll timed out — Didit session still pending, check the agent page later",
            session_id=session_id,
        )
        print(
            json.dumps(
                {
                    "agent_address": agent,
                    "session_id": session_id,
                    "verification_url": verification_url,
                    "status": "Pending",
                    "timed_out": True,
                }
            )
        )
        return

    print(
        json.dumps(
            {
                "agent_address": agent,
                "owner_address": owner,
                "session_id": session_id,
                "status": final.get("status"),
                "attestation_id": final.get("attestation_id"),
            }
        )
    )


if __name__ == "__main__":
    main()
