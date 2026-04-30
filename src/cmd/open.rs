use super::Ctx;
use crate::error::{ErrorKind, KyaError, Result};
use crate::{magiclink, output};
use clap::Parser;
use serde_json::json;
use std::process::Command as ProcCommand;

#[derive(Parser, Debug)]
pub struct Args {
    /// kya-sign:// URL produced by KYA web.
    pub url: String,

    /// Print the dispatched command instead of executing it.
    #[arg(long)]
    pub dry_run: bool,
}

pub fn run(_ctx: &Ctx, args: Args) -> Result<()> {
    let parsed = magiclink::parse(&args.url)?;
    let cmd = magiclink::dispatch_command(&parsed)?.ok_or_else(|| {
        KyaError::new(
            ErrorKind::MagicLinkInvalid,
            format!("unknown magic link flow: {:?}", parsed.flow),
        )
    })?;

    if args.dry_run {
        let body = json!({
            "ok": true,
            "flow": parsed.flow,
            "params": &parsed.params,
            "dispatch": cmd,
        });
        output::ok(body, "execute_dispatch", Some(&cmd));
        return Ok(());
    }

    output::step(
        "open.dispatch",
        json!({ "flow": &parsed.flow, "command": &cmd }),
    );

    // Re-invoke ourselves with the dispatched argv. We split on whitespace
    // because we built `cmd` with controlled escaping in magiclink.rs — the
    // shell-escape there only quotes when it has to.
    let argv = shell_words(&cmd);
    if argv.is_empty() {
        return Err(KyaError::new(
            ErrorKind::Internal,
            "magic link dispatch produced empty argv",
        ));
    }
    // argv[0] should be `kya-agent`; we exec ourselves so that any Cargo
    // installation / install.sh placement is found via PATH.
    let status = ProcCommand::new(&argv[0])
        .args(&argv[1..])
        .status()
        .map_err(|e| {
            KyaError::new(
                ErrorKind::Internal,
                format!(
                    "failed to invoke `{}`: {e}. Ensure kya-agent is on PATH.",
                    &argv[0]
                ),
            )
        })?;
    std::process::exit(status.code().unwrap_or(1));
}

/// Minimal shell-word splitter — handles single-quote groups produced by
/// magiclink::shell_escape. Not a general-purpose tokeniser.
fn shell_words(s: &str) -> Vec<String> {
    let mut out = Vec::new();
    let mut cur = String::new();
    let mut in_single = false;
    let mut chars = s.chars().peekable();
    while let Some(c) = chars.next() {
        if in_single {
            if c == '\'' {
                // Could be end of group or escaped '\'' (which we encode as '\'').
                if let Some(&next) = chars.peek() {
                    if next == '\\' {
                        // peek further
                        let saved: Vec<char> = chars.clone().take(3).collect();
                        if saved == ['\\', '\'', '\''] {
                            chars.next();
                            chars.next();
                            chars.next();
                            cur.push('\'');
                            continue;
                        }
                    }
                }
                in_single = false;
            } else {
                cur.push(c);
            }
        } else if c == '\'' {
            in_single = true;
        } else if c.is_whitespace() {
            if !cur.is_empty() {
                out.push(std::mem::take(&mut cur));
            }
        } else {
            cur.push(c);
        }
    }
    if !cur.is_empty() {
        out.push(cur);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn split_basic() {
        assert_eq!(shell_words("kya-agent claim-twitter"), vec!["kya-agent", "claim-twitter"]);
    }

    #[test]
    fn split_quoted() {
        let out = shell_words("kya-agent --tweet-url 'https://x.com/a/status/1'");
        assert_eq!(
            out,
            vec!["kya-agent", "--tweet-url", "https://x.com/a/status/1"]
        );
    }
}
