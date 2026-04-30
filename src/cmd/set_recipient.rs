use super::{
    poll_relay, poll_staking_request, resolve_agent, sign_action, signed, Ctx,
};
use crate::address::{validate_address, validate_signature};
use crate::client;
use crate::eip712::{awp_to_wei, build_set_recipient_typed_data, now_unix_seconds};
use crate::env::DEFAULT_KYA_WORKNET_ID;
use crate::error::{ErrorKind, KyaError, Result};
use crate::{output, relay, rpc, wallet};
use clap::Parser;
use serde_json::json;
use std::time::Duration;

#[derive(Parser, Debug)]
pub struct Args {
    #[arg(long, default_value = "")]
    pub agent: String,
    /// Reward recipient. Default: KYA deposit address looked up from API.
    #[arg(long, default_value = "")]
    pub recipient: String,
    #[arg(long, default_value = DEFAULT_KYA_WORKNET_ID)]
    pub worknet: String,
    /// AWP decimal amount the owner wants matched. Triggers stage 2 when set.
    #[arg(long, default_value = "")]
    pub amount: String,
    #[arg(long, default_value_t = 3600)]
    pub deadline_seconds: u64,
    #[arg(long)]
    pub no_poll: bool,
    #[arg(long)]
    pub no_poll_staking: bool,
    #[arg(long, default_value_t = 300)]
    pub staking_poll_timeout: u64,
}

pub fn run(ctx: &Ctx, args: Args) -> Result<()> {
    let agent = resolve_agent(ctx, &args.agent)?;
    output::step("agent.resolved", json!({ "agent": &agent }));

    let recipient = if args.recipient.is_empty() {
        let payload = client::deposit_address(&ctx.api_base, &agent, &args.worknet)?;
        let r = payload
            .get("deposit_address")
            .and_then(|x| x.as_str())
            .ok_or_else(|| {
                KyaError::new(
                    ErrorKind::KyaError,
                    format!("KYA returned no deposit_address: {payload}"),
                )
            })?;
        validate_address(r, "recipient")?
    } else {
        validate_address(&args.recipient, "--recipient")?
    };
    output::step(
        "recipient.resolved",
        json!({
            "recipient": &recipient,
            "source": if args.recipient.is_empty() { "kya" } else { "flag" },
        }),
    );

    let amount_awp_norm: Option<String> = if args.amount.is_empty() {
        None
    } else {
        Some(validate_amount(&args.amount)?)
    };
    if amount_awp_norm.is_some() {
        output::step("amount.resolved", json!({ "amount_awp": amount_awp_norm }));
        // Eligibility precheck.
        let via = ensure_verified(&ctx.api_base, &agent)?;
        output::step("agent.verified", json!({ "via": via.join(",") }));
    }

    // Stage 1 — sign + relay setRecipient.
    let nonce = rpc::registry_nonce(&agent)?;
    let deadline = now_unix_seconds() + args.deadline_seconds.max(60);
    let typed = build_set_recipient_typed_data(
        &agent,
        &recipient,
        nonce,
        deadline,
        ctx.chain_id,
    )?;
    output::step(
        "eip712.built",
        json!({ "primary_type": typed["primaryType"], "deadline": deadline }),
    );
    let signature = wallet::sign_typed_data(&typed, &ctx.token)?;
    validate_signature(&signature)?;
    output::step("eip712.signed", json!({}));

    let res = relay::set_recipient(ctx.chain_id, &agent, &recipient, deadline, &signature)?;
    let tx_hash = res
        .get("txHash")
        .or_else(|| res.get("tx_hash"))
        .and_then(|x| x.as_str())
        .map(String::from);
    output::step(
        "relay.submitted",
        json!({ "tx_hash": &tx_hash, "status": res.get("status") }),
    );

    let final_status = if let (Some(tx), false) = (&tx_hash, args.no_poll) {
        Some(poll_relay(
            tx,
            Duration::from_secs(3),
            Duration::from_secs(90),
        )?)
    } else {
        None
    };
    output::info("relay set-recipient done", json!({ "tx_hash": &tx_hash }));

    let mut staking_request: Option<serde_json::Value> = None;

    if let Some(amount_awp) = amount_awp_norm {
        // Don't stage-2 if relay didn't confirm cleanly.
        if let Some(s) = &final_status {
            let st = s.get("status").and_then(|x| x.as_str()).unwrap_or("");
            if !st.is_empty() && st != "confirmed" {
                return Err(KyaError::new(
                    ErrorKind::RelayTxReverted,
                    format!(
                        "Skipping delegated-staking request because the relay tx didn't confirm cleanly (status={st:?})"
                    ),
                ));
            }
        }
        staking_request = Some(post_delegated_staking_request(
            ctx,
            &agent,
            &amount_awp,
            &args.worknet,
        )?);

        if let Some(req_obj) = staking_request
            .as_ref()
            .and_then(|w| w.get("request"))
            .cloned()
        {
            let request_id = req_obj
                .get("id")
                .and_then(|x| x.as_str())
                .map(String::from);
            output::step(
                "kya.staking_request.queued",
                json!({
                    "request_id": &request_id,
                    "status": req_obj.get("status"),
                    "worknet_id": req_obj.get("worknet_id"),
                    "amount_wei": req_obj.get("amount_wei"),
                }),
            );
            let status = req_obj.get("status").and_then(|x| x.as_str()).unwrap_or("");
            if matches!(status, "matched" | "no_capacity" | "failed") {
                fail_on_unsuccessful_terminal(&req_obj)?;
            }
            if !args.no_poll_staking {
                if let Some(rid) = request_id.as_ref() {
                    let final_req = poll_staking_request(
                        &ctx.api_base,
                        &agent,
                        rid,
                        Duration::from_secs(5),
                        Duration::from_secs(args.staking_poll_timeout.max(30)),
                    )?;
                    if let Some(req) = final_req {
                        staking_request = Some(json!({ "request": &req }));
                        output::step(
                            "kya.staking_request.terminal",
                            json!({
                                "request_id": req.get("id"),
                                "status": req.get("status"),
                                "matched_provider": req.get("matched_provider"),
                                "matched_allocation_id": req.get("matched_allocation_id"),
                                "failed_reason": req.get("failed_reason"),
                            }),
                        );
                        fail_on_unsuccessful_terminal(&req)?;
                    } else {
                        output::step(
                            "kya.staking_request.timeout",
                            json!({ "request_id": rid, "timeout_sec": args.staking_poll_timeout }),
                        );
                    }
                }
            }
        }
    }

    let body = json!({
        "agent_address": &agent,
        "recipient": &recipient,
        "tx_hash": tx_hash,
        "relay_response": res,
        "final_status": final_status,
        "amount_awp": amount_awp_norm_into_value(args.amount.as_str()),
        "staking_request": staking_request,
    });
    output::ok(body, "ready", None);
    Ok(())
}

fn validate_amount(raw: &str) -> Result<String> {
    let s = raw.trim();
    let re = regex::Regex::new(r"^\d+(?:\.\d{1,18})?$").unwrap();
    if !re.is_match(s) {
        return Err(KyaError::new(
            ErrorKind::InputRequired,
            format!("--amount must be a positive decimal, got {raw:?}"),
        ));
    }
    if s.parse::<f64>().unwrap_or(0.0) <= 0.0 {
        return Err(KyaError::new(
            ErrorKind::InputRequired,
            format!("--amount must be > 0, got {raw:?}"),
        ));
    }
    Ok(s.to_string())
}

fn ensure_verified(api_base: &str, agent: &str) -> Result<Vec<String>> {
    let payload = client::list_attestations(api_base, agent, None)?;
    let items = payload
        .get("attestations")
        .and_then(|x| x.as_array())
        .cloned()
        .unwrap_or_default();
    let mut via = Vec::new();
    for att in items {
        if att.get("status").and_then(|x| x.as_str()) != Some("active") {
            continue;
        }
        // Twitter / Telegram / Email all qualify as Social. KYC qualifies
        // as Human. The matching worker enforces "≥1 of either" — keep
        // both kinds in `via` for the audit log even if redundant.
        match att.get("type").and_then(|x| x.as_str()) {
            Some("twitter_claim" | "telegram_claim" | "email_claim")
                if !via.iter().any(|s: &String| s == "social") =>
            {
                via.push("social".to_string())
            }
            Some("kyc") if !via.iter().any(|s: &String| s == "human") => {
                via.push("human".to_string())
            }
            _ => {}
        }
    }
    if via.is_empty() {
        // Hand the calling agent a structured option list so it surfaces
        // the four verification methods to the owner instead of picking
        // one (which would be a paternalism failure — see SKILL.md rules).
        let options = serde_json::json!([
            {"kind":"social","method":"twitter","label":"Twitter (X) — public tweet","command":"kya-agent claim-twitter"},
            {"kind":"social","method":"telegram","label":"Telegram — public-channel post","command":"kya-agent claim-telegram"},
            {"kind":"social","method":"email","label":"Email — 6-digit code","command":"kya-agent claim-email"},
            {"kind":"human","method":"kyc","label":"KYC — Didit selfie + ID","command":"kya-agent kyc --owner <OWNER_ADDR>"}
        ]);
        return Err(KyaError::new(
            ErrorKind::NotVerified,
            "Agent must complete at least one verification before delegated staking.",
        )
        .with_hint("ask the owner to pick one of the four options; do not pick for them")
        .with_extras(serde_json::json!({
            "next_action": "choose_verification",
            "active_kinds": [],
            "options": options,
        })));
    }
    Ok(via)
}

fn post_delegated_staking_request(
    ctx: &Ctx,
    agent: &str,
    amount_awp: &str,
    worknet_id: &str,
) -> Result<serde_json::Value> {
    let amount_wei = awp_to_wei(amount_awp)?;
    output::step(
        "kya.staking_request.signing",
        json!({
            "agent": agent,
            "amount_awp": amount_awp,
            "amount_wei": &amount_wei,
            "worknet_id": worknet_id,
        }),
    );
    let (sig, ts, n) = sign_action(ctx, "delegated_staking_request", agent)?;
    client::request_delegated_staking(
        &ctx.api_base,
        agent,
        &amount_wei,
        worknet_id,
        signed(&sig, ts, &n),
    )
}

fn fail_on_unsuccessful_terminal(req: &serde_json::Value) -> Result<()> {
    let status = req.get("status").and_then(|x| x.as_str()).unwrap_or("");
    let failed_reason = req.get("failed_reason").and_then(|x| x.as_str()).unwrap_or("");
    let request_id = req.get("id").and_then(|x| x.as_str()).unwrap_or("");
    match status {
        "matched" => Ok(()),
        "no_capacity" => Err(KyaError::new(
            ErrorKind::NoCapacity,
            format!(
                "Delegated staking request reached no_capacity (request_id={request_id})"
            ),
        )),
        "failed" if failed_reason == "per_agent_cap_exceeded" => Err(KyaError::new(
            ErrorKind::PerAgentCapExceeded,
            format!(
                "Delegated staking failed: per-agent cap (10000 AWP) exceeded; request_id={request_id}"
            ),
        )),
        "failed" => Err(KyaError::new(
            ErrorKind::StakingRequestFailed,
            format!(
                "Delegated staking failed: failed_reason={failed_reason}, request_id={request_id}"
            ),
        )),
        _ => Ok(()),
    }
}

fn amount_awp_norm_into_value(raw: &str) -> serde_json::Value {
    if raw.is_empty() {
        json!(null)
    } else {
        json!(raw.trim())
    }
}
