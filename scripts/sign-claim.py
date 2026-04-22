#!/usr/bin/env python3
"""KYA Twitter Claim — sign + prepare + (post tweet) + claim + poll, end-to-end.

This is the "lean B+" flow: the user copies a magic link from KYA web, runs this
script in their IDE, and it handles every step that previously required manual
JSON / signature copy-paste:

  1. Read agent address from awp-wallet (or --agent override).
  2. POST /v1/attestations/twitter/prepare — sign EIP-712 Action(twitter_prepare),
     receive `{ nonce, claim_text, expires_at }`.
  3. Print the claim_text and an X intent URL; pause for the user to publish the
     tweet and paste the tweet URL back (unless --tweet-url is provided).
  4. POST /v1/attestations/twitter/claim — sign EIP-712 Action(twitter_claim),
     submit the tweet URL.
  5. Poll GET /v1/agents/:address/attestations until the new attestation goes
     active or revoked.

Examples:
  # Interactive: tells user to publish tweet and paste URL
  KYA_API_BASE=https://kya.link python3 sign-claim.py

  # Headless (already published the tweet):
  python3 sign-claim.py --tweet-url https://x.com/me/status/123 --agent 0xabc...

  # Custom chain id and base URL:
  python3 sign-claim.py --api-base http://localhost:8080 --chain-id 31337
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse

from kya_lib import (
    apply_api_base,
    base_parser,
    build_action_typed_data,
    die,
    get_wallet_address,
    info,
    kya_claim_twitter,
    kya_poll_attestation,
    kya_prepare_twitter,
    new_signature_nonce,
    now_unix_seconds,
    sign_typed_data,
    step,
    validate_address,
    validate_tweet_url,
)


def _parse_args() -> argparse.Namespace:
    parser = base_parser(
        "Run the KYA Twitter claim flow end-to-end (sign + prepare + claim + poll)."
    )
    parser.add_argument(
        "--agent",
        default="",
        help="Override the agent address (default: read from awp-wallet)",
    )
    parser.add_argument(
        "--tweet-url",
        default="",
        help=(
            "Pre-published tweet URL. If omitted, the script prints the claim text "
            "and waits for the user to publish then paste the URL on stdin."
        ),
    )
    parser.add_argument(
        "--no-poll",
        action="store_true",
        help="Skip the post-claim attestation polling (returns immediately).",
    )
    parser.add_argument(
        "--poll-timeout",
        type=int,
        default=120,
        help="Seconds to wait for the attestation to flip to active|revoked (default 120).",
    )
    return parser.parse_args()


def _x_intent_url(text: str) -> str:
    return "https://twitter.com/intent/tweet?text=" + urllib.parse.quote(text, safe="")


def _sign_action(
    *,
    action: str,
    agent_address: str,
    chain_id: int,
    token: str,
) -> tuple[str, int, str]:
    """构造 typed-data → 走 awp-wallet 签 → 返回 (signature, timestamp, nonce)。"""
    timestamp = now_unix_seconds()
    nonce = new_signature_nonce()
    typed = build_action_typed_data(
        action=action,
        agent_address=agent_address,
        timestamp=timestamp,
        nonce=nonce,
        chain_id=chain_id,
    )
    step(
        "sign.request",
        action=action,
        agent_address=agent_address,
        timestamp=timestamp,
        nonce=nonce,
    )
    signature = sign_typed_data(typed, token=token)
    step("sign.ok", action=action, signature_prefix=signature[:10] + "…")
    return signature, timestamp, nonce


def _prompt_tweet_url() -> str:
    """从 stdin 阻塞读 URL，trim & validate；空输入视为放弃。"""
    print(
        "\nPaste the tweet URL once you've published it (or empty line to abort):",
        file=sys.stderr,
        flush=True,
    )
    try:
        raw = input("> ").strip()
    except EOFError:
        die("no input received from stdin (run interactively or pass --tweet-url)")
        return ""  # unreachable
    if not raw:
        die("aborted by user (no tweet URL provided)")
    return validate_tweet_url(raw)


def main() -> None:
    args = _parse_args()
    apply_api_base(args)

    agent = (
        validate_address(args.agent, "--agent")
        if args.agent
        else get_wallet_address(args.token or None)
    )
    info("agent resolved", agent=agent, chain_id=args.chain_id)

    # ── Step 1: prepare ───────────────────────────────────
    sig, ts, nonce = _sign_action(
        action="twitter_prepare",
        agent_address=agent,
        chain_id=args.chain_id,
        token=args.token,
    )
    prepared = kya_prepare_twitter(
        agent_address=agent, signature=sig, timestamp=ts, nonce=nonce
    )
    claim_text = prepared.get("claim_text") or ""
    claim_nonce = prepared.get("nonce") or ""
    if not claim_text or not claim_nonce:
        die(f"unexpected prepare response: {prepared}")
    step(
        "prepare.ok",
        nonce=claim_nonce,
        expires_at=prepared.get("expires_at"),
        claim_text_chars=len(claim_text),
    )

    # ── Step 2: tweet (interactive) ──────────────────────
    print("\n────── Tweet to publish ──────", file=sys.stderr)
    print(claim_text, file=sys.stderr)
    print("──────────────────────────────", file=sys.stderr)
    print(f"Quick intent link: {_x_intent_url(claim_text)}", file=sys.stderr)

    tweet_url = (
        validate_tweet_url(args.tweet_url) if args.tweet_url else _prompt_tweet_url()
    )

    # ── Step 3: claim ─────────────────────────────────────
    sig2, ts2, nonce2 = _sign_action(
        action="twitter_claim",
        agent_address=agent,
        chain_id=args.chain_id,
        token=args.token,
    )
    claim_resp = kya_claim_twitter(
        agent_address=agent,
        tweet_url=tweet_url,
        claim_nonce=claim_nonce,
        signature=sig2,
        timestamp=ts2,
        nonce=nonce2,
    )
    attestation_id = claim_resp.get("attestation_id") or ""
    if not attestation_id:
        die(f"unexpected claim response: {claim_resp}")
    step("claim.ok", attestation_id=attestation_id, status=claim_resp.get("status"))

    # ── Step 4: poll ──────────────────────────────────────
    if args.no_poll:
        print(
            json.dumps(
                {
                    "agent_address": agent,
                    "attestation_id": attestation_id,
                    "status": claim_resp.get("status"),
                    "tweet_url": tweet_url,
                }
            )
        )
        return

    final = kya_poll_attestation(
        agent_address=agent,
        attestation_id=attestation_id,
        type_filter="twitter_claim",
        interval_sec=5,
        timeout_sec=args.poll_timeout,
    )
    if not final:
        info(
            "poll timed out — verification still in queue, check the agent page later",
            attestation_id=attestation_id,
        )
        print(
            json.dumps(
                {
                    "agent_address": agent,
                    "attestation_id": attestation_id,
                    "status": "pending",
                    "tweet_url": tweet_url,
                    "timed_out": True,
                }
            )
        )
        return

    print(
        json.dumps(
            {
                "agent_address": agent,
                "attestation_id": final.get("id"),
                "status": final.get("status"),
                "tweet_url": tweet_url,
                "metadata": final.get("metadata", {}),
            }
        )
    )


if __name__ == "__main__":
    main()
