use super::Ctx;
use crate::address::{validate_address, validate_signature};
use crate::eip712::{build_action_typed_data, build_kyc_init_typed_data};
use crate::error::{ErrorKind, KyaError, Result};
use crate::{output, wallet};
use clap::{Parser, ValueEnum};
use serde_json::json;
use std::path::PathBuf;

#[derive(Copy, Clone, Debug, ValueEnum)]
pub enum ActionKind {
    TwitterPrepare,
    TwitterClaim,
    TelegramPrepare,
    TelegramClaim,
    EmailPrepare,
    EmailConfirm,
    KycInit,
    AttestationReveal,
}

impl ActionKind {
    fn as_str(self) -> &'static str {
        match self {
            ActionKind::TwitterPrepare => "twitter_prepare",
            ActionKind::TwitterClaim => "twitter_claim",
            ActionKind::TelegramPrepare => "telegram_prepare",
            ActionKind::TelegramClaim => "telegram_claim",
            ActionKind::EmailPrepare => "email_prepare",
            ActionKind::EmailConfirm => "email_confirm",
            ActionKind::KycInit => "kyc_init",
            ActionKind::AttestationReveal => "attestation_reveal",
        }
    }
}

#[derive(Parser, Debug)]
pub struct Args {
    /// KYA action to sign. `kyc_init` requires `--owner`.
    #[arg(long, value_enum)]
    pub action: ActionKind,
    #[arg(long)]
    pub agent: String,
    /// Required for `--action kyc-init`.
    #[arg(long, default_value = "")]
    pub owner: String,
    /// Unix seconds, as shown by the wizard.
    #[arg(long)]
    pub timestamp: u64,
    /// 16-byte hex nonce shown by the wizard.
    #[arg(long)]
    pub nonce: String,
    /// Also write the signature to this file.
    #[arg(long)]
    pub write_file: Option<PathBuf>,
}

pub fn run(ctx: &Ctx, args: Args) -> Result<()> {
    if args.timestamp == 0 {
        return Err(KyaError::new(
            ErrorKind::InputRequired,
            "--timestamp must be positive unix seconds",
        ));
    }
    let n = args.nonce.trim();
    if !(8..=128).contains(&n.len()) || !n.chars().all(|c| c.is_ascii_hexdigit()) {
        return Err(KyaError::new(
            ErrorKind::InputRequired,
            format!("--nonce must be hex (8-128 chars); got length={}: {n:?}", n.len()),
        ));
    }
    let agent = validate_address(&args.agent, "agent")?;

    let typed = match args.action {
        ActionKind::KycInit => {
            if args.owner.is_empty() {
                return Err(KyaError::new(
                    ErrorKind::InputRequired,
                    "--owner is required when --action kyc-init",
                ));
            }
            let owner = validate_address(&args.owner, "owner")?;
            build_kyc_init_typed_data(&agent, &owner, args.timestamp, n, ctx.chain_id)?
        }
        other => {
            if !args.owner.is_empty() {
                output::info(
                    "warning: --owner is ignored for action",
                    json!({ "action": other.as_str() }),
                );
            }
            build_action_typed_data(other.as_str(), &agent, args.timestamp, n, ctx.chain_id)?
        }
    };

    output::step(
        "sign.request",
        json!({
            "action": args.action.as_str(),
            "agent": &agent,
            "chain_id": ctx.chain_id,
            "timestamp": args.timestamp,
            "nonce": n,
        }),
    );
    let signature = wallet::sign_typed_data(&typed, &ctx.token)?;
    validate_signature(&signature)?;

    if let Some(p) = &args.write_file {
        std::fs::write(p, signature.as_bytes())?;
        output::info("signature written", json!({ "path": p.display().to_string() }));
    }
    println!("{signature}");
    Ok(())
}
