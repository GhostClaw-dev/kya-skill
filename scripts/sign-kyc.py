#!/usr/bin/env python3
"""KYA KYC initiation — sign machine half, hand the user a KYA web link.

Revision 5 flow (agent → user handoff):

  1. Read agent + owner addresses (from awp-wallet or --agent / --owner).
  2. Sign EIP-712 KycInit.
  3. POST /kyc/sessions → receive {session_id, verification_url, …}.
  4. Build and print a KYA web URL:
        https://kya.link/verify/human/session#agent=…&session_id=…&didit_url=…
     The user opens it in their browser; the landing page embeds the Didit
     iframe and polls KYA for the session status.
  5. Exit. No long-running poll, no terminal-blocking prompts.

Why fragment (#): keeps the Didit verification URL out of KYA web's server
logs and out of the Referer header.

Headless / CI fallback (`--no-handoff`):
  Skip printing the KYA landing URL and just print the raw Didit URL + poll
  the session to terminal status, like the legacy behaviour.

Examples:
  # Default — print the KYA web URL and exit
  KYA_KYC_BASE=https://kya.link python3 sign-kyc.py

  # Different owner / agent
  python3 sign-kyc.py --owner 0xowner... --agent 0xagent...

  # Headless poll-and-wait (legacy)
  python3 sign-kyc.py --no-handoff --poll-timeout 300
"""

from __future__ import annotations

import argparse
import json
import sys

from kya_lib import (
    apply_api_base,
    base_parser,
    build_kyc_init_typed_data,
    build_web_landing_url,
    die,
    get_wallet_address,
    info,
    kyc_create_session,
    kyc_poll_session,
    kya_web_base,
    new_signature_nonce,
    now_unix_seconds,
    sign_typed_data,
    step,
    validate_address,
)


def _parse_args() -> argparse.Namespace:
    parser = base_parser(
        "Run the KYA KYC initiation flow and hand the user a KYA web link to "
        "complete the Didit verification."
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
        "--no-handoff",
        action="store_true",
        help="Headless mode: skip the KYA web link and poll the Didit session inline.",
    )
    parser.add_argument(
        "--poll-timeout",
        type=int,
        default=900,
        help="(Headless mode only) seconds to wait for terminal status (default 900).",
    )
    return parser.parse_args()


def _print_handoff_link(
    *,
    web_base: str,
    agent: str,
    owner: str,
    session_id: str,
    verification_url: str,
    session_status: str,
) -> None:
    fragment = {
        "agent": agent,
        "session_id": session_id,
        "didit_url": verification_url,
    }
    url = build_web_landing_url(
        web_base=web_base,
        path="/verify/human/session",
        fragment_params=fragment,
    )

    # See sign-claim.py: avoid box characters around the URL so terminal soft-wrap
    # can't trick a copy. JSON summary on stdout has the same value at handoff_url.
    print("", file=sys.stderr)
    print("HAND THIS URL TO THE USER (single line, do not break):", file=sys.stderr)
    print("HANDOFF_URL>>>", file=sys.stderr)
    print(url, file=sys.stderr)
    print("<<<HANDOFF_URL", file=sys.stderr)
    print(
        "The landing page will embed Didit and poll KYA. "
        "The same URL is also in the JSON summary on stdout under 'handoff_url'.",
        file=sys.stderr,
    )

    print(
        json.dumps(
            {
                "mode": "handoff",
                "agent_address": agent,
                "owner_address": owner,
                "session_id": session_id,
                "verification_url": verification_url,
                "status": session_status,
                "handoff_url": url,
            }
        )
    )


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

    if not args.no_handoff:
        _print_handoff_link(
            web_base=kya_web_base(args),
            agent=agent,
            owner=owner,
            session_id=session_id,
            verification_url=verification_url,
            session_status=session.get("status", "Pending"),
        )
        return

    # ── Headless / legacy path ────────────────────────
    print("\n────── KYC verification ──────", flush=True)
    print("Open this URL in any browser to complete Didit:", flush=True)
    print(verification_url, flush=True)
    print("──────────────────────────────", flush=True)

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
                    "mode": "headless",
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
                "mode": "headless",
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
