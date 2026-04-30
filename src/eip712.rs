// EIP-712 typed-data builders.
//
// We don't hash anything here — all hashing is delegated to
// `awp-wallet sign-typed-data`. This module just produces the canonical JSON
// that `awp-wallet` ingests. Schemas mirror api/src/crypto/eip712.ts and
// web/lib/eip712.ts (three-way alignment).

use crate::address::validate_address;
use crate::env::{
    AWP_REGISTRY_ADDRESS, AWP_REGISTRY_DOMAIN_NAME, AWP_REGISTRY_DOMAIN_VERSION,
    KYA_DOMAIN_NAME, KYA_DOMAIN_VERSION,
};
use crate::error::{ErrorKind, KyaError, Result};
use rand::RngCore;
use serde_json::{json, Value};

pub fn now_unix_seconds() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// 16-byte hex CSPRNG nonce — matches web/lib/eip712.ts `newSignatureNonce()`.
pub fn new_signature_nonce() -> String {
    let mut bytes = [0u8; 16];
    rand::thread_rng().fill_bytes(&mut bytes);
    hex::encode(bytes)
}

const KNOWN_ACTIONS: &[&str] = &[
    "twitter_prepare",
    "twitter_claim",
    "telegram_prepare",
    "telegram_claim",
    "email_prepare",
    "email_confirm",
    "delegated_staking_request",
    "attestation_reveal",
];

fn kya_domain(chain_id: u64) -> Value {
    json!({
        "name": KYA_DOMAIN_NAME,
        "version": KYA_DOMAIN_VERSION,
        "chainId": chain_id,
    })
}

fn kya_types_envelope(extra: Value) -> Value {
    let mut obj = json!({
        "EIP712Domain": [
            {"name": "name", "type": "string"},
            {"name": "version", "type": "string"},
            {"name": "chainId", "type": "uint256"},
        ],
    });
    if let (Value::Object(map), Value::Object(extras)) = (&mut obj, extra) {
        for (k, v) in extras {
            map.insert(k, v);
        }
    }
    obj
}

pub fn build_action_typed_data(
    action: &str,
    agent_address: &str,
    timestamp: u64,
    nonce: &str,
    chain_id: u64,
) -> Result<Value> {
    if !KNOWN_ACTIONS.contains(&action) {
        return Err(KyaError::new(
            ErrorKind::InputRequired,
            format!(
                "unknown action {action:?}; expected one of {}",
                KNOWN_ACTIONS.join(" | ")
            ),
        ));
    }
    let agent = validate_address(agent_address, "agent_address")?;
    Ok(json!({
        "domain": kya_domain(chain_id),
        "types": kya_types_envelope(json!({
            "Action": [
                {"name": "action",        "type": "string"},
                {"name": "agent_address", "type": "address"},
                {"name": "timestamp",     "type": "uint64"},
                {"name": "nonce",         "type": "string"},
            ]
        })),
        "primaryType": "Action",
        "message": {
            "action": action,
            "agent_address": agent,
            "timestamp": timestamp.to_string(),
            "nonce": nonce,
        },
    }))
}

pub fn build_kyc_init_typed_data(
    agent_address: &str,
    owner_address: &str,
    timestamp: u64,
    nonce: &str,
    chain_id: u64,
) -> Result<Value> {
    let agent = validate_address(agent_address, "agent_address")?;
    let owner = validate_address(owner_address, "owner_address")?;
    Ok(json!({
        "domain": kya_domain(chain_id),
        "types": kya_types_envelope(json!({
            "KycInit": [
                {"name": "action",        "type": "string"},
                {"name": "agent_address", "type": "address"},
                {"name": "owner_address", "type": "address"},
                {"name": "timestamp",     "type": "uint64"},
                {"name": "nonce",         "type": "string"},
            ]
        })),
        "primaryType": "KycInit",
        "message": {
            "action": "kyc_init",
            "agent_address": agent,
            "owner_address": owner,
            "timestamp": timestamp.to_string(),
            "nonce": nonce,
        },
    }))
}

fn awp_registry_domain(chain_id: u64) -> Value {
    json!({
        "name": AWP_REGISTRY_DOMAIN_NAME,
        "version": AWP_REGISTRY_DOMAIN_VERSION,
        "chainId": chain_id,
        "verifyingContract": AWP_REGISTRY_ADDRESS,
    })
}

fn awp_registry_types(primary_type: &str) -> Value {
    let counterparty_field = if primary_type == "SetRecipient" {
        "recipient"
    } else {
        "delegate"
    };
    json!({
        "EIP712Domain": [
            {"name": "name", "type": "string"},
            {"name": "version", "type": "string"},
            {"name": "chainId", "type": "uint256"},
            {"name": "verifyingContract", "type": "address"},
        ],
        primary_type: [
            {"name": "user", "type": "address"},
            {"name": counterparty_field, "type": "address"},
            {"name": "nonce", "type": "uint256"},
            {"name": "deadline", "type": "uint256"},
        ],
    })
}

pub fn build_set_recipient_typed_data(
    user_address: &str,
    recipient_address: &str,
    nonce: u128,
    deadline: u64,
    chain_id: u64,
) -> Result<Value> {
    let user = validate_address(user_address, "user")?;
    let recipient = validate_address(recipient_address, "recipient")?;
    Ok(json!({
        "domain": awp_registry_domain(chain_id),
        "types": awp_registry_types("SetRecipient"),
        "primaryType": "SetRecipient",
        "message": {
            "user": user,
            "recipient": recipient,
            "nonce": nonce.to_string(),
            "deadline": deadline.to_string(),
        },
    }))
}

pub fn build_grant_delegate_typed_data(
    user_address: &str,
    delegate_address: &str,
    nonce: u128,
    deadline: u64,
    chain_id: u64,
) -> Result<Value> {
    let user = validate_address(user_address, "user")?;
    let delegate = validate_address(delegate_address, "delegate")?;
    Ok(json!({
        "domain": awp_registry_domain(chain_id),
        "types": awp_registry_types("GrantDelegate"),
        "primaryType": "GrantDelegate",
        "message": {
            "user": user,
            "delegate": delegate,
            "nonce": nonce.to_string(),
            "deadline": deadline.to_string(),
        },
    }))
}

/// Validate user-facing AWP decimal string (e.g. "1000.5") and return wei
/// (integer base-10 string). Mirrors kya_lib.awp_to_wei.
pub fn awp_to_wei(amount_awp: &str) -> Result<String> {
    let s = amount_awp.trim();
    let (whole, frac) = match s.split_once('.') {
        Some((w, f)) => (w, f),
        None => (s, ""),
    };
    if whole.is_empty() || !whole.chars().all(|c| c.is_ascii_digit()) {
        return Err(KyaError::new(
            ErrorKind::InputRequired,
            format!("amount must be a positive decimal, got {amount_awp:?}"),
        ));
    }
    if !frac.is_empty() && !frac.chars().all(|c| c.is_ascii_digit()) {
        return Err(KyaError::new(
            ErrorKind::InputRequired,
            format!("amount must be a positive decimal, got {amount_awp:?}"),
        ));
    }
    if frac.len() > 18 {
        return Err(KyaError::new(
            ErrorKind::InputRequired,
            format!("amount has more than 18 decimal places: {amount_awp:?}"),
        ));
    }
    let mut padded = String::from(frac);
    while padded.len() < 18 {
        padded.push('0');
    }
    let mut wei = String::with_capacity(whole.len() + 18);
    wei.push_str(whole);
    wei.push_str(&padded);
    let wei = wei.trim_start_matches('0');
    let wei = if wei.is_empty() { "0" } else { wei };
    if wei == "0" {
        return Err(KyaError::new(
            ErrorKind::InputRequired,
            format!("amount must be > 0, got {amount_awp:?}"),
        ));
    }
    Ok(wei.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn nonce_is_32_hex_chars() {
        let n = new_signature_nonce();
        assert_eq!(n.len(), 32);
        assert!(n.chars().all(|c| c.is_ascii_hexdigit()));
    }

    #[test]
    fn action_typed_data_shape() {
        let td = build_action_typed_data(
            "twitter_prepare",
            "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd",
            1_700_000_000,
            "deadbeefdeadbeefdeadbeefdeadbeef",
            8453,
        )
        .unwrap();
        assert_eq!(td["primaryType"], "Action");
        assert_eq!(td["message"]["timestamp"], "1700000000");
        assert_eq!(td["domain"]["name"], "KYA");
    }

    #[test]
    fn unknown_action_rejected() {
        assert!(build_action_typed_data(
            "make_coffee",
            "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd",
            0,
            "00",
            8453,
        )
        .is_err());
    }

    #[test]
    fn awp_to_wei_basics() {
        assert_eq!(awp_to_wei("1").unwrap(), "1000000000000000000");
        assert_eq!(awp_to_wei("1000").unwrap(), "1000000000000000000000");
        assert_eq!(awp_to_wei("0.5").unwrap(), "500000000000000000");
        assert_eq!(awp_to_wei("12.5").unwrap(), "12500000000000000000");
    }

    #[test]
    fn awp_to_wei_rejects_zero() {
        assert!(awp_to_wei("0").is_err());
        assert!(awp_to_wei("0.0").is_err());
    }
}
