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

USER_AGENT = "kya-skill/0.2.0"

# ── AWP 协议常量 ───────────────────────────────────────
# 这些地址来自 KYA web 的 chainActions.ts，与上游 AWP 协议合约一致；
# 我们故意硬编码而不接受外部覆盖：skill 只为 KYA 矩阵服务，把签名目标
# 写死可以避免被诱导成对未知合约的 EIP-712 攻击载体。
AWP_REGISTRY_ADDRESS = "0x0000F34Ed3594F54faABbCb2Ec45738DDD1c001A"
KYA_ALLOCATOR_PROXY_ADDRESS = "0xD544E5A2EF9100d3BD2fB7CffD2a4f7C773a1963"

AWP_REGISTRY_DOMAIN_NAME = "AWPRegistry"
AWP_REGISTRY_DOMAIN_VERSION = "1"

# AWP relayer 公网入口；用户可以通过 AWP_RELAY_BASE 覆盖。
DEFAULT_AWP_RELAY_BASE = "https://api.awp.sh"
# Base mainnet RPC;只用于读 AWPRegistry.nonces(user)。
DEFAULT_BASE_RPC_URL = "https://mainnet.base.org"

# KYA 默认接入的 worknet ID(chainId<<64 | counter,counter=12)。
# 仅用于在 prompt / 文档里展示;真正落库走 KYA 后端 worknet directory。
DEFAULT_KYA_WORKNET_ID = "845300000012"

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


def _awp_wallet_exec(args: list[str], timeout: int = 60) -> tuple[int, str, str]:
    """底层 awp-wallet 调用：返回 (returncode, stdout, stderr)，**不** die。

    所有业务函数在此之上封装：成功走快速路径；失败时可按语义判断是否自动 unlock 重试。
    """
    bin_path = _awp_wallet_bin()
    try:
        result = subprocess.run(
            [bin_path, *args], capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"awp-wallet {args[0]} timed out after {timeout}s"
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _awp_wallet_run(args: list[str], timeout: int = 60) -> str:
    """通用 awp-wallet 调用：失败时直接 die，成功返回 stdout。"""
    code, out, err = _awp_wallet_exec(args, timeout=timeout)
    if code != 0:
        msg = err or out or "unknown error"
        die(f"awp-wallet {args[0]} failed: {msg}")
    return out


# ── unlock / 自动重试 ───────────────────────────────────

# 触发自动 unlock 的关键词（stderr 里出现任一即认为是"没 token / 锁了"）
_LOCK_HINTS = (
    "locked",
    "unlocked",          # "wallet is not unlocked"
    "unauthoriz",        # "unauthorized" / "unauthorised"
    "token required",
    "missing token",
    "invalid token",
    "session expired",
    "no session",
    "--token",
)


def _looks_like_lock_error(stderr: str, stdout: str) -> bool:
    """启发式：只在 awp-wallet 提示"需要 token / session 失效"时才触发 unlock。"""
    blob = f"{stderr}\n{stdout}".lower()
    return any(h in blob for h in _LOCK_HINTS)


def unlock_wallet(
    *,
    scope: str = "transfer",
    duration_sec: int = 3600,
    persist_env: bool = True,
) -> str:
    """调用 `awp-wallet unlock` 获取 session token。

    - scope / duration 与 awp-skill 的约定一致（`--scope transfer --duration 3600`）。
    - unlock 的 stdout 是 `{"token": "..."}`，解析失败就 die。
    - persist_env=True 时顺便写入 AWP_WALLET_TOKEN，让同进程后续所有 awp-wallet 调用复用。
    """
    step("wallet.unlock", scope=scope, duration_sec=duration_sec)
    code, out, err = _awp_wallet_exec(
        ["unlock", "--scope", scope, "--duration", str(duration_sec)]
    )
    if code != 0:
        msg = err or out or "unknown error"
        die(
            "awp-wallet unlock failed: "
            f"{msg}. If your wallet is not initialized, run `awp-wallet init` first."
        )
    try:
        token = json.loads(out).get("token", "")
    except json.JSONDecodeError:
        # 一些老版本支持 `unlock --raw` 直接打印 token；兼容一下
        token = out if SIG_RE.pattern and out else ""
    token = token.strip()
    if not token:
        die(f"awp-wallet unlock returned no token (stdout={out!r})")
    if persist_env:
        os.environ["AWP_WALLET_TOKEN"] = token
    info("wallet unlocked", scope=scope)
    return token


def _call_with_autounlock(
    args_with_token: list[str],
    *,
    token: Optional[str],
    purpose: str,
    timeout: int = 60,
) -> str:
    """先尝试用现有 token 跑命令；若命令提示"需要 token"则自动 unlock 重试一次。

    `args_with_token` 是不含 `--token` 的参数列表；token 由本函数按状态拼进去。
    """
    attempt_token = token or os.environ.get("AWP_WALLET_TOKEN", "")
    base_args: list[str] = list(args_with_token)
    args_first = base_args + (["--token", attempt_token] if attempt_token else [])
    code, out, err = _awp_wallet_exec(args_first, timeout=timeout)
    if code == 0:
        return out
    if not _looks_like_lock_error(err, out):
        msg = err or out or "unknown error"
        die(f"awp-wallet {base_args[0]} failed during {purpose}: {msg}")

    info("awp-wallet indicates wallet is locked; unlocking automatically", purpose=purpose)
    fresh = unlock_wallet()
    code, out, err = _awp_wallet_exec(base_args + ["--token", fresh], timeout=timeout)
    if code != 0:
        msg = err or out or "unknown error"
        die(f"awp-wallet {base_args[0]} failed after unlock during {purpose}: {msg}")
    return out


def get_wallet_address(token: Optional[str] = None) -> str:
    """读取 awp-wallet 当前 EOA 地址；wallet 被锁时自动 unlock。"""
    out = _call_with_autounlock(["receive"], token=token, purpose="get_wallet_address")
    try:
        addr = json.loads(out).get("eoaAddress", "")
    except json.JSONDecodeError:
        die(f"awp-wallet receive returned non-JSON output: {out!r}")
        return ""  # unreachable
    return validate_address(addr, "wallet address")


def sign_typed_data(typed_data: dict, token: Optional[str] = None) -> str:
    """让 awp-wallet 对 EIP-712 typed-data 签名；wallet 被锁时自动 unlock 后重试。返回 0x...130hex。"""
    out = _call_with_autounlock(
        ["sign-typed-data", "--data", json.dumps(typed_data, separators=(",", ":"))],
        token=token,
        purpose="sign_typed_data",
    )
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
            "export KYA_API_BASE=https://kya.link"
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
            "export KYA_KYC_BASE=https://kya.link"
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


# ── AWP relayer ────────────────────────────────────────
#
# AWP 协议官方提供的 gasless 中继：用户对 AWPRegistry / AWPToken 上的
# EIP-712 typed-data 签名,把签名 POST 给 relayer,relayer 代付 gas
# 上链。KYA 默认假设用户钱包没有 ETH,所以撮合相关的三类操作
# (setRecipient / grantDelegate / stake) 全部走 relayer。
#
# 安全说明:
#  - typed-data 的 domain.verifyingContract 在本模块写死成 AWPRegistry
#    或由 relayer 在 stake/prepare 里返回,不接受外部覆盖。
#  - chainId 默认 8453(Base mainnet),与 KYA 后端校验保持一致。
#  - skill 不在用户机器上发链上交易,所有上链动作由 AWP relayer 完成。


def _awp_relay_base() -> str:
    base = (os.environ.get("AWP_RELAY_BASE") or DEFAULT_AWP_RELAY_BASE).rstrip("/")
    return base


def _base_rpc_url() -> str:
    return (os.environ.get("BASE_RPC_URL") or DEFAULT_BASE_RPC_URL).rstrip("/")


def _eth_call_nonces(user_address: str) -> int:
    """读 `AWPRegistry.nonces(address)` —— 用纯 stdlib 直接打 JSON-RPC eth_call。

    selector(`nonces(address)`) = 0x7ecebe00;tail 是 32 字节填充的地址。
    成功返回十进制 nonce(uint256)。失败时友好 die。
    """
    user = validate_address(user_address, "user")
    selector = "0x7ecebe00"
    data = selector + user.lower().replace("0x", "").rjust(64, "0")
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{"to": AWP_REGISTRY_ADDRESS, "data": data}, "latest"],
    }
    rpc = _base_rpc_url()
    status, body = _http_request("POST", rpc, body=payload)
    if status >= 400 or not isinstance(body, dict):
        die(f"Base RPC eth_call failed (status={status}): {body!r}")
    err = body.get("error")
    if err:
        die(f"AWPRegistry.nonces revert: {err}")
    result = body.get("result")
    if not isinstance(result, str) or not result.startswith("0x"):
        die(f"AWPRegistry.nonces returned malformed result: {result!r}")
    try:
        return int(result, 16)
    except ValueError:
        die(f"AWPRegistry.nonces non-hex result: {result!r}")
        return 0  # unreachable


def _awp_registry_domain(chain_id: int) -> dict:
    return {
        "name": AWP_REGISTRY_DOMAIN_NAME,
        "version": AWP_REGISTRY_DOMAIN_VERSION,
        "chainId": chain_id,
        "verifyingContract": AWP_REGISTRY_ADDRESS,
    }


def _awp_registry_types(primary_type: str) -> dict:
    """AWPRegistry 的 EIP712Domain 含 verifyingContract。"""
    return {
        "EIP712Domain": [
            {"name": "name", "type": "string"},
            {"name": "version", "type": "string"},
            {"name": "chainId", "type": "uint256"},
            {"name": "verifyingContract", "type": "address"},
        ],
        primary_type: [
            {"name": "user", "type": "address"},
            {"name": ("recipient" if primary_type == "SetRecipient" else "delegate"), "type": "address"},
            {"name": "nonce", "type": "uint256"},
            {"name": "deadline", "type": "uint256"},
        ],
    }


def build_awp_set_recipient_typed_data(
    *,
    user_address: str,
    recipient_address: str,
    nonce: int,
    deadline: int,
    chain_id: int = DEFAULT_CHAIN_ID,
) -> dict:
    """构造 AWPRegistry.SetRecipient typed-data,与 web/lib/awpRelay.ts 对齐。"""
    user = validate_address(user_address, "user")
    recipient = validate_address(recipient_address, "recipient")
    return {
        "domain": _awp_registry_domain(chain_id),
        "types": _awp_registry_types("SetRecipient"),
        "primaryType": "SetRecipient",
        "message": {
            "user": user,
            "recipient": recipient,
            "nonce": str(nonce),
            "deadline": str(deadline),
        },
    }


def build_awp_grant_delegate_typed_data(
    *,
    user_address: str,
    delegate_address: str,
    nonce: int,
    deadline: int,
    chain_id: int = DEFAULT_CHAIN_ID,
) -> dict:
    user = validate_address(user_address, "user")
    delegate = validate_address(delegate_address, "delegate")
    return {
        "domain": _awp_registry_domain(chain_id),
        "types": _awp_registry_types("GrantDelegate"),
        "primaryType": "GrantDelegate",
        "message": {
            "user": user,
            "delegate": delegate,
            "nonce": str(nonce),
            "deadline": str(deadline),
        },
    }


def awp_get_registry_nonce(user_address: str) -> int:
    """读 AWPRegistry.nonces(user)。包了一层日志,方便 wrapper 监控。"""
    step("awp.nonce.read", user=user_address)
    n = _eth_call_nonces(user_address)
    info("awp registry nonce", user=user_address, nonce=n)
    return n


def _post_relay(path: str, body: dict, *, timeout: int = 30) -> dict:
    base = _awp_relay_base()
    url = f"{base}{path}"
    status, payload = _http_request("POST", url, body=body, timeout=timeout)
    if status >= 400:
        err = payload.get("error") if isinstance(payload, dict) else None
        die(f"AWP relay {path} failed (status={status}): {err or payload!r}")
    return payload if isinstance(payload, dict) else {}


def relay_set_recipient(
    *,
    user_address: str,
    recipient_address: str,
    deadline: int,
    signature: str,
    chain_id: int = DEFAULT_CHAIN_ID,
) -> dict:
    """POST /api/relay/set-recipient —— relayer 代付 gas 把 setRecipient 上链。"""
    if not SIG_RE.match(signature):
        die(f"signature must be 0x followed by 130 hex chars (got: {signature!r})")
    return _post_relay(
        "/api/relay/set-recipient",
        {
            "chainId": chain_id,
            "user": user_address,
            "recipient": recipient_address,
            "deadline": str(deadline),
            "signature": signature,
        },
    )


def relay_grant_delegate(
    *,
    user_address: str,
    delegate_address: str,
    deadline: int,
    signature: str,
    chain_id: int = DEFAULT_CHAIN_ID,
) -> dict:
    if not SIG_RE.match(signature):
        die(f"signature must be 0x followed by 130 hex chars (got: {signature!r})")
    return _post_relay(
        "/api/relay/grant-delegate",
        {
            "chainId": chain_id,
            "user": user_address,
            "delegate": delegate_address,
            "deadline": str(deadline),
            "signature": signature,
        },
    )


def relay_stake_prepare(
    *,
    user_address: str,
    amount_wei: int,
    lock_seconds: int,
    chain_id: int = DEFAULT_CHAIN_ID,
) -> dict:
    """POST /api/relay/stake/prepare —— 拿到 Permit typed-data 与 submitTo 信息。"""
    return _post_relay(
        "/api/relay/stake/prepare",
        {
            "chainId": chain_id,
            "user": user_address,
            "amount": str(amount_wei),
            "lockDuration": int(lock_seconds),
        },
    )


def relay_stake_submit(submit_to: dict, signature: str) -> dict:
    """把 stake 签名 POST 回 relayer 返回的 submitTo.url。

    接受 prepare 给的 submitTo dict({method,url,body}),把 signature 合并进 body
    再发出去;只允许 url 落在 AWP_RELAY_BASE 下,避免被诱导成 SSRF。
    """
    if not SIG_RE.match(signature):
        die(f"signature must be 0x followed by 130 hex chars (got: {signature!r})")
    if not isinstance(submit_to, dict):
        die("submit_to must be the object returned by stake/prepare")
    url = submit_to.get("url")
    method = submit_to.get("method") or "POST"
    body = submit_to.get("body") or {}
    if not isinstance(url, str) or not isinstance(body, dict):
        die("submit_to is missing url/body")
    if not url.startswith(_awp_relay_base()):
        die(f"submit_to.url is not under AWP_RELAY_BASE ({url})")
    body_with_sig = dict(body)
    body_with_sig["signature"] = signature
    status, payload = _http_request(method, url, body=body_with_sig, timeout=30)
    if status >= 400:
        die(f"AWP relay stake submit failed (status={status}): {payload!r}")
    return payload if isinstance(payload, dict) else {}


def relay_status(tx_hash: str, *, timeout: int = 15) -> dict:
    """GET /api/relay/status/:txHash —— 查询 relay 提交后的链上确认状态。"""
    if not re.match(r"^0x[a-fA-F0-9]{64}$", tx_hash or ""):
        die(f"tx_hash must be 0x followed by 64 hex chars (got: {tx_hash!r})")
    base = _awp_relay_base()
    status, payload = _http_request(
        "GET", f"{base}/api/relay/status/{tx_hash}", timeout=timeout
    )
    if status >= 400:
        die(f"AWP relay status failed (status={status}): {payload!r}")
    return payload if isinstance(payload, dict) else {}


def wait_relay_confirmation(
    tx_hash: str,
    *,
    interval_sec: int = 3,
    timeout_sec: int = 90,
) -> dict:
    """轮询 relay status 至 confirmed/failed/timeout,沿途打 step 日志。"""
    started = time.time()
    while time.time() - started < timeout_sec:
        s = relay_status(tx_hash)
        st = s.get("status")
        step("relay.poll", tx_hash=tx_hash, status=st)
        if st in ("confirmed", "failed"):
            return s
        time.sleep(interval_sec)
    return {"txHash": tx_hash, "status": "timeout"}


# ── 通用 CLI parser ────────────────────────────────────


def base_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--token",
        default=os.environ.get("AWP_WALLET_TOKEN", ""),
        help=(
            "awp-wallet session token. Optional: newer awp-wallet versions don't need "
            "it, and for older/locked wallets this skill will call `awp-wallet unlock` "
            "automatically. Set AWP_WALLET_TOKEN env var to reuse a token."
        ),
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
