use super::{resolve_agent, sign_action, signed, Ctx};
use crate::client;
use crate::error::{ErrorKind, KyaError, Result};
use crate::output;
use clap::Parser;
use serde_json::json;

#[derive(Parser, Debug)]
pub struct Args {
    /// Override agent address (default: read from awp-wallet).
    #[arg(long, default_value = "")]
    pub agent: String,
}

/// Twitter (X) claim — handoff-only.
///
/// We sign `twitter_prepare` + `twitter_claim` locally with awp-wallet,
/// embed both signatures in a `https://kya.link/verify/social/claim#...`
/// URL, and hand that URL to the calling agent. KYA web walks the owner
/// through publishing the tweet and POSTs the claim itself; the agent
/// does NOT take the tweet URL back from the owner. After the owner
/// reports done, the agent re-runs `kya-agent attestations` to verify
/// the new attestation landed.
///
/// The previous `--tweet-url` agent-driven path was removed in v0.3.2 —
/// real-world testing showed calling LLMs (e.g. kaito on OpenClaw) drift
/// to it instead of presenting the handoff URL, producing a worse owner
/// experience and silently inviting wrong-shape signatures. CI / power
/// users that need a non-interactive path can construct the handoff URL
/// themselves and POST it; the binary stays single-purpose.
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

    // Stage 2 — sign twitter_claim. Both signatures land in the handoff URL.
    let (sig2, ts2, n2) = sign_action(ctx, "twitter_claim", &agent)?;

    let expires_at = prepared
        .get("expires_at")
        .and_then(|x| x.as_str())
        .unwrap_or_default();
    let ts2_str = ts2.to_string();
    let url = build_handoff_url(
        &ctx.web_base,
        "/verify/social/claim",
        &[
            ("agent", &agent),
            ("nonce", &claim_nonce),
            ("claim_text", &claim_text),
            ("expires_at", expires_at),
            ("sig", &sig2),
            ("ts", &ts2_str),
            ("msg_nonce", &n2),
        ],
    );
    // Stdout body intentionally exposes ONLY the handoff_url. Earlier
    // versions also surfaced claim_text / claim_nonce / expires_at, and
    // calling LLMs (kaito on OpenClaw, observed 2026-04-30) used those
    // fields to drift back into the deleted "ask owner to publish + paste
    // tweet URL" pattern. With claim_text gone from the JSON contract,
    // the agent has no raw material to invent that flow — only handoff_url
    // can be relayed to the owner. KYA web shows claim_text inside the
    // browser flow once the URL opens; the agent never needs to see it.
    let body = json!({
        "mode": "handoff",
        "agent_address": &agent,
        "handoff_url": &url,
        "instructions_for_agent": "Relay handoff_url verbatim to the owner. Do NOT ask the owner to publish a tweet/post or paste any URL back. KYA web walks them through it. After they say done, run `kya-agent attestations`.",
    });
    output::ok(body, "browser_handoff_then_verify", Some("kya-agent attestations"));
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
