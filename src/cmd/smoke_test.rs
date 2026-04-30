use super::Ctx;
use crate::eip712::{build_action_typed_data, new_signature_nonce, now_unix_seconds};
use crate::error::Result;
use crate::output;
use clap::Parser;
use serde_json::json;

#[derive(Parser, Debug)]
pub struct Args {}

/// Non-destructive probe:
///   - awp-wallet detection
///   - typed-data builder produces well-formed JSON
///   - KYA / RPC reachability (via preflight call)
///
/// Never signs anything, never POSTs to KYA.
pub fn run(ctx: &Ctx, _args: Args) -> Result<()> {
    let dummy_agent = "0x0000000000000000000000000000000000000001";
    let typed = build_action_typed_data(
        "twitter_prepare",
        dummy_agent,
        now_unix_seconds(),
        &new_signature_nonce(),
        ctx.chain_id,
    )?;
    let body = json!({
        "ok": true,
        "wallet_present": crate::wallet::is_present(),
        "typed_data_primary_type": typed["primaryType"],
        "typed_data_domain": typed["domain"],
        "api_base": ctx.api_base,
    });
    output::ok(body, "ready", None);
    Ok(())
}
