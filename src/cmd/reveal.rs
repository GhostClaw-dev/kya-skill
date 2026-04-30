use super::{resolve_agent, sign_action, signed, Ctx};
use crate::client;
use crate::error::{ErrorKind, KyaError, Result};
use crate::output;
use clap::{Parser, ValueEnum};
use serde_json::json;

#[derive(Copy, Clone, Debug, ValueEnum)]
pub enum RevealType {
    TwitterClaim,
    TelegramClaim,
    EmailClaim,
    Staking,
    Kyc,
}

impl RevealType {
    fn as_api(self) -> &'static str {
        match self {
            RevealType::TwitterClaim => "twitter_claim",
            RevealType::TelegramClaim => "telegram_claim",
            RevealType::EmailClaim => "email_claim",
            RevealType::Staking => "staking",
            RevealType::Kyc => "kyc",
        }
    }
}

#[derive(Parser, Debug)]
pub struct Args {
    #[arg(long, default_value = "")]
    pub agent: String,
    /// Limit reveal to one attestation type. Empty = all types.
    #[arg(long = "type", value_enum)]
    pub type_filter: Option<RevealType>,
}

pub fn run(ctx: &Ctx, args: Args) -> Result<()> {
    let agent = resolve_agent(ctx, &args.agent)?;
    let type_str = args.type_filter.map(|t| t.as_api());
    output::info(
        "agent resolved",
        json!({
            "agent": &agent,
            "chain_id": ctx.chain_id,
            "type": type_str.unwrap_or("all"),
        }),
    );

    let (sig, ts, n) = sign_action(ctx, "attestation_reveal", &agent)?;
    let payload =
        client::reveal_attestations(&ctx.api_base, &agent, type_str, signed(&sig, ts, &n))?;

    let attestations = payload
        .get("attestations")
        .and_then(|x| x.as_array())
        .cloned();
    if attestations.is_none() {
        return Err(KyaError::new(
            ErrorKind::KyaError,
            format!("unexpected reveal response shape: {payload}"),
        ));
    }
    output::step(
        "reveal.ok",
        json!({
            "agent": &agent,
            "type": type_str.unwrap_or("all"),
            "count": attestations.as_ref().map(|a| a.len()).unwrap_or(0),
        }),
    );
    output::ok(payload, "ready", None);
    Ok(())
}
