use super::Ctx;
use crate::address::validate_signature;
use crate::error::{ErrorKind, KyaError, Result};
use crate::{output, wallet};
use clap::Parser;
use serde_json::Value;
use std::io::Read;
use std::path::PathBuf;
use std::process::{Command, Stdio};

#[derive(Parser, Debug)]
pub struct Args {
    /// Read typed-data from a JSON file.
    #[arg(long)]
    pub from_file: Option<PathBuf>,
    /// Read typed-data from the system clipboard.
    #[arg(long)]
    pub from_clipboard: bool,
    /// Optional positional typed-data JSON; "-" means stdin.
    #[arg(default_value = "")]
    pub input: String,
    /// Also write the signature to this file.
    #[arg(long)]
    pub write_file: Option<PathBuf>,
}

pub fn run(ctx: &Ctx, args: Args) -> Result<()> {
    let raw = if let Some(path) = &args.from_file {
        std::fs::read_to_string(path).map_err(|e| {
            KyaError::new(
                ErrorKind::InputRequired,
                format!("file not readable: {} ({e})", path.display()),
            )
        })?
    } else if args.from_clipboard {
        read_clipboard()?
    } else if !args.input.is_empty() && args.input != "-" {
        args.input.clone()
    } else {
        output::info("reading typed-data from stdin", serde_json::json!({}));
        let mut buf = String::new();
        std::io::stdin().read_to_string(&mut buf)?;
        buf
    };
    let typed = parse_typed_data(&raw)?;
    let domain = typed.get("domain").cloned().unwrap_or(serde_json::json!({}));
    let primary = typed
        .get("primaryType")
        .and_then(|x| x.as_str())
        .unwrap_or("?")
        .to_string();
    output::info(
        "typed-data parsed",
        serde_json::json!({
            "primary_type": &primary,
            "domain_name": domain.get("name"),
            "chain_id": domain.get("chainId"),
        }),
    );
    output::step("sign.request", serde_json::json!({ "primary_type": &primary }));

    let signature = wallet::sign_typed_data(&typed, &ctx.token)?;
    validate_signature(&signature)?;
    if let Some(p) = &args.write_file {
        std::fs::write(p, signature.as_bytes())?;
        output::info("signature written", serde_json::json!({ "path": p.display().to_string() }));
    }
    println!("{signature}");
    Ok(())
}

fn parse_typed_data(raw: &str) -> Result<Value> {
    let raw = raw.trim();
    if raw.is_empty() {
        return Err(KyaError::new(ErrorKind::InputRequired, "empty typed-data input"));
    }
    let v: Value = serde_json::from_str(raw).map_err(|e| {
        KyaError::new(ErrorKind::InputRequired, format!("typed-data not valid JSON: {e}"))
    })?;
    if !v.is_object() {
        return Err(KyaError::new(
            ErrorKind::InputRequired,
            "typed-data must be a JSON object",
        ));
    }
    for k in &["domain", "types", "primaryType", "message"] {
        if v.get(*k).is_none() {
            return Err(KyaError::new(
                ErrorKind::InputRequired,
                format!("typed-data missing required key: {k}"),
            ));
        }
    }
    Ok(v)
}

fn read_clipboard() -> Result<String> {
    let argv: Vec<&str> = if cfg!(windows) {
        vec!["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"]
    } else if cfg!(target_os = "macos") {
        vec!["pbpaste"]
    } else {
        vec!["xclip", "-selection", "clipboard", "-o"]
    };
    let out = Command::new(argv[0])
        .args(&argv[1..])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .map_err(|e| {
            KyaError::new(
                ErrorKind::InputRequired,
                format!("clipboard tool {} not available: {e}", argv[0]),
            )
        })?;
    if !out.status.success() {
        return Err(KyaError::new(
            ErrorKind::InputRequired,
            format!(
                "clipboard read failed: {}",
                String::from_utf8_lossy(&out.stderr)
            ),
        ));
    }
    Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
}
