#!/usr/bin/env python3
"""KYA Twitter Claim — sign machine half, hand the user a KYA web link.

Revision 5 flow (agent → user handoff):

  1. Read agent address from awp-wallet (or --agent override).
  2. Sign EIP-712 Action(twitter_prepare).
  3. POST /v1/attestations/twitter/prepare → receive {nonce, claim_text, expires_at}.
  4. Sign EIP-712 Action(twitter_claim) — cached for the web landing page to submit.
  5. Build and print a single KYA web URL:
        https://kya.link/verify/social/claim#agent=…&nonce=…&claim_text=…&sig=…&ts=…&msg_nonce=…
     The user opens it in their browser, posts the tweet, pastes the URL on the
     landing page, and submits — KYA writes the attestation.
  6. Exit. No stdin reads, no polling.

Why fragment (#) instead of query: keeps the claim signature out of KYA web's
server logs and out of the Referer header sent to twitter.com.

Headless / CI fallback (`--tweet-url`):
  When you already know the published tweet URL (no human in the loop), pass
  --tweet-url and the script will POST claim + poll attestations as before.

Examples:
  # Default — print the KYA web URL and exit
  KYA_API_BASE=https://kya.link python3 sign-claim.py

  # Headless — submit immediately (no web page involved)
  python3 sign-claim.py --tweet-url https://x.com/me/status/123 --agent 0xabc...

  # Local KYA / chain
  python3 sign-claim.py --api-base http://localhost:8080 --web-base http://localhost:3000 --chain-id 31337
"""

from __future__ import annotations

import argparse
import json
import sys

from kya_lib import (
    apply_api_base,
    base_parser,
    build_action_typed_data,
    build_web_landing_url,
    die,
    get_wallet_address,
    info,
    kya_claim_twitter,
    kya_poll_attestation,
    kya_prepare_twitter,
    kya_web_base,
    new_signature_nonce,
    now_unix_seconds,
    sign_typed_data,
    step,
    validate_address,
    validate_tweet_url,
)


def _parse_args() -> argparse.Namespace:
    parser = base_parser(
        "Run the KYA Twitter claim machine half and hand the user a web link "
        "to publish the tweet from."
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
            "Headless fallback: submit this tweet URL directly without printing "
            "a web link. Useful for CI / pre-published tweets."
        ),
    )
    parser.add_argument(
        "--no-poll",
        action="store_true",
        help="(Headless mode only) skip attestation polling and return immediately.",
    )
    parser.add_argument(
        "--poll-timeout",
        type=int,
        default=120,
        help="(Headless mode only) seconds to wait for the attestation (default 120).",
    )
    return parser.parse_args()


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


def _print_handoff_link(*, web_base: str, agent: str, prepared: dict, claim_sig: tuple[str, int, str]) -> None:
    """构造并向 stderr 打印 KYA 落地链接；stdout 输出结构化 JSON 给上游 agent。

    stderr 的"块状"打印是给人类（agent host UI）看的醒目提示；
    stdout 的 JSON 让 agent 解析、转发、记录。两路同时输出避免上游误读人类提示。
    """
    sig, ts, msg_nonce = claim_sig
    fragment = {
        "agent": agent,
        "nonce": prepared.get("nonce") or "",
        "claim_text": prepared.get("claim_text") or "",
        "expires_at": prepared.get("expires_at") or "",
        "sig": sig,
        "ts": str(ts),
        "msg_nonce": msg_nonce,
    }
    url = build_web_landing_url(
        web_base=web_base,
        path="/verify/social/claim",
        fragment_params=fragment,
    )

    print("", file=sys.stderr)
    print("────── Hand this link to the user ──────", file=sys.stderr)
    print(url, file=sys.stderr)
    print("────────────────────────────────────────", file=sys.stderr)
    print(
        "The link is valid for ~5 minutes (claim signature timestamp window). "
        "If it expires, re-run this script.",
        file=sys.stderr,
    )

    # Machine-readable summary so the host agent can forward / display the link.
    print(
        json.dumps(
            {
                "mode": "handoff",
                "agent_address": agent,
                "claim_nonce": prepared.get("nonce"),
                "claim_text": prepared.get("claim_text"),
                "expires_at": prepared.get("expires_at"),
                "handoff_url": url,
            }
        )
    )


def _run_headless(*, agent: str, args: argparse.Namespace, prepared: dict, claim_sig: tuple[str, int, str]) -> None:
    """`--tweet-url` 直接提交并可选轮询。保留给 CI / 已发推场景。"""
    sig2, ts2, nonce2 = claim_sig
    tweet_url = validate_tweet_url(args.tweet_url)
    claim_resp = kya_claim_twitter(
        agent_address=agent,
        tweet_url=tweet_url,
        claim_nonce=prepared.get("nonce") or "",
        signature=sig2,
        timestamp=ts2,
        nonce=nonce2,
    )
    attestation_id = claim_resp.get("attestation_id") or ""
    if not attestation_id:
        die(f"unexpected claim response: {claim_resp}")
    step("claim.ok", attestation_id=attestation_id, status=claim_resp.get("status"))

    if args.no_poll:
        print(
            json.dumps(
                {
                    "mode": "headless",
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
                    "mode": "headless",
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
                "mode": "headless",
                "agent_address": agent,
                "attestation_id": final.get("id"),
                "status": final.get("status"),
                "tweet_url": tweet_url,
                "metadata": final.get("metadata", {}),
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
    info("agent resolved", agent=agent, chain_id=args.chain_id)

    # ── Sign prepare ─────────────────────────────────
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

    # ── Sign claim (cached for landing page or submitted headless) ──
    claim_sig = _sign_action(
        action="twitter_claim",
        agent_address=agent,
        chain_id=args.chain_id,
        token=args.token,
    )

    # ── Branch: headless vs handoff ──────────────────
    if args.tweet_url:
        _run_headless(
            agent=agent, args=args, prepared=prepared, claim_sig=claim_sig
        )
        return

    _print_handoff_link(
        web_base=kya_web_base(args),
        agent=agent,
        prepared=prepared,
        claim_sig=claim_sig,
    )


if __name__ == "__main__":
    main()
