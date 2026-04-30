use super::{poll_attestation, resolve_agent, sign_action, signed, Ctx};
use crate::client;
use crate::error::{ErrorKind, KyaError, Result};
use crate::output;
use clap::Parser;
use once_cell::sync::Lazy;
use regex::Regex;
use serde_json::json;
use std::time::Duration;

static TWEET_URL_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"^https?://(?:twitter|x)\.com/[A-Za-z0-9_]+/status/\d+(?:\?.*)?$")
        .expect("tweet url regex compiles")
});

#[derive(Parser, Debug)]
pub struct Args {
    /// Override agent address (default: read from awp-wallet).
    #[arg(long, default_value = "")]
    pub agent: String,

    /// Pre-published tweet URL — when set, the script claims directly without
    /// printing a handoff link.
    #[arg(long, default_value = "")]
    pub tweet_url: String,

    /// Skip post-claim attestation polling (headless mode only).
    #[arg(long)]
    pub no_poll: bool,

    /// Poll timeout in seconds (headless mode).
    #[arg(long, default_value_t = 120)]
    pub poll_timeout: u64,
}

pub fn run(ctx: &Ctx, args: Args) -> Result<()> {
    let agent = resolve_agent(ctx, &args.agent)?;
    output::info(
        "agent resolved",
        json!({ "agent": &agent, "chain_id": ctx.chain_id }),
    );

    // Stage 1 — sign twitter_prepare.
    let (sig1, ts1, n1) = sign_action(ctx, "twitter_prepare", &agent)?;
    let prepared = client::prepare_twitter(&ctx.api_base, &agent, signed(&sig1, ts1, &n1))?;
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

    // Stage 2 — sign twitter_claim (always; reused either by handoff URL or by direct submit).
    let (sig2, ts2, n2) = sign_action(ctx, "twitter_claim", &agent)?;

    if args.tweet_url.is_empty() {
        // Handoff branch — print KYA web URL.
        if !TWEET_URL_RE.is_match(&args.tweet_url) && !args.tweet_url.is_empty() {
            return Err(KyaError::new(
                ErrorKind::InputRequired,
                "tweet_url must be https://(twitter|x).com/<handle>/status/<id>",
            ));
        }
        let url = build_handoff_url(
            &ctx.web_base,
            "/verify/social/claim",
            &[
                ("agent", &agent),
                ("nonce", &claim_nonce),
                ("claim_text", &claim_text),
                (
                    "expires_at",
                    prepared
                        .get("expires_at")
                        .and_then(|x| x.as_str())
                        .unwrap_or_default(),
                ),
                ("sig", &sig2),
                ("ts", &ts2.to_string()),
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
        // Canonical path is web-driven: the owner clicks the handoff URL,
        // KYA web walks them through publishing the tweet and POSTs the
        // claim itself (signatures are already embedded in the URL). The
        // calling agent does NOT take the tweet URL back from the owner;
        // it just verifies via `kya-agent attestations` once the owner
        // says they're done. The legacy `--tweet-url` path is still
        // supported (see headless submit below) for power users / CI, but
        // it's not the journey the SKILL.md walks owners through.
        output::ok(body, "browser_handoff_then_verify", Some("kya-agent attestations"));
        return Ok(());
    }

    // Headless submit.
    if !TWEET_URL_RE.is_match(&args.tweet_url) {
        return Err(KyaError::new(
            ErrorKind::InputRequired,
            format!(
                "tweet_url must be https://(twitter|x).com/<handle>/status/<id>, got {:?}",
                args.tweet_url
            ),
        ));
    }
    let claim_resp = client::claim_twitter(
        &ctx.api_base,
        &agent,
        &args.tweet_url,
        &claim_nonce,
        signed(&sig2, ts2, &n2),
    )?;
    let attestation_id = claim_resp
        .get("attestation_id")
        .and_then(|x| x.as_str())
        .unwrap_or("")
        .to_string();
    if attestation_id.is_empty() {
        return Err(KyaError::new(
            ErrorKind::KyaError,
            format!("unexpected claim response: {claim_resp}"),
        ));
    }
    output::step(
        "claim.ok",
        json!({
            "attestation_id": &attestation_id,
            "status": claim_resp.get("status"),
        }),
    );

    if args.no_poll {
        let body = json!({
            "mode": "headless",
            "agent_address": &agent,
            "attestation_id": &attestation_id,
            "status": claim_resp.get("status"),
            "tweet_url": &args.tweet_url,
        });
        output::ok(body, "ready", None);
        return Ok(());
    }

    let final_att = poll_attestation(
        &ctx.api_base,
        &agent,
        &attestation_id,
        "twitter_claim",
        Duration::from_secs(5),
        Duration::from_secs(args.poll_timeout),
    )?;
    let body = match final_att {
        Some(att) => json!({
            "mode": "headless",
            "agent_address": &agent,
            "attestation_id": att.get("id"),
            "status": att.get("status"),
            "tweet_url": &args.tweet_url,
            "metadata": att.get("metadata").cloned().unwrap_or(json!({})),
        }),
        None => json!({
            "mode": "headless",
            "agent_address": &agent,
            "attestation_id": &attestation_id,
            "status": "pending",
            "tweet_url": &args.tweet_url,
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
