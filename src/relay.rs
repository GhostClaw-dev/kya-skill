// AWP relayer client.
//
// We POST signed typed-data to the AWP relayer; it broadcasts on Base.
// Relay schema requires `deadline` as JSON number, NOT string (see kya_lib.py
// notes). Same for the on-chain status query.

use crate::address::{validate_signature, validate_tx_hash};
use crate::client::http;
use crate::env::resolve_relay_base;
use crate::error::{ErrorKind, KyaError, Result};
use serde_json::{json, Value};
use std::time::Duration;

fn post_relay(path: &str, body: &Value) -> Result<Value> {
    let url = format!("{}{}", resolve_relay_base(), path);
    let resp = http()
        .post(&url)
        .json(body)
        .timeout(Duration::from_secs(30))
        .send()
        .map_err(|e| {
            KyaError::new(
                ErrorKind::RelayUnreachable,
                format!("AWP relay unreachable ({url}): {e}"),
            )
        })?;
    let status = resp.status();
    let text = resp.text().unwrap_or_default();
    let parsed: Value = serde_json::from_str(&text).unwrap_or(json!({
        "error": { "message": &text[..text.len().min(200)] }
    }));
    if status.is_success() {
        return Ok(parsed);
    }
    let err_msg = parsed
        .get("error")
        .and_then(|e| e.get("message"))
        .and_then(|x| x.as_str())
        .map(|s| s.to_string())
        .unwrap_or_else(|| text.clone());
    Err(KyaError::new(
        ErrorKind::RelayUnreachable,
        format!("AWP relay {path} failed (status={}): {err_msg}", status.as_u16()),
    ))
}

pub fn set_recipient(
    chain_id: u64,
    user_address: &str,
    recipient_address: &str,
    deadline: u64,
    signature: &str,
) -> Result<Value> {
    validate_signature(signature)?;
    post_relay(
        "/api/relay/set-recipient",
        &json!({
            "chainId": chain_id,
            "user": user_address,
            "recipient": recipient_address,
            // Relayer schema demands JSON number; on-chain typed-data still uses string.
            "deadline": deadline,
            "signature": signature,
        }),
    )
}

pub fn grant_delegate(
    chain_id: u64,
    user_address: &str,
    delegate_address: &str,
    deadline: u64,
    signature: &str,
) -> Result<Value> {
    validate_signature(signature)?;
    post_relay(
        "/api/relay/grant-delegate",
        &json!({
            "chainId": chain_id,
            "user": user_address,
            "delegate": delegate_address,
            "deadline": deadline,
            "signature": signature,
        }),
    )
}

pub fn status(tx_hash: &str) -> Result<Value> {
    validate_tx_hash(tx_hash)?;
    let url = format!("{}/api/relay/status/{}", resolve_relay_base(), tx_hash);
    let resp = http()
        .get(&url)
        .timeout(Duration::from_secs(15))
        .send()
        .map_err(|e| {
            KyaError::new(
                ErrorKind::RelayUnreachable,
                format!("AWP relay status unreachable ({url}): {e}"),
            )
        })?;
    let status = resp.status();
    let text = resp.text().unwrap_or_default();
    let parsed: Value = serde_json::from_str(&text).unwrap_or(json!({}));
    if !status.is_success() {
        return Err(KyaError::new(
            ErrorKind::RelayUnreachable,
            format!("AWP relay status failed (HTTP {})", status.as_u16()),
        ));
    }
    Ok(parsed)
}

/// Reachability probe for preflight.
pub fn ping() -> Result<()> {
    let url = format!("{}/api/health", resolve_relay_base());
    let resp = http()
        .get(&url)
        .timeout(Duration::from_secs(5))
        .send()
        .map_err(|e| {
            KyaError::new(
                ErrorKind::RelayUnreachable,
                format!("AWP relay unreachable: {e}"),
            )
        })?;
    let s = resp.status().as_u16();
    if (200..500).contains(&s) {
        Ok(())
    } else {
        Err(KyaError::new(
            ErrorKind::RelayUnreachable,
            format!("AWP relay ping returned {s}"),
        ))
    }
}
