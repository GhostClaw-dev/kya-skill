#!/usr/bin/env python3
"""KYA Email Claim — sign two halves and bind an inbox to the agent EOA.

Flow:
  1. Read agent address from awp-wallet (or --agent override) and target email
     (--email or first positional arg, else prompted on stdin).
  2. Sign EIP-712 Action(email_prepare) → POST /v1/attestations/email/prepare.
     KYA mails the user a one-time 6-digit code (~10 min TTL, 5 wrong tries
     invalidate the code).
  3. Tell the user to open their inbox and paste the code; read it from stdin
     (or --code in CI mode).
  4. Sign EIP-712 Action(email_confirm) → POST /v1/attestations/email/confirm.
     Server verifies the code and writes the email_claim attestation atomically.
  5. Optionally poll /v1/agents/{addr}/attestations?type=email_claim until the
     attestation is `active` (skip with --no-poll for headless / scripted use).
  6. Emit { agent_address, attestation_id, status, email } JSON on stdout.
     Live progress (`step` / `info` JSON lines) on stderr.

Why this script (vs sign-action.py loop):
  sign-action.py only signs one Action and exits — fine for the wizard's
  manual-paste UX, but for "agent does it for me" we want a single command
  that handles both signs, the inbox round-trip, and polling. This is the
  email analogue of sign-claim.py.

Examples:
  # Interactive (will prompt for email + code):
  python3 scripts/sign-email.py

  # Email pre-supplied, prompt only for code:
  python3 scripts/sign-email.py --email me@example.com

  # Fully headless (CI / re-run after a known code):
  python3 scripts/sign-email.py --email me@example.com --code 123456 --no-poll
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from kya_lib import (
    apply_api_base,
    base_parser,
    build_action_typed_data,
    die,
    get_wallet_address,
    info,
    kya_confirm_email,
    kya_poll_attestation,
    kya_prepare_email,
    new_signature_nonce,
    now_unix_seconds,
    sign_typed_data,
    step,
    validate_address,
)


# 与后端 emailVerificationService 校验一致：本地先挡一次，避免把明显不合法的
# 邮箱发到服务端再被 EMAIL_INVALID 退回。
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_CODE_RE = re.compile(r"^[0-9]{6}$")


def _parse_args() -> argparse.Namespace:
    parser = base_parser(
        "Bind an email to the agent EOA via KYA's email_prepare / "
        "email_confirm endpoints (two EIP-712 signatures, one inbox)."
    )
    parser.add_argument(
        "--agent",
        default="",
        help="Override the agent address (default: read from awp-wallet)",
    )
    parser.add_argument(
        "email",
        nargs="?",
        default="",
        help="Email address to bind. If omitted, you will be prompted on stdin.",
    )
    parser.add_argument(
        "--email",
        dest="email_flag",
        default="",
        help="Same as the positional email; takes precedence when both are given.",
    )
    parser.add_argument(
        "--code",
        default="",
        help="(Headless) 6-digit code from the verification email; skips stdin prompt.",
    )
    parser.add_argument(
        "--no-poll",
        action="store_true",
        help="Skip the post-confirm attestation poll and exit immediately.",
    )
    parser.add_argument(
        "--poll-timeout",
        type=int,
        default=60,
        help="Seconds to wait for status=active (default 60). Email is synchronous "
        "so 60s is plenty; bump only on very slow servers.",
    )
    return parser.parse_args()


def _resolve_email(args: argparse.Namespace) -> str:
    raw = (args.email_flag or args.email or "").strip()
    if not raw:
        if not sys.stdin.isatty():
            die("email required (pipe it as --email or as the first positional arg)")
        try:
            raw = input("Email to bind: ").strip()
        except (EOFError, KeyboardInterrupt):
            die("aborted by user (no email provided)")
    if not _EMAIL_RE.match(raw):
        die(f"invalid email format: {raw!r}")
    return raw


def _resolve_code(args: argparse.Namespace) -> str:
    raw = (args.code or "").strip()
    if not raw:
        if not sys.stdin.isatty():
            die(
                "code required in non-interactive mode "
                "(pass --code <6 digits> after reading the verification email)"
            )
        info(
            "check your inbox (and spam) for a 6-digit code from KYA",
            note="codes expire in ~10 minutes; 5 wrong attempts invalidate the code",
        )
        try:
            raw = input("Verification code (6 digits): ").strip()
        except (EOFError, KeyboardInterrupt):
            die("aborted by user (no code provided)")
    if not _CODE_RE.match(raw):
        die(f"code must be exactly 6 digits, got {raw!r}")
    return raw


def _sign_action(
    *,
    action: str,
    agent_address: str,
    chain_id: int,
    token: str,
) -> tuple[str, int, str]:
    """构造 typed-data → 走 awp-wallet 签 → 返回 (signature, timestamp, nonce)。

    与 sign-claim 保持一致：每个 action 都用全新的 timestamp + nonce，避免
    服务端 nonceService 拒绝重放。
    """
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


def main() -> None:
    args = _parse_args()
    apply_api_base(args)

    agent = (
        validate_address(args.agent, "--agent")
        if args.agent
        else get_wallet_address(args.token or None)
    )
    email = _resolve_email(args)
    info("agent resolved", agent=agent, chain_id=args.chain_id, email=email)

    # ── Stage 1: email_prepare ────────────────────────────
    sig1, ts1, nonce1 = _sign_action(
        action="email_prepare",
        agent_address=agent,
        chain_id=args.chain_id,
        token=args.token,
    )
    prepared = kya_prepare_email(
        agent_address=agent,
        email=email,
        signature=sig1,
        timestamp=ts1,
        nonce=nonce1,
    )
    step(
        "prepare.ok",
        email=prepared.get("email") or email,
        expires_at=prepared.get("expires_at"),
        resend_available_at=prepared.get("resend_available_at"),
    )

    # ── Stage 2: read 6-digit code from inbox ─────────────
    code = _resolve_code(args)

    # ── Stage 3: email_confirm ────────────────────────────
    sig2, ts2, nonce2 = _sign_action(
        action="email_confirm",
        agent_address=agent,
        chain_id=args.chain_id,
        token=args.token,
    )
    confirmed = kya_confirm_email(
        agent_address=agent,
        email=email,
        code=code,
        signature=sig2,
        timestamp=ts2,
        nonce=nonce2,
    )
    attestation_id = confirmed.get("attestation_id") or ""
    if not attestation_id:
        die(f"unexpected confirm response: {confirmed}")
    step("confirm.ok", attestation_id=attestation_id, status=confirmed.get("status"))

    # ── Stage 4: optional poll for status=active ──────────
    final_status = confirmed.get("status") or "pending"
    timed_out = False
    if not args.no_poll:
        final = kya_poll_attestation(
            agent_address=agent,
            attestation_id=attestation_id,
            type_filter="email_claim",
            interval_sec=3,
            timeout_sec=args.poll_timeout,
        )
        if final:
            final_status = final.get("status") or final_status
        else:
            timed_out = True
            info(
                "poll timed out — attestation should appear shortly, "
                "check the agent page later",
                attestation_id=attestation_id,
            )

    print(
        json.dumps(
            {
                "agent_address": agent,
                "attestation_id": attestation_id,
                "status": final_status,
                "email": email,
                "timed_out": timed_out,
            }
        )
    )


if __name__ == "__main__":
    main()
