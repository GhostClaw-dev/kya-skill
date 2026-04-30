// 0x-address validation + EIP-55 checksum.
//
// EIP-712 message field types are `address`, but our HTTP bodies / typed-data
// JSON serialise them as lowercase-hex strings — same convention as kya_lib.py
// `validate_address(...).lower()`. This module exposes both validate (-> lower)
// and to_eip55 (for human display only — never put it on the wire).

use crate::error::{ErrorKind, KyaError, Result};
use once_cell::sync::Lazy;
use regex::Regex;
use sha3::{Digest, Keccak256};

static ADDR_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"^0x[a-fA-F0-9]{40}$").expect("address regex compiles"));
static SIG_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"^0x[a-fA-F0-9]{130}$").expect("signature regex compiles"));
static TX_HASH_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"^0x[a-fA-F0-9]{64}$").expect("tx hash regex compiles"));

/// Validate `0x` + 40 hex; return the lowercase form (wire convention).
pub fn validate_address(value: &str, name: &str) -> Result<String> {
    if !ADDR_RE.is_match(value) {
        return Err(KyaError::new(
            ErrorKind::InputRequired,
            format!(
                "{name} must look like 0x followed by 40 hex chars (got: {value:?})"
            ),
        ));
    }
    Ok(value.to_lowercase())
}

pub fn validate_signature(value: &str) -> Result<()> {
    if !SIG_RE.is_match(value) {
        return Err(KyaError::new(
            ErrorKind::InvalidSignature,
            format!("signature must be 0x followed by 130 hex chars (got: {value:?})"),
        ));
    }
    Ok(())
}

pub fn validate_tx_hash(value: &str) -> Result<()> {
    if !TX_HASH_RE.is_match(value) {
        return Err(KyaError::new(
            ErrorKind::Internal,
            format!("tx_hash must be 0x followed by 64 hex chars (got: {value:?})"),
        ));
    }
    Ok(())
}

/// EIP-55 checksum encoding. For human display only.
#[allow(dead_code)]
pub fn to_eip55(addr: &str) -> Result<String> {
    let lower = validate_address(addr, "address")?;
    let stripped = &lower[2..];
    let hash = Keccak256::digest(stripped.as_bytes());
    let hash_hex = hex::encode(hash);
    let mut out = String::with_capacity(42);
    out.push_str("0x");
    for (i, c) in stripped.chars().enumerate() {
        if c.is_ascii_alphabetic() {
            // Each hex nibble of the hash; >=8 means upper-case.
            let nibble = u8::from_str_radix(&hash_hex[i..=i], 16).unwrap_or(0);
            if nibble >= 8 {
                out.extend(c.to_uppercase());
            } else {
                out.push(c);
            }
        } else {
            out.push(c);
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validate_address_ok() {
        let v =
            validate_address("0xabCDef0123456789ABCDEF0123456789abcdef01", "agent").unwrap();
        assert_eq!(v, "0xabcdef0123456789abcdef0123456789abcdef01");
    }

    #[test]
    fn validate_address_bad() {
        assert!(validate_address("0xnope", "agent").is_err());
        assert!(validate_address("abCDef0123456789ABCDEF0123456789abcdef01", "agent").is_err());
    }

    #[test]
    fn validate_signature_ok() {
        let s = format!("0x{}", "a".repeat(130));
        assert!(validate_signature(&s).is_ok());
    }

    #[test]
    fn eip55_known_vector() {
        // EIP-55 sample from the spec.
        let v = to_eip55("0xfb6916095ca1df60bb79ce92ce3ea74c37c5d359").unwrap();
        assert_eq!(v, "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359");
    }
}
