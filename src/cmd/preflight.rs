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

    // 6. AWP registration on Base — kya is a subnet of awp, every flow that
    // writes attestation or signs setRecipient assumes the EOA is already on
    // AWP. We DO NOT register here; that's awp-skill's job. We just detect
    // and bounce the user via _internal.handoff.
    let mut handoff: Option<serde_json::Value> = None;
    if !args.skip_rpc {
        if let Some(a) = agent.as_deref() {
            match rpc::awp_is_registered(a) {
                Ok(true) => {
                    checks.push(json!({
                        "name": "awp-registration",
                        "ok": true,
                        "detail": "agent EOA is registered on AWPRegistry"
                    }));
                }
                Ok(false) => {
                    checks.push(json!({
                        "name": "awp-registration",
                        "ok": false,
                        "detail": "agent EOA has neither boundTo nor recipient set on AWPRegistry"
                    }));
                    ready = false;
                    if next_action == "ready" {
                        next_action = "register_on_awp".to_string();
                    }
                    handoff = Some(json!({
                        "skill": "awp",
                        "skill_repo": "https://github.com/awp-core/awp-skill",
                        "intent": "register_agent",
                        "rationale": "KYA is a subnet of AWP; the agent EOA must be on AWPRegistry before any KYA flow. awp-skill provides a free, gasless onboarding (relay-onboard) that calls setRecipient(self).",
                        "user_message": "Your agent isn't registered on AWP yet. Install awp-skill (or use it if already installed) and run AWP onboarding — that's a free, gasless step. Once it lands, re-run `kya-agent preflight` and the KYA flow will continue."
                    }));
                }
                Err(e) => {
                    // RPC failure already accounted for as base-rpc check; don't
                    // double-fail. But surface the read error so a degraded RPC
                    // doesn't masquerade as "definitely not registered".
                    checks.push(json!({
                        "name": "awp-registration",
                        "ok": false,
                        "detail": format!("could not read AWPRegistry.isRegistered: {e}"),
                        "indeterminate": true
                    }));
                }
            }
        }
    }

    let body = json!({
        "ok": ready,
        "version": version::VERSION,
        "agent_address": agent,
        "api_base": ctx.api_base,
        "checks": checks,
    });
    let mut extras_map = serde_json::Map::new();
    if let Some(h) = handoff {
        extras_map.insert("handoff".to_string(), h);
    }
    if ready {
        // When everything is green, hint at the most common next move so the
        // calling agent doesn't have to guess. Other intents (verify, reveal,
        // grant-delegate) override this in conversation context.
        extras_map.insert(
            "suggested_journey".to_string(),
            json!("delegated_staking"),
        );
        extras_map.insert(
            "next_command_hint".to_string(),
            json!("kya-agent attestations"),
        );
    }
    let extras = if extras_map.is_empty() {
        None
    } else {
        Some(serde_json::Value::Object(extras_map))
    };
    output::ok_extra(body, &next_action, next_command.as_deref(), extras);
    if !ready {
        // Pick the most informative ErrorKind for the failure mode. AWP
        // registration is the most actionable miss (single skill handoff);
        // fall back to WalletNotConfigured for env / wallet failures.
        let kind = if next_action == "register_on_awp" {
            ErrorKind::AwpNotRegistered
        } else {
            ErrorKind::WalletNotConfigured
        };
        return Err(KyaError::new(
            kind,
            "preflight reported one or more failed checks",
        ));
    }
    Ok(())
}
