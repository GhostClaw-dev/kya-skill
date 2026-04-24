#!/usr/bin/env python3
"""Generic EIP-712 signer — sign any typed-data JSON with awp-wallet.

Useful when KYA web wants the user to sign a one-off payload that doesn't fit
the bundled `sign-claim.py` / `sign-kyc.py` recipes. The script reads the
typed-data JSON from one of:

  --from-file path.json      most explicit
  --from-clipboard           Windows / macOS / Linux (xclip/pbpaste)
  -                          stdin
  (default)                  if neither flag nor positional arg → stdin

Outputs the 0x...130hex signature to stdout (so it composes nicely with shell
pipes). Add `--write-file out.txt` to also write the signature to disk.

Examples:
  echo '{"domain":{...},"types":{...},...}' | python3 sign.py
  python3 sign.py --from-file typed.json
  python3 sign.py --from-clipboard --write-file sig.txt
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from kya_lib import SIG_RE, base_parser, die, info, sign_typed_data, step


def _read_clipboard() -> str:
    """跨平台读剪贴板。Windows 用 PowerShell，macOS pbpaste，Linux xclip。"""
    if sys.platform.startswith("win"):
        cmd = ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"]
    elif sys.platform == "darwin":
        cmd = ["pbpaste"]
    else:
        cmd = ["xclip", "-selection", "clipboard", "-o"]
    if not shutil.which(cmd[0]):
        die(f"clipboard tool not found: {cmd[0]}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired:
        die("clipboard read timed out")
        return ""  # unreachable
    if result.returncode != 0:
        die(f"clipboard read failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _parse_typed_data(raw: str) -> dict:
    raw = raw.strip()
    if not raw:
        die("empty typed-data input")
    try:
        td = json.loads(raw)
    except json.JSONDecodeError as e:
        die(f"typed-data is not valid JSON: {e}")
        return {}  # unreachable
    if not isinstance(td, dict):
        die("typed-data must be a JSON object")
    for key in ("domain", "types", "primaryType", "message"):
        if key not in td:
            die(f"typed-data missing required key: {key}")
    return td


def main() -> None:
    parser = base_parser("Sign any EIP-712 typed-data JSON with awp-wallet.")
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--from-file", default="", help="Read typed-data from a JSON file")
    src.add_argument(
        "--from-clipboard",
        action="store_true",
        help="Read typed-data from the system clipboard",
    )
    parser.add_argument(
        "--write-file",
        default="",
        help="Also write the signature to this file (in addition to stdout)",
    )
    parser.add_argument(
        "input",
        nargs="?",
        default="",
        help="Optional positional typed-data JSON or '-' for stdin",
    )
    args = parser.parse_args()

    if args.from_file:
        path = Path(args.from_file)
        if not path.is_file():
            die(f"file not found: {args.from_file}")
        raw = path.read_text(encoding="utf-8")
    elif args.from_clipboard:
        raw = _read_clipboard()
    elif args.input and args.input != "-":
        raw = args.input
    else:
        info("reading typed-data from stdin (Ctrl+D to end)")
        raw = sys.stdin.read()

    typed = _parse_typed_data(raw)
    domain = typed.get("domain", {})
    primary = typed.get("primaryType", "?")
    info(
        "typed-data parsed",
        primary_type=primary,
        domain_name=domain.get("name"),
        chain_id=domain.get("chainId"),
    )
    step("sign.request", primary_type=primary)

    signature = sign_typed_data(typed, token=args.token or None)
    if not SIG_RE.match(signature):
        die(f"awp-wallet returned malformed signature: {signature!r}")

    if args.write_file:
        Path(args.write_file).write_text(signature, encoding="utf-8")
        info("signature written", path=args.write_file)

    print(signature)


if __name__ == "__main__":
    main()
