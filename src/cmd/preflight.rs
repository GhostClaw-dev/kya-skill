use super::Ctx;
use crate::error::{ErrorKind, KyaError, Result};
use crate::{client, output, relay, rpc, version, wallet};
use clap::Parser;
use serde_json::json;

#[derive(Parser, Debug)]
pub struct Args {
    /// Skip the AWP relay reachability probe.
    #[arg(long)]
    pub skip_relay: bool,
    /// Skip the Base RPC reachability probe.
    #[arg(long)]
    pub skip_rpc: bool,
}

pub fn run(ctx: &Ctx, args: Args) -> Result<()> {
    let mut checks = Vec::new();
    let mut ready = true;
    let mut next_action = "ready".to_string();
    let mut next_command: Option<String> = None;

    // 1. awp-wallet present?
    let wallet_present = wallet::is_present();
    checks.push(json!({
        "name": "awp-wallet",
        "ok": wallet_present,
        "detail": if wallet_present { "found in PATH" } else { "missing — install from https://github.com/awp-core/awp-wallet" }
    }));
    if !wallet_present {
        ready = false;
        next_action = "install_awp_wallet".to_string();
    }

    // 2. wallet address resolvable?
    let mut agent: Option<String> = None;
    if wallet_present {
        match wallet::get_address(&ctx.token) {
            Ok(a) => {
                agent = Some(a.clone());
                checks.push(json!({
                    "name": "wallet-address",
                    "ok": true,
                    "detail": a,
                }));
            }
            Err(e) => {
                checks.push(json!({
                    "name": "wallet-address",
                    "ok": false,
                    "detail": e.to_string(),
                }));
                ready = false;
                if next_action == "ready" {
                    next_action = "init_wallet".to_string();
                    next_command = Some("awp-wallet init".into());
                }
            }
        }
    }

    // 3. KYA reachable
    output::step("preflight.kya", json!({ "api_base": &ctx.api_base }));
    match client::ping(&ctx.api_base) {
        Ok(_) => checks.push(json!({ "name": "kya-api", "ok": true, "detail": ctx.api_base.clone() })),
        Err(e) => {
            checks.push(json!({ "name": "kya-api", "ok": false, "detail": e.to_string() }));
            ready = false;
            if next_action == "ready" {
                next_action = "check_kya_endpoint".to_string();
            }
        }
    }

    // 4. AWP relay reachable
    if !args.skip_relay {
        match relay::ping() {
            Ok(_) => checks.push(json!({ "name": "awp-relay", "ok": true })),
            Err(e) => checks.push(json!({ "name": "awp-relay", "ok": false, "detail": e.to_string() })),
        }
    }

    // 5. Base RPC reachable
    if !args.skip_rpc {
        match rpc::ping() {
            Ok(_) => checks.push(json!({ "name": "base-rpc", "ok": true })),
            Err(e) => checks.push(json!({ "name": "base-rpc", "ok": false, "detail": e.to_string() })),
        }
    }

    let body = json!({
        "ok": ready,
        "version": version::VERSION,
        "agent_address": agent,
        "api_base": ctx.api_base,
        "checks": checks,
    });
    output::ok(body, &next_action, next_command.as_deref());
    if !ready {
        return Err(KyaError::new(
            ErrorKind::WalletNotConfigured,
            "preflight reported one or more failed checks",
        ));
    }
    Ok(())
}
