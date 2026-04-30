// kya-agent — Know Your Agent CLI.
//
// Single static binary, clap subcommands, JSON-on-stdout / NDJSON-on-stderr
// protocol. See SKILL.md for the public contract.

use clap::{Parser, Subcommand};

mod address;
mod client;
mod cmd;
mod eip712;
mod env;
mod error;
mod magiclink;
mod output;
mod relay;
mod rpc;
mod version;
mod wallet;

#[derive(Parser)]
#[command(
    name = "kya-agent",
    version = version::VERSION,
    about = "KYA — sign identity & matchmaking attestations, drive AWP relayer set-recipient / grant-delegate.",
    long_about = None,
)]
struct Cli {
    /// awp-wallet session token (legacy awp-wallet only). Set AWP_WALLET_TOKEN to reuse.
    #[arg(long, env = "AWP_WALLET_TOKEN", global = true, default_value = "")]
    token: String,

    /// EIP-712 chain id. Default 8453 (Base mainnet).
    #[arg(long, env = "KYA_CHAIN_ID", global = true, default_value_t = 8453)]
    chain_id: u64,

    /// KYA API base URL. Default https://kya.link.
    #[arg(long, env = "KYA_API_BASE", global = true, default_value = "")]
    api_base: String,

    /// KYA web base URL (handoff URLs). Default https://kya.link.
    #[arg(long, env = "KYA_WEB_BASE", global = true, default_value = "")]
    web_base: String,

    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Self-check: awp-wallet present, KYA reachable, RPC reachable.
    Preflight(cmd::preflight::Args),
    /// First-run entry; prints onboarding hint after preflight.
    Bootstrap(cmd::bootstrap::Args),
    /// Non-destructive end-to-end probe; no signing, no API writes.
    SmokeTest(cmd::smoke_test::Args),
    /// Parse a kya-sign:// magic link and dispatch to the right subcommand.
    Open(cmd::open::Args),
    /// Twitter (X) claim — sign and post to KYA, hand the user a web link.
    ClaimTwitter(cmd::claim_twitter::Args),
    /// Telegram public-channel claim.
    ClaimTelegram(cmd::claim_telegram::Args),
    /// Email claim — bind a real inbox to the agent EOA.
    ClaimEmail(cmd::claim_email::Args),
    /// KYC — sign KycInit, create Didit session, poll terminal status.
    Kyc(cmd::kyc::Args),
    /// Reveal unredacted attestation metadata (off-chain only).
    Reveal(cmd::reveal::Args),
    /// List active attestations and report delegated-staking eligibility.
    Attestations(cmd::attestations::Args),
    /// AWPRegistry.setRecipient via AWP relayer (+ optional delegated-staking request).
    SetRecipient(cmd::set_recipient::Args),
    /// Re-check delegated-staking request status (post-timeout, manual re-poll).
    StakingStatus(cmd::staking_status::Args),
    /// AWPRegistry.grantDelegate(KyaAllocatorProxy) via AWP relayer.
    GrantDelegate(cmd::grant_delegate::Args),
    /// Generic EIP-712 signer (file / clipboard / stdin).
    Sign(cmd::sign::Args),
    /// Single-action signer (Action / KycInit) from wizard-supplied nonce + ts.
    SignAction(cmd::sign_action::Args),
}

fn main() {
    let cli = Cli::parse();
    let ctx = cmd::Ctx {
        token: cli.token.clone(),
        chain_id: cli.chain_id,
        api_base: env::resolve_api_base(&cli.api_base),
        web_base: env::resolve_web_base(&cli.web_base),
    };

    let result = match cli.command {
        Command::Preflight(a) => cmd::preflight::run(&ctx, a),
        Command::Bootstrap(a) => cmd::bootstrap::run(&ctx, a),
        Command::SmokeTest(a) => cmd::smoke_test::run(&ctx, a),
        Command::Open(a) => cmd::open::run(&ctx, a),
        Command::ClaimTwitter(a) => cmd::claim_twitter::run(&ctx, a),
        Command::ClaimTelegram(a) => cmd::claim_telegram::run(&ctx, a),
        Command::ClaimEmail(a) => cmd::claim_email::run(&ctx, a),
        Command::Kyc(a) => cmd::kyc::run(&ctx, a),
        Command::Reveal(a) => cmd::reveal::run(&ctx, a),
        Command::Attestations(a) => cmd::attestations::run(&ctx, a),
        Command::SetRecipient(a) => cmd::set_recipient::run(&ctx, a),
        Command::StakingStatus(a) => cmd::staking_status::run(&ctx, a),
        Command::GrantDelegate(a) => cmd::grant_delegate::run(&ctx, a),
        Command::Sign(a) => cmd::sign::run(&ctx, a),
        Command::SignAction(a) => cmd::sign_action::run(&ctx, a),
    };

    if let Err(e) = result {
        let kind = e.kind();
        output::emit_error(&e);
        std::process::exit(kind.exit_code());
    }
}
