// kya-sign:// magic link parser.
//
// Forms (from current SKILL.md table; we accept both "kya-sign://x?y=z" and
// "kya-sign://x/?y=z" — url crate normalises path/host inconsistently across
// schemes, so we parse manually):
//
//   kya-sign://twitter-claim?api=<base>&chain=8453
//   kya-sign://telegram-claim?api=<base>&chain=8453
//   kya-sign://email-claim?api=<base>[&email=<addr>]
//   kya-sign://kyc?api=<base>&owner=0x...
//   kya-sign://reveal?api=<base>[&type=email_claim|kyc|...]
//   kya-sign://sign?clip=1
//   kya-sign://set-recipient?api=<base>&worknet=<id>[&amount=<awp>]
//   kya-sign://set-recipient?recipient=0xdeposit...
//   kya-sign://grant-delegate

use crate::error::{ErrorKind, KyaError, Result};
use std::collections::BTreeMap;

#[derive(Debug, PartialEq)]
pub struct ParsedLink {
    pub flow: String,
    pub params: BTreeMap<String, String>,
}

pub fn parse(input: &str) -> Result<ParsedLink> {
    let s = input.trim();
    let rest = s
        .strip_prefix("kya-sign://")
        .ok_or_else(|| {
            KyaError::new(
                ErrorKind::MagicLinkInvalid,
                format!("expected kya-sign:// scheme, got {input:?}"),
            )
        })?;
    if rest.is_empty() {
        return Err(KyaError::new(
            ErrorKind::MagicLinkInvalid,
            "magic link has no flow",
        ));
    }
    let (flow_raw, query) = match rest.find('?') {
        Some(i) => (&rest[..i], &rest[i + 1..]),
        None => (rest, ""),
    };
    // Strip optional trailing slash.
    let flow = flow_raw.trim_end_matches('/').to_string();
    if flow.is_empty() {
        return Err(KyaError::new(
            ErrorKind::MagicLinkInvalid,
            "magic link flow is empty",
        ));
    }

    let mut params = BTreeMap::new();
    if !query.is_empty() {
        for pair in query.split('&') {
            if pair.is_empty() {
                continue;
            }
            let (k, v) = match pair.split_once('=') {
                Some((k, v)) => (k.to_string(), v.to_string()),
                None => (pair.to_string(), String::new()),
            };
            params.insert(percent_decode(&k), percent_decode(&v));
        }
    }
    Ok(ParsedLink { flow, params })
}

fn percent_decode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        let b = bytes[i];
        if b == b'+' {
            out.push(b' ');
            i += 1;
        } else if b == b'%' && i + 2 < bytes.len() {
            let hi = (bytes[i + 1] as char).to_digit(16);
            let lo = (bytes[i + 2] as char).to_digit(16);
            if let (Some(h), Some(l)) = (hi, lo) {
                out.push((h * 16 + l) as u8);
                i += 3;
            } else {
                out.push(b);
                i += 1;
            }
        } else {
            out.push(b);
            i += 1;
        }
    }
    String::from_utf8(out).unwrap_or_else(|e| {
        // Fall back to lossy if not utf8 — magic links should always be utf8.
        String::from_utf8_lossy(e.as_bytes()).into_owned()
    })
}

/// Build the equivalent `kya-agent ...` command for display purposes.
/// Returns Ok(None) if we don't recognise the flow.
pub fn dispatch_command(link: &ParsedLink) -> Result<Option<String>> {
    let p = &link.params;
    let api = p.get("api").cloned();
    let chain = p.get("chain").cloned();
    let mut parts: Vec<String> = vec!["kya-agent".to_string()];
    if let Some(a) = &api {
        parts.push(format!("--api-base {}", shell_escape(a)));
    }
    if let Some(c) = &chain {
        parts.push(format!("--chain-id {}", shell_escape(c)));
    }
    match link.flow.as_str() {
        "twitter-claim" => {
            // Web-driven only since v0.3.2. Any `tweet=` param is dropped on
            // purpose — the canonical path is the handoff URL the binary
            // emits, which KYA web walks the owner through.
            parts.push("claim-twitter".into());
        }
        "telegram-claim" => {
            // Same as twitter-claim — `message=` param dropped post-v0.3.2.
            parts.push("claim-telegram".into());
        }
        "email-claim" => {
            parts.push("claim-email".into());
            if let Some(e) = p.get("email") {
                parts.push(format!("--email {}", shell_escape(e)));
            }
        }
        "kyc" => {
            parts.push("kyc".into());
            if let Some(o) = p.get("owner") {
                parts.push(format!("--owner {}", shell_escape(o)));
            }
        }
        "reveal" => {
            parts.push("reveal".into());
            if let Some(t) = p.get("type") {
                parts.push(format!("--type {}", shell_escape(t)));
            }
        }
        "sign" => {
            parts.push("sign".into());
            if p.get("clip").map(|s| s.as_str()) == Some("1") {
                parts.push("--from-clipboard".into());
            }
        }
        "set-recipient" => {
            parts.push("set-recipient".into());
            if let Some(r) = p.get("recipient") {
                parts.push(format!("--recipient {}", shell_escape(r)));
            }
            if let Some(w) = p.get("worknet") {
                parts.push(format!("--worknet {}", shell_escape(w)));
            }
            if let Some(a) = p.get("amount") {
                parts.push(format!("--amount {}", shell_escape(a)));
            }
        }
        "grant-delegate" => parts.push("grant-delegate".into()),
        _ => return Ok(None),
    }
    Ok(Some(parts.join(" ")))
}

fn shell_escape(s: &str) -> String {
    if s.chars()
        .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-' | '/' | ':' | '.' | '@'))
    {
        s.to_string()
    } else {
        let escaped = s.replace('\'', "'\\''");
        format!("'{}'", escaped)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_basic() {
        let l =
            parse("kya-sign://reveal?api=https://kya.link&type=email_claim").unwrap();
        assert_eq!(l.flow, "reveal");
        assert_eq!(l.params.get("api").unwrap(), "https://kya.link");
        assert_eq!(l.params.get("type").unwrap(), "email_claim");
    }

    #[test]
    fn parse_no_query() {
        let l = parse("kya-sign://grant-delegate").unwrap();
        assert_eq!(l.flow, "grant-delegate");
        assert!(l.params.is_empty());
    }

    #[test]
    fn parse_percent_encoded() {
        let l = parse("kya-sign://email-claim?email=alice%40example.com").unwrap();
        assert_eq!(l.params.get("email").unwrap(), "alice@example.com");
    }

    #[test]
    fn parse_rejects_other_scheme() {
        assert!(parse("https://kya.link/x").is_err());
    }

    #[test]
    fn dispatch_twitter_drops_legacy_tweet_param() {
        // Magic-link `tweet=` is intentionally ignored since v0.3.2 —
        // the canonical path is the handoff URL emitted by claim-twitter,
        // and `--tweet-url` was removed from the binary.
        let l = parse("kya-sign://twitter-claim?api=http://x.test&tweet=https%3A%2F%2Fx.com%2Fa%2Fstatus%2F1")
            .unwrap();
        let cmd = dispatch_command(&l).unwrap().unwrap();
        assert!(cmd.contains("claim-twitter"));
        assert!(!cmd.contains("--tweet-url"));
    }

    #[test]
    fn dispatch_unknown() {
        let l = parse("kya-sign://make-coffee").unwrap();
        assert!(dispatch_command(&l).unwrap().is_none());
    }
}
