use super::{
    poll_attestation, resolve_agent, sign_action, signed, stdin_is_tty, Ctx,
};
use crate::client;
use crate::error::{ErrorKind, KyaError, Result};
use crate::output;
use clap::Parser;
use once_cell::sync::Lazy;
use regex::Regex;
use serde_json::json;
use std::io::{stdout, Write};
use std::time::Duration;

static EMAIL_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"^[^\s@]+@[^\s@]+\.[^\s@]+$").expect("email regex"));
static CODE_RE: Lazy<Regex> = Lazy::new(|| Regex::new(r"^[0-9]{6}$").expect("code regex"));

#[derive(Parser, Debug)]
pub struct Args {
    #[arg(long, default_value = "")]
    pub agent: String,
    /// Email address to bind. Required when stdin is not a TTY.
    #[arg(long, default_value = "")]
    pub email: String,
    /// 6-digit code from the verification email. Required when stdin is not a TTY.
    #[arg(long, default_value = "")]
    pub code: String,
    /// Skip the post-confirm poll.
    #[arg(long)]
    pub no_poll: bool,
    #[arg(long, default_value_t = 60)]
    pub poll_timeout: u64,
}

pub fn run(ctx: &Ctx, args: Args) -> Result<()> {
    let agent = resolve_agent(ctx, &args.agent)?;
    let email = resolve_email(&args)?;
    output::info(
        "agent resolved",
        json!({ "agent": &agent, "chain_id": ctx.chain_id, "email": &email }),
    );

    // Stage 1 — email_prepare.
    let (sig1, ts1, n1) = sign_action(ctx, "email_prepare", &agent)?;
    let prepared = client::prepare_email(&ctx.api_base, &agent, &email, signed(&sig1, ts1, &n1))?;
    output::step(
        "prepare.ok",
        json!({
            "email": prepared.get("email").cloned().unwrap_or(json!(&email)),
            "expires_at": prepared.get("expires_at"),
            "resend_available_at": prepared.get("resend_available_at"),
        }),
    );

    // Stage 2 — read code.
    let code = resolve_code(&args)?;

    // Stage 3 — email_confirm.
    let (sig2, ts2, n2) = sign_action(ctx, "email_confirm", &agent)?;
    let confirmed = client::confirm_email(
        &ctx.api_base,
        &agent,
        &email,
        &code,
        signed(&sig2, ts2, &n2),
    )?;
    let attestation_id = confirmed
        .get("attestation_id")
        .and_then(|x| x.as_str())
        .unwrap_or("")
        .to_string();
    if attestation_id.is_empty() {
        return Err(KyaError::new(
            ErrorKind::KyaError,
            format!("unexpected confirm response: {confirmed}"),
        ));
    }
    output::step(
        "confirm.ok",
        json!({
            "attestation_id": &attestation_id,
            "status": confirmed.get("status"),
        }),
    );

    let (final_status, timed_out) = if !args.no_poll {
        let final_att = poll_attestation(
            &ctx.api_base,
            &agent,
            &attestation_id,
            "email_claim",
            Duration::from_secs(3),
            Duration::from_secs(args.poll_timeout),
        )?;
        match final_att {
            Some(att) => (
                att.get("status")
                    .and_then(|x| x.as_str())
                    .unwrap_or("pending")
                    .to_string(),
                false,
            ),
            None => {
                output::info(
                    "poll timed out — attestation should appear shortly",
                    json!({ "attestation_id": &attestation_id }),
                );
                (
                    confirmed
                        .get("status")
                        .and_then(|x| x.as_str())
                        .unwrap_or("pending")
                        .to_string(),
                    true,
                )
            }
        }
    } else {
        (
            confirmed
                .get("status")
                .and_then(|x| x.as_str())
                .unwrap_or("pending")
                .to_string(),
            false,
        )
    };

    let body = json!({
        "agent_address": &agent,
        "attestation_id": &attestation_id,
        "status": final_status,
        "email": &email,
        "timed_out": timed_out,
    });
    output::ok(body, "ready", None);
    Ok(())
}

fn resolve_email(args: &Args) -> Result<String> {
    let raw = args.email.trim().to_string();
    let raw = if raw.is_empty() {
        if !stdin_is_tty() {
            return Err(KyaError::new(
                ErrorKind::InputRequired,
                "email required (pass --email <addr> in non-interactive mode)",
            ));
        }
        let _ = write!(stdout(), "Email to bind: ");
        let _ = stdout().flush();
        let mut buf = String::new();
        std::io::stdin()
            .read_line(&mut buf)
            .map_err(|e| KyaError::new(ErrorKind::Internal, format!("stdin: {e}")))?;
        buf.trim().to_string()
    } else {
        raw
    };
    if !EMAIL_RE.is_match(&raw) {
        return Err(KyaError::new(
            ErrorKind::EmailInvalid,
            format!("invalid email format: {raw:?}"),
        ));
    }
    Ok(raw)
}

fn resolve_code(args: &Args) -> Result<String> {
    let raw = args.code.trim().to_string();
    let raw = if raw.is_empty() {
        if !stdin_is_tty() {
            return Err(KyaError::new(
                ErrorKind::InputRequired,
                "code required in non-interactive mode (pass --code <6 digits>)",
            ));
        }
        output::info(
            "check your inbox (and spam) for a 6-digit code from KYA",
            json!({
                "note": "codes expire in ~10 minutes; 5 wrong attempts invalidate the code",
            }),
        );
        let _ = write!(stdout(), "Verification code (6 digits): ");
        let _ = stdout().flush();
        let mut buf = String::new();
        std::io::stdin()
            .read_line(&mut buf)
            .map_err(|e| KyaError::new(ErrorKind::Internal, format!("stdin: {e}")))?;
        buf.trim().to_string()
    } else {
        raw
    };
    if !CODE_RE.is_match(&raw) {
        return Err(KyaError::new(
            ErrorKind::EmailCodeInvalid,
            format!("code must be exactly 6 digits, got {raw:?}"),
        ));
    }
    Ok(raw)
}
