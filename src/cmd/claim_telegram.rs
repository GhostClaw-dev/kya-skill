use super::{poll_attestation, resolve_agent, sign_action, signed, Ctx};
use crate::client;
use crate::error::{ErrorKind, KyaError, Result};
use crate::output;
use clap::Parser;
use once_cell::sync::Lazy;
use regex::Regex;
use serde_json::json;
use std::time::Duration;

static TG_URL_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"^https?://t\.me/[A-Za-z0-9_]+/\d+(?:\?.*)?$")
        .expect("telegram url regex compiles")
});

#[derive(Parser, Debug)]
pub struct Args {
    #[arg(long, default_value = "")]
    pub agent: String,
    /// Pre-published public-channel message URL.
    #[arg(long, default_value = "")]
    pub message_url: String,
    #[arg(long)]
    pub no_poll: bool,
    #[arg(long, default_value_t = 120)]
    pub poll_timeout: u64,
}

pub fn run(ctx: &Ctx, args: Args) -> Result<()> {
    let agent = resolve_agent(ctx, &args.agent)?;
    output::info(
        "agent resolved",
        json!({ "agent": &agent, "chain_id": ctx.chain_id }),
    );

    let (sig1, ts1, n1) = sign_action(ctx, "telegram_prepare", &agent)?;
    let prepared = client::prepare_telegram(&ctx.api_base, &agent, signed(&sig1, ts1, &n1))?;
    let claim_text = prepared
        .get("claim_text")
        .and_then(|x| x.as_str())
        .unwrap_or("")
        .to_string();
    let claim_nonce = prepared
        .get("nonce")
        .and_then(|x| x.as_str())
        .unwrap_or("")
        .to_string();
    if claim_text.is_empty() || claim_nonce.is_empty() {
        return Err(KyaError::new(
            ErrorKind::KyaError,
            format!("unexpected prepare response: {prepared}"),
        ));
    }
    output::step(
        "prepare.ok",
        json!({
            "nonce": &claim_nonce,
            "expires_at": prepared.get("expires_at"),
            "claim_text_chars": claim_text.len(),
        }),
    );

    let (sig2, ts2, n2) = sign_action(ctx, "telegram_claim", &agent)?;

    if args.message_url.is_empty() {
        let ts2_str = ts2.to_string();
        let url = build_handoff_url(
            &ctx.web_base,
            "/verify/social/telegram",
            &[
                ("agent", &agent),
                ("nonce", &claim_nonce),
                ("claim_text", &claim_text),
                ("expires_at", prepared
                    .get("expires_at")
                    .and_then(|x| x.as_str())
                    .unwrap_or_default()),
                ("sig", &sig2),
                ("ts", &ts2_str),
                ("msg_nonce", &n2),
            ],
        );
        let body = json!({
            "mode": "handoff",
            "agent_address": &agent,
            "claim_nonce": &claim_nonce,
            "claim_text": &claim_text,
            "expires_at": prepared.get("expires_at"),
            "handoff_url": &url,
        });
        let next_cmd =
            "kya-agent claim-telegram --message-url <PUBLIC_CHANNEL_MESSAGE_URL>";
        output::ok(body, "post_telegram_message_then_resubmit", Some(next_cmd));
        return Ok(());
    }

    if !TG_URL_RE.is_match(&args.message_url) {
        return Err(KyaError::new(
            ErrorKind::InputRequired,
            format!(
                "message_url must be https://t.me/<channel>/<msg_id> (public channel only); got {:?}",
                args.message_url
            ),
        ));
    }
    let resp = client::claim_telegram(
        &ctx.api_base,
        &agent,
        &args.message_url,
        &claim_nonce,
        signed(&sig2, ts2, &n2),
    )?;
    let attestation_id = resp
        .get("attestation_id")
        .and_then(|x| x.as_str())
        .unwrap_or("")
        .to_string();
    if attestation_id.is_empty() {
        return Err(KyaError::new(
            ErrorKind::KyaError,
            format!("unexpected claim response: {resp}"),
        ));
    }
    output::step(
        "claim.ok",
        json!({ "attestation_id": &attestation_id, "status": resp.get("status") }),
    );

    if args.no_poll {
        output::ok(
            json!({
                "agent_address": &agent,
                "attestation_id": &attestation_id,
                "status": resp.get("status"),
                "message_url": &args.message_url,
            }),
            "ready",
            None,
        );
        return Ok(());
    }
    let final_att = poll_attestation(
        &ctx.api_base,
        &agent,
        &attestation_id,
        "telegram_claim",
        Duration::from_secs(5),
        Duration::from_secs(args.poll_timeout),
    )?;
    let body = match final_att {
        Some(att) => json!({
            "agent_address": &agent,
            "attestation_id": att.get("id"),
            "status": att.get("status"),
            "message_url": &args.message_url,
            "metadata": att.get("metadata").cloned().unwrap_or(json!({})),
        }),
        None => json!({
            "agent_address": &agent,
            "attestation_id": &attestation_id,
            "status": "pending",
            "message_url": &args.message_url,
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
