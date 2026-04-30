use super::{poll_kyc_session, resolve_agent, Ctx};
use crate::address::validate_address;
use crate::client;
use crate::eip712::{build_kyc_init_typed_data, new_signature_nonce, now_unix_seconds};
use crate::env::resolve_kyc_base;
use crate::error::{ErrorKind, KyaError, Result};
use crate::{output, wallet};
use clap::Parser;
use serde_json::json;
use std::time::Duration;

#[derive(Parser, Debug)]
pub struct Args {
    #[arg(long, default_value = "")]
    pub agent: String,
    /// Owner address. Defaults to the agent EOA (self-owned).
    #[arg(long, default_value = "")]
    pub owner: String,
    /// Skip the KYA web handoff and poll Didit inline.
    #[arg(long)]
    pub no_handoff: bool,
    /// Poll timeout in seconds (only when --no-handoff).
    #[arg(long, default_value_t = 900)]
    pub poll_timeout: u64,
}

pub fn run(ctx: &Ctx, args: Args) -> Result<()> {
    let agent = resolve_agent(ctx, &args.agent)?;
    let owner = if args.owner.is_empty() {
        agent.clone()
    } else {
        validate_address(&args.owner, "--owner")?
    };
    output::info(
        "addresses resolved",
        json!({ "agent": &agent, "owner": &owner, "chain_id": ctx.chain_id }),
    );

    let timestamp = now_unix_seconds();
    let nonce = new_signature_nonce();
    let typed = build_kyc_init_typed_data(&agent, &owner, timestamp, &nonce, ctx.chain_id)?;
    output::step(
        "sign.request",
        json!({
            "action": "kyc_init",
            "agent_address": &agent,
            "owner_address": &owner,
            "timestamp": timestamp,
            "nonce": &nonce,
        }),
    );
    let signature = wallet::sign_typed_data(&typed, &ctx.token)?;
    output::step(
        "sign.ok",
        json!({ "action": "kyc_init", "signature_prefix": &signature[..10.min(signature.len())] }),
    );

    let kyc_base = resolve_kyc_base();
    let session = client::kyc_create_session(
        &kyc_base,
        &agent,
        &owner,
        crate::client::SignedHeaders {
            signature: &signature,
            timestamp,
            nonce: &nonce,
        },
    )?;
    let session_id = session
        .get("session_id")
        .or_else(|| session.get("id"))
        .and_then(|x| x.as_str())
        .unwrap_or("")
        .to_string();
    let verification_url = session
        .get("verification_url")
        .and_then(|x| x.as_str())
        .unwrap_or("")
        .to_string();
    if session_id.is_empty() || verification_url.is_empty() {
        return Err(KyaError::new(
            ErrorKind::KyaError,
            format!("unexpected create_session response: {session}"),
        ));
    }
    output::step("kyc.session_created", json!({ "session_id": &session_id }));

    if !args.no_handoff {
        let url = build_handoff_url(
            &ctx.web_base,
            "/verify/human/session",
            &[
                ("agent", &agent),
                ("session_id", &session_id),
                ("didit_url", &verification_url),
            ],
        );
        let body = json!({
            "mode": "handoff",
            "agent_address": &agent,
            "owner_address": &owner,
            "session_id": &session_id,
            "verification_url": &verification_url,
            "status": session.get("status").cloned().unwrap_or(json!("Pending")),
            "handoff_url": &url,
        });
        output::ok(body, "complete_kyc_in_browser", None);
        return Ok(());
    }

    let final_session = poll_kyc_session(
        &kyc_base,
        &session_id,
        Duration::from_secs(5),
        Duration::from_secs(args.poll_timeout),
    )?;
    let body = match final_session {
        Some(s) => json!({
            "mode": "headless",
            "agent_address": &agent,
            "owner_address": &owner,
            "session_id": &session_id,
            "status": s.get("status"),
            "attestation_id": s.get("attestation_id"),
        }),
        None => json!({
            "mode": "headless",
            "agent_address": &agent,
            "session_id": &session_id,
            "verification_url": &verification_url,
            "status": "Pending",
            "timed_out": true,
        }),
    };
    output::ok(body, "ready", None);
    Ok(())
}

fn build_handoff_url(web_base: &str, path: &str, params: &[(&str, &str)]) -> String {
    let frag: Vec<String> = params
        .iter()
        .map(|(k, v)| format!("{}={}", percent_encode(k), percent_encode(v)))
        .collect();
    format!("{}{}#{}", web_base.trim_end_matches('/'), path, frag.join("&"))
}

fn percent_encode(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(b as char);
            }
            _ => out.push_str(&format!("%{:02X}", b)),
        }
    }
    out
}
