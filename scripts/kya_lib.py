#!/usr/bin/env python3
"""KYA helper library — shared utilities for kya-skill scripts.

KYA (Know Your Agent) skill 与 awp-skill 完全独立，但复用了相同的
`awp-wallet sign-typed-data` CLI 来产出 EIP-712 签名。本模块封装：

  - awp-wallet 调用（地址 / typed-data 签名）
  - KYA HTTP 客户端（POST /v1/attestations/twitter/{prepare,claim}，GET /agents/:addr/attestations）
  - EIP-712 typed-data 构造（与 web/lib/eip712.ts、api/src/crypto/eip712.ts 三方对齐）
  - 通用 stderr 日志、参数校验

设计原则：
  - 纯 stdlib，无 pip 依赖（开箱即跑）
  - 所有日志 → stderr，只有最终结果（签名 / attestation_id）输出到 stdout
  - 与 awp-skill 一样的 JSON-line 风格 `{"step": "...", ...}`，便于上游解析
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Optional

# ── 常量 ────────────────────────────────────────────────

ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
SIG_RE = re.compile(r"^0x[a-fA-F0-9]{130}$")
TWEET_URL_RE = re.compile(
    r"^https?://(?:twitter|x)\.com/[A-Za-z0-9_]+/status/\d+(?:\?.*)?$"
)

KYA_DOMAIN_NAME = "KYA"
KYA_DOMAIN_VERSION = "1"
DEFAULT_CHAIN_ID = 8453  # Base mainnet

USER_AGENT = "kya-skill/0.1.0"

# ── stderr 日志 ─────────────────────────────────────────


def info(msg: str, **fields: Any) -> None:
    """普通 info：JSON line 写 stderr。"""
    payload = {"info": msg}
    payload.update(fields)
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr, flush=True)


def step(name: str, **fields: Any) -> None:
    """关键步骤：方便外部 wrapper（IDE / CI）解析。"""
    payload = {"step": name}
    payload.update(fields)
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr, flush=True)


def die(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[name-defined]
    """打印错误并退出。"""
    print(json.dumps({"error": msg}, ensure_ascii=False), file=sys.stderr, flush=True)
    sys.exit(code)


# ── 校验 ────────────────────────────────────────────────


def validate_address(value: str, name: str = "address") -> str:
    """校验 0x...40hex；返回小写。"""
    if not isinstance(value, str) or not ADDR_RE.match(value):
        die(f"{name} must look like 0x followed by 40 hex chars (got: {value!r})")
    return value.lower()


def validate_tweet_url(value: str) -> str:
    if not isinstance(value, str) or not TWEET_URL_RE.match(value):
        die(
            "tweet_url must be https://(twitter|x).com/<handle>/status/<id> "
            f"(got: {value!r})"
        )
    return value


# ── awp-wallet bridge ───────────────────────────────────


def _awp_wallet_bin() -> str:
    """找 awp-wallet 二进制；未装则给出友好提示。"""
    bin_path = shutil.which("awp-wallet")
    if bin_path:
        return bin_path
    die(
        "awp-wallet CLI not found in PATH. Install from "
        "https://github.com/awp-core/awp-wallet, then retry."
    )
    return ""  # unreachable


def _awp_wallet_run(args: list[str], timeout: int = 60) -> str:
    """通用 awp-wallet 调用。失败时 die；成功返回 stdout 去尾空白。"""
    bin_path = _awp_wallet_bin()
    try:
        result = subprocess.run(
            [bin_path, *args], capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        die(f"awp-wallet {args[0]} timed out after {timeout}s")
        return ""  # unreachable
    if result.returncode != 0:
        msg = result.stderr.strip() or result.stdout.strip() or "unknown error"
        die(f"awp-wallet {args[0]} failed: {msg}")
    return result.stdout.strip()


def get_wallet_address(token: Optional[str] = None) -> str:
    """读取 awp-wallet 当前 EOA 地址。"""
    args = ["receive"]
    if token:
        args += ["--token", token]
    out = _awp_wallet_run(args)
    try:
        addr = json.loads(out).get("eoaAddress", "")
    except json.JSONDecodeError:
        die(f"awp-wallet receive returned non-JSON output: {out!r}")
        return ""  # unreachable
    return validate_address(addr, "wallet address")


def sign_typed_data(typed_data: dict, token: Optional[str] = None) -> str:
    """让 awp-wallet 对 EIP-712 typed-data 签名，返回 0x...130hex。"""
    args = ["sign-typed-data", "--data", json.dumps(typed_data, separators=(",", ":"))]
    if token:
        args += ["--token", token]
    out = _awp_wallet_run(args)
    try:
        sig = json.loads(out).get("signature", "")
    except json.JSONDecodeError:
        die(f"awp-wallet sign-typed-data returned non-JSON output: {out!r}")
        return ""  # unreachable
    if not SIG_RE.match(sig):
        die(f"awp-wallet returned malformed signature: {sig!r}")
    return sig


# ── EIP-712 typed-data ─────────────────────────────────


def _eip712_domain(chain_id: int) -> dict:
    return {"name": KYA_DOMAIN_NAME, "version": KYA_DOMAIN_VERSION, "chainId": chain_id}


def _eip712_types_envelope(extra: dict) -> dict:
    """awp-wallet / 大部分 EIP-712 工具要求 types 里包含 EIP712Domain。"""
    return {
        "EIP712Domain": [
            {"name": "name", "type": "string"},
            {"name": "version", "type": "string"},
            {"name": "chainId", "type": "uint256"},
        ],
        **extra,
    }


def build_action_typed_data(
    *,
    action: str,
    agent_address: str,
    timestamp: int,
    nonce: str,
    chain_id: int = DEFAULT_CHAIN_ID,
) -> dict:
    """构造 KYA twitter_prepare / twitter_claim 的 typed-data。"""
    if action not in ("twitter_prepare", "twitter_claim"):
        die(f"unknown action {action!r}; expected twitter_prepare|twitter_claim")
    return {
        "domain": _eip712_domain(chain_id),
        "types": _eip712_types_envelope(
            {
                "Action": [
                    {"name": "action", "type": "string"},
                    {"name": "agent_address", "type": "address"},
                    {"name": "timestamp", "type": "uint64"},
                    {"name": "nonce", "type": "string"},
                ]
            }
        ),
        "primaryType": "Action",
        "message": {
            "action": action,
            "agent_address": agent_address,
            "timestamp": str(timestamp),  # JSON 不支持 uint64，统一转字符串
            "nonce": nonce,
        },
    }


def build_kyc_init_typed_data(
    *,
    agent_address: str,
    owner_address: str,
    timestamp: int,
    nonce: str,
    chain_id: int = DEFAULT_CHAIN_ID,
) -> dict:
    return {
        "domain": _eip712_domain(chain_id),
        "types": _eip712_types_envelope(
            {
                "KycInit": [
                    {"name": "action", "type": "string"},
                    {"name": "agent_address", "type": "address"},
                    {"name": "owner_address", "type": "address"},
                    {"name": "timestamp", "type": "uint64"},
                    {"name": "nonce", "type": "string"},
                ]
            }
        ),
        "primaryType": "KycInit",
        "message": {
            "action": "kyc_init",
            "agent_address": agent_address,
            "owner_address": owner_address,
            "timestamp": str(timestamp),
            "nonce": nonce,
        },
    }


def now_unix_seconds() -> int:
    return int(time.time())


def new_signature_nonce() -> str:
    """16 字节十六进制随机串（与 web/lib/eip712.ts 对齐）。"""
    return secrets.token_hex(16)


# ── KYA HTTP 客户端 ────────────────────────────────────


def _kya_base() -> str:
    base = (os.environ.get("KYA_API_BASE") or "").rstrip("/")
    if not base:
        die(
            "KYA_API_BASE environment variable not set. Example: "
            "export KYA_API_BASE=https://kya.awp.network"
        )
    return base


def _http_request(
    method: str,
    url: str,
    *,
    headers: Optional[dict] = None,
    body: Optional[dict] = None,
    timeout: int = 20,
) -> tuple[int, dict]:
    """通用 HTTP 调用。返回 (status, parsed_json)。失败时不 die，让调用方决定。"""
    h = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    data: Optional[bytes] = None
    if body is not None:
        h["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, method=method, headers=h, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8") or "{}"
            return resp.status, json.loads(raw)
    except urllib.error.HTTPError as e:
        raw = (e.read() or b"").decode("utf-8") or "{}"
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"error": {"code": "HTTP_ERROR", "message": raw[:200]}}
    except urllib.error.URLError as e:
        die(f"KYA API unreachable ({url}): {e.reason}")
        return 0, {}  # unreachable


def _signed_headers(signature: str, timestamp: int, nonce: str) -> dict:
    return {
        "X-Agent-Signature": signature,
        "X-Agent-Timestamp": str(timestamp),
        "X-Agent-Nonce": nonce,
    }


def _check_response(status: int, payload: dict, action: str) -> dict:
    """KYA 错误格式约定为 `{ "error": { "code": "...", "message": "..." } }`。"""
    if 200 <= status < 300:
        return payload
    err = payload.get("error", {}) if isinstance(payload, dict) else {}
    code = err.get("code") or "HTTP_ERROR"
    msg = err.get("message") or f"{action} failed with HTTP {status}"
    die(f"[{code}] {action}: {msg}")
    return {}  # unreachable


def kya_prepare_twitter(
    *,
    agent_address: str,
    signature: str,
    timestamp: int,
    nonce: str,
) -> dict:
    """POST /v1/attestations/twitter/prepare"""
    base = _kya_base()
    status, payload = _http_request(
        "POST",
        f"{base}/v1/attestations/twitter/prepare",
        headers=_signed_headers(signature, timestamp, nonce),
        body={"agent_address": agent_address},
    )
    return _check_response(status, payload, "twitter_prepare")


def kya_claim_twitter(
    *,
    agent_address: str,
    tweet_url: str,
    claim_nonce: str,
    signature: str,
    timestamp: int,
    nonce: str,
) -> dict:
    """POST /v1/attestations/twitter/claim"""
    base = _kya_base()
    status, payload = _http_request(
        "POST",
        f"{base}/v1/attestations/twitter/claim",
        headers=_signed_headers(signature, timestamp, nonce),
        body={
            "agent_address": agent_address,
            "tweet_url": tweet_url,
            "nonce": claim_nonce,
        },
    )
    return _check_response(status, payload, "twitter_claim")


def kya_list_attestations(
    *, agent_address: str, type_filter: Optional[str] = None
) -> dict:
    """GET /v1/agents/:address/attestations"""
    base = _kya_base()
    qs = f"?type={type_filter}" if type_filter else ""
    status, payload = _http_request(
        "GET", f"{base}/v1/agents/{agent_address}/attestations{qs}"
    )
    return _check_response(status, payload, "list_attestations")


def kya_poll_attestation(
    *,
    agent_address: str,
    attestation_id: str,
    type_filter: str,
    interval_sec: int = 5,
    timeout_sec: int = 90,
) -> Optional[dict]:
    """轮询直到指定 attestation 进入 active/revoked，超时返回 None。"""
    started = time.time()
    while time.time() - started < timeout_sec:
        payload = kya_list_attestations(
            agent_address=agent_address, type_filter=type_filter
        )
        items = payload.get("attestations", []) if isinstance(payload, dict) else []
        for att in items:
            if att.get("id") == attestation_id:
                status = att.get("status")
                step("attestation.poll", attestation_id=attestation_id, status=status)
                if status in ("active", "revoked"):
                    return att
        time.sleep(interval_sec)
    return None


# ── KYC service ────────────────────────────────────────


def _kyc_base() -> str:
    base = (os.environ.get("KYA_KYC_BASE") or "").rstrip("/")
    if not base:
        die(
            "KYA_KYC_BASE environment variable not set. Example: "
            "export KYA_KYC_BASE=https://kya.awp.network"
        )
    return base


def kyc_create_session(
    *,
    agent_address: str,
    owner_address: str,
    signature: str,
    timestamp: int,
    nonce: str,
) -> dict:
    """POST /kyc/sessions（kyc-service）"""
    base = _kyc_base()
    status, payload = _http_request(
        "POST",
        f"{base}/kyc/sessions",
        headers=_signed_headers(signature, timestamp, nonce),
        body={"agent_address": agent_address, "owner_address": owner_address},
    )
    return _check_response(status, payload, "kyc_create_session")


def kyc_get_session(session_id: str) -> dict:
    base = _kyc_base()
    status, payload = _http_request("GET", f"{base}/kyc/sessions/{session_id}")
    return _check_response(status, payload, "kyc_get_session")


def kyc_poll_session(
    session_id: str, *, interval_sec: int = 5, timeout_sec: int = 600
) -> Optional[dict]:
    """轮询 KYC 会话直至终态（Approved/Declined/Abandoned），超时返回 None。"""
    started = time.time()
    terminal = {"Approved", "Declined", "Abandoned", "Expired"}
    while time.time() - started < timeout_sec:
        payload = kyc_get_session(session_id)
        status_field = payload.get("status")
        step(
            "kyc.poll",
            session_id=session_id,
            status=status_field,
            attestation_id=payload.get("attestation_id"),
        )
        if status_field in terminal:
            return payload
        time.sleep(interval_sec)
    return None


# ── 通用 CLI parser ────────────────────────────────────


def base_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--token",
        default=os.environ.get("AWP_WALLET_TOKEN", ""),
        help="awp-wallet session token (optional for newer wallet versions)",
    )
    parser.add_argument(
        "--chain-id",
        type=int,
        default=int(os.environ.get("KYA_CHAIN_ID", DEFAULT_CHAIN_ID)),
        help=f"EVM chain id used in EIP-712 domain (default: {DEFAULT_CHAIN_ID})",
    )
    parser.add_argument(
        "--api-base",
        default=os.environ.get("KYA_API_BASE", ""),
        help="KYA API base URL (overrides env KYA_API_BASE)",
    )
    return parser


def apply_api_base(args: argparse.Namespace) -> None:
    """让 --api-base 命令行参数覆盖环境变量。"""
    if getattr(args, "api_base", ""):
        os.environ["KYA_API_BASE"] = args.api_base
