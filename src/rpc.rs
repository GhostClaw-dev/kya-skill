// Base RPC eth_call to read AWPRegistry.nonces(user).
//
// Selector(`nonces(address)`) = 0x7ecebe00; tail = 32-byte zero-padded address.

use crate::address::validate_address;
use crate::client::http;
use crate::env::{resolve_rpc_url, AWP_REGISTRY_ADDRESS};
use crate::error::{ErrorKind, KyaError, Result};
use serde_json::{json, Value};
use std::time::Duration;

pub fn registry_nonce(user_address: &str) -> Result<u128> {
    let user = validate_address(user_address, "user")?;
    let selector = "0x7ecebe00";
    let mut data = String::with_capacity(74);
    data.push_str(selector);
    let stripped = user.trim_start_matches("0x");
    for _ in 0..(64 - stripped.len()) {
        data.push('0');
    }
    data.push_str(stripped);

    let body = json!({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{ "to": AWP_REGISTRY_ADDRESS, "data": data }, "latest"],
    });
    let url = resolve_rpc_url();
    let resp = http()
        .post(&url)
        .json(&body)
        .timeout(Duration::from_secs(15))
        .send()
        .map_err(|e| {
            KyaError::new(
                ErrorKind::RpcUnreachable,
                format!("Base RPC unreachable ({url}): {e}"),
            )
        })?;
    let v: Value = resp.json().map_err(|e| {
        KyaError::new(
            ErrorKind::RpcUnreachable,
            format!("Base RPC returned non-JSON: {e}"),
        )
    })?;
    if let Some(err) = v.get("error") {
        return Err(KyaError::new(
            ErrorKind::RpcUnreachable,
            format!("AWPRegistry.nonces revert: {err}"),
        ));
    }
    let result = v
        .get("result")
        .and_then(|x| x.as_str())
        .unwrap_or_default();
    if !result.starts_with("0x") {
        return Err(KyaError::new(
            ErrorKind::RpcUnreachable,
            format!("AWPRegistry.nonces malformed result: {result:?}"),
        ));
    }
    u128::from_str_radix(result.trim_start_matches("0x"), 16).map_err(|e| {
        KyaError::new(
            ErrorKind::RpcUnreachable,
            format!("AWPRegistry.nonces non-hex: {e}"),
        )
    })
}

/// Read AWPRegistry.isRegistered(addr) — true iff `boundTo[addr] != 0 ||
/// recipient[addr] != 0`. The on-chain definition is authoritative; we
/// don't fall back to the JSON-RPC `address.check` mirror because that
/// can lag behind chain state (per awp-skill SKILL.md guidance).
///
/// Selector(`isRegistered(address)`) = 0xc3c5a547.
pub fn awp_is_registered(user_address: &str) -> Result<bool> {
    let user = validate_address(user_address, "user")?;
    let selector = "0xc3c5a547";
    let mut data = String::with_capacity(74);
    data.push_str(selector);
    let stripped = user.trim_start_matches("0x");
    for _ in 0..(64 - stripped.len()) {
        data.push('0');
    }
    data.push_str(stripped);

    let body = json!({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{ "to": AWP_REGISTRY_ADDRESS, "data": data }, "latest"],
    });
    let url = resolve_rpc_url();
    let resp = http()
        .post(&url)
        .json(&body)
        .timeout(Duration::from_secs(15))
        .send()
        .map_err(|e| {
            KyaError::new(
                ErrorKind::RpcUnreachable,
                format!("Base RPC unreachable ({url}): {e}"),
            )
        })?;
    let v: Value = resp.json().map_err(|e| {
        KyaError::new(
            ErrorKind::RpcUnreachable,
            format!("Base RPC returned non-JSON: {e}"),
        )
    })?;
    if let Some(err) = v.get("error") {
        return Err(KyaError::new(
            ErrorKind::RpcUnreachable,
            format!("AWPRegistry.isRegistered revert: {err}"),
        ));
    }
    let result = v
        .get("result")
        .and_then(|x| x.as_str())
        .unwrap_or_default();
    if !result.starts_with("0x") {
        return Err(KyaError::new(
            ErrorKind::RpcUnreachable,
            format!("AWPRegistry.isRegistered malformed result: {result:?}"),
        ));
    }
    // bool ABI-encoded: last byte is 0 or 1, rest zero-padded.
    let trimmed = result.trim_start_matches("0x");
    let last_byte = u8::from_str_radix(
        &trimmed[trimmed.len().saturating_sub(2)..],
        16,
    )
    .map_err(|e| {
        KyaError::new(
            ErrorKind::RpcUnreachable,
            format!("AWPRegistry.isRegistered non-hex: {e}"),
        )
    })?;
    Ok(last_byte != 0)
}

pub fn ping() -> Result<()> {
    let body = json!({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_chainId",
        "params": [],
    });
    let url = resolve_rpc_url();
    let resp = http()
        .post(&url)
        .json(&body)
        .timeout(Duration::from_secs(5))
        .send()
        .map_err(|e| {
            KyaError::new(ErrorKind::RpcUnreachable, format!("Base RPC unreachable: {e}"))
        })?;
    if !resp.status().is_success() {
        return Err(KyaError::new(
            ErrorKind::RpcUnreachable,
            format!("Base RPC returned HTTP {}", resp.status().as_u16()),
        ));
    }
    Ok(())
}
