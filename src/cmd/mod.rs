// Subcommand modules.

use crate::address::validate_address;
use crate::client::SignedHeaders;
use crate::eip712::{build_action_typed_data, new_signature_nonce, now_unix_seconds};
use crate::error::Result;
use crate::output;
use crate::wallet;
use serde_json::json;
use std::time::{Duration, Instant};

pub mod attestations;
pub mod bootstrap;
pub mod claim_email;
pub mod claim_telegram;
pub mod claim_twitter;
pub mod grant_delegate;
pub mod kyc;
pub mod open;
pub mod preflight;
pub mod reveal;
pub mod set_recipient;
pub mod sign;
pub mod sign_action;
pub mod smoke_test;

/// Shared context derived from global flags. Subcommands consume by reference.
pub struct Ctx {
    pub token: String,
    pub chain_id: u64,
    pub api_base: String,
    pub web_base: String,
}

/// Read agent address from awp-wallet (or honour caller override).
pub fn resolve_agent(ctx: &Ctx, override_addr: &str) -> Result<String> {
    if !override_addr.is_empty() {
        validate_address(override_addr, "--agent")
    } else {
        wallet::get_address(&ctx.token)
    }
}

/// Sign one Action(...) payload — returns (signature, timestamp, nonce).
pub fn sign_action(
    ctx: &Ctx,
    action: &str,
    agent_address: &str,
) -> Result<(String, u64, String)> {
    let timestamp = now_unix_seconds();
    let nonce = new_signature_nonce();
    let typed = build_action_typed_data(action, agent_address, timestamp, &nonce, ctx.chain_id)?;
    output::step(
        "sign.request",
        json!({
            "action": action,
            "agent_address": agent_address,
            "timestamp": timestamp,
            "nonce": &nonce,
        }),
    );
    let signature = wallet::sign_typed_data(&typed, &ctx.token)?;
    output::step(
        "sign.ok",
        json!({ "action": action, "signature_prefix": &signature[..10.min(signature.len())] }),
    );
    Ok((signature, timestamp, nonce))
}

pub fn signed<'a>(
    signature: &'a str,
    timestamp: u64,
    nonce: &'a str,
) -> SignedHeaders<'a> {
    SignedHeaders {
        signature,
        timestamp,
        nonce,
    }
}

/// Poll an attestation until status is `active` / `revoked` or the timeout
/// elapses. Returns Ok(Some(att)) on terminal state, Ok(None) on timeout.
pub fn poll_attestation(
    api_base: &str,
    agent_address: &str,
    attestation_id: &str,
    type_filter: &str,
    interval: Duration,
    timeout: Duration,
) -> Result<Option<serde_json::Value>> {
    let started = Instant::now();
    while started.elapsed() < timeout {
        let payload = crate::client::list_attestations(api_base, agent_address, Some(type_filter))?;
        let items = payload
            .get("attestations")
            .and_then(|x| x.as_array())
            .cloned()
            .unwrap_or_default();
        for att in &items {
            if att.get("id").and_then(|x| x.as_str()) == Some(attestation_id) {
                let st = att.get("status").and_then(|x| x.as_str()).unwrap_or("");
                output::step(
                    "attestation.poll",
                    json!({ "attestation_id": attestation_id, "status": st }),
                );
                if matches!(st, "active" | "revoked") {
                    return Ok(Some(att.clone()));
                }
            }
        }
        std::thread::sleep(interval);
    }
    Ok(None)
}

pub fn poll_relay(tx_hash: &str, interval: Duration, timeout: Duration) -> Result<serde_json::Value> {
    let started = Instant::now();
    while started.elapsed() < timeout {
        let s = crate::relay::status(tx_hash)?;
        let st = s.get("status").and_then(|x| x.as_str()).unwrap_or("");
        output::step("relay.poll", json!({ "tx_hash": tx_hash, "status": st }));
        if matches!(st, "confirmed" | "failed") {
            return Ok(s);
        }
        std::thread::sleep(interval);
    }
    Ok(json!({ "txHash": tx_hash, "status": "timeout" }))
}

pub fn poll_staking_request(
    api_base: &str,
    agent_address: &str,
    request_id: &str,
    interval: Duration,
    timeout: Duration,
) -> Result<Option<serde_json::Value>> {
    let started = Instant::now();
    let terminal = ["matched", "no_capacity", "failed"];
    while started.elapsed() < timeout {
        let items = crate::client::list_staking_requests(api_base, agent_address)?;
        for req in &items {
            if req.get("id").and_then(|x| x.as_str()) == Some(request_id) {
                let st = req.get("status").and_then(|x| x.as_str()).unwrap_or("");
                if terminal.contains(&st) {
                    return Ok(Some(req.clone()));
                }
                break;
            }
        }
        std::thread::sleep(interval);
    }
    Ok(None)
}

pub fn poll_kyc_session(
    kyc_base: &str,
    session_id: &str,
    interval: Duration,
    timeout: Duration,
) -> Result<Option<serde_json::Value>> {
    let started = Instant::now();
    let terminal = ["Approved", "Declined", "Abandoned", "Expired"];
    while started.elapsed() < timeout {
        let payload = crate::client::kyc_get_session(kyc_base, session_id)?;
        let st = payload.get("status").and_then(|x| x.as_str()).unwrap_or("");
        output::step(
            "kyc.poll",
            json!({
                "session_id": session_id,
                "status": st,
                "attestation_id": payload.get("attestation_id"),
            }),
        );
        if terminal.contains(&st) {
            return Ok(Some(payload));
        }
        std::thread::sleep(interval);
    }
    Ok(None)
}

/// Convenience for stdin TTY/piped detection — used by the interactive scripts.
pub fn stdin_is_tty() -> bool {
    use std::io::IsTerminal;
    std::io::stdin().is_terminal()
}
