// `kya-agent staking-status` — re-check the status of one or more KYA
// delegated-staking requests. The dominant call site is post-timeout: when
// `set-recipient --amount` exits with `_internal.next_action: staking_pending`
// (KYA's pool stake didn't land before the poll deadline), the calling agent
// loops back here later to see if it's been resolved.
//
// Public endpoint, no signing.

use super::{resolve_agent, Ctx};
use crate::client;
use crate::error::Result;
use crate::output;
use clap::Parser;
use serde_json::{json, Value};

#[derive(Parser, Debug)]
pub struct Args {
    /// Agent EOA. Default: read from awp-wallet.
    #[arg(long, default_value = "")]
    pub agent: String,
    /// Narrow to a single request id. Empty = list all requests for the agent.
    #[arg(long, default_value = "")]
    pub request_id: String,
}

pub fn run(ctx: &Ctx, args: Args) -> Result<()> {
    let agent = resolve_agent(ctx, &args.agent)?;

    let all = client::list_staking_requests(&ctx.api_base, &agent)?;
    let items: Vec<Value> = if args.request_id.is_empty() {
        all
    } else {
        all.into_iter()
            .filter(|r| {
                r.get("id").and_then(|x| x.as_str()) == Some(args.request_id.as_str())
            })
            .collect()
    };

    // Latest = first in API order. Status across multiple requests doesn't
    // collapse cleanly (one matched + one queued is its own state), so we
    // surface the focused row's status if --request-id was given, else the
    // most-recent overall.
    let focused = items.first().cloned();
    let latest_status = focused
        .as_ref()
        .and_then(|r| r.get("status"))
        .and_then(|s| s.as_str())
        .unwrap_or("")
        .to_string();

    // Map server status → next_action so the calling agent can branch
    // without re-reading SKILL.md. `staking_pending` here means "still
    // queued past the design timeout — server-side issue, not a normal
    // wait state". `no_requests` covers the case the agent ran this
    // before set-recipient even posted anything.
    let next_action = match latest_status.as_str() {
        "matched" => "matched",
        "queued" => "staking_pending",
        "failed" => "staking_failed",
        "no_capacity" => "no_capacity",
        "" => "no_requests",
        _ => "ready",
    };

    let extras = if latest_status == "queued" {
        Some(json!({
            "anomaly": {
                "code": "STAKING_PENDING",
                "message": "Per KYA's design the pool stakes immediately on submit — there is no matching/queue. A `queued` status past the post-submit timeout is a known server-side issue. Wait for the KYA team to resolve, then re-run this command. Do NOT re-run `kya-agent set-recipient --amount` — it would consume a fresh nonce on a still-pending request.",
            }
        }))
    } else {
        None
    };

    let body = json!({
        "agent_address": agent,
        "request_id_filter": if args.request_id.is_empty() { Value::Null } else { json!(args.request_id) },
        "items": items,
        "count": focused.as_ref().map(|_| 1).unwrap_or(0),
        "latest_status": latest_status,
    });
    output::ok_extra(body, next_action, None, extras);
    Ok(())
}
