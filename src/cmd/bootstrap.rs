use super::{preflight, Ctx};
use crate::error::Result;
use crate::output;
use clap::Parser;
use serde_json::json;

#[derive(Parser, Debug)]
pub struct Args {}

pub fn run(ctx: &Ctx, _args: Args) -> Result<()> {
    output::info(
        "bootstrap is a thin alias for preflight + onboarding hint",
        json!({}),
    );
    preflight::run(ctx, preflight::Args { skip_relay: false, skip_rpc: false })
}
