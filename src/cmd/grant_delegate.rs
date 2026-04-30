use super::{poll_relay, resolve_agent, Ctx};
use crate::address::{validate_address, validate_signature};
use crate::eip712::{build_grant_delegate_typed_data, now_unix_seconds};
use crate::env::KYA_ALLOCATOR_PROXY_ADDRESS;
use crate::error::Result;
use crate::{output, relay, rpc, wallet};
use clap::Parser;
use serde_json::json;
use std::time::Duration;

#[derive(Parser, Debug)]
pub struct Args {
    /// Provider EOA. Default: read from awp-wallet.
    #[arg(long, default_value = "")]
    pub provider: String,
    /// Delegate address. Default: canonical KyaAllocatorProxy.
    #[arg(long, default_value = KYA_ALLOCATOR_PROXY_ADDRESS)]
    pub delegate: String,
    #[arg(long, default_value_t = 3600)]
    pub deadline_seconds: u64,
    #[arg(long)]
    pub no_poll: bool,
}

pub fn run(ctx: &Ctx, args: Args) -> Result<()> {
    let provider = resolve_agent(ctx, &args.provider)?;
    let delegate = validate_address(&args.delegate, "--delegate")?;
    output::step(
        "provider.resolved",
        json!({ "provider": &provider, "delegate": &delegate }),
    );

    let nonce = rpc::registry_nonce(&provider)?;
    let deadline = now_unix_seconds() + args.deadline_seconds.max(60);
    let typed = build_grant_delegate_typed_data(
        &provider,
        &delegate,
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

    let res = relay::grant_delegate(ctx.chain_id, &provider, &delegate, deadline, &signature)?;
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
    output::info("relay grant-delegate done", json!({ "tx_hash": &tx_hash }));

    let body = json!({
        "provider_address": &provider,
        "delegate": &delegate,
        "tx_hash": tx_hash,
        "relay_response": res,
        "final_status": final_status,
    });
    output::ok(body, "ready", None);
    Ok(())
}
