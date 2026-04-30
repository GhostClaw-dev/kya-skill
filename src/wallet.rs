// awp-wallet subprocess bridge.
//
// We never touch the user's private key. Every signing operation goes through
// `awp-wallet sign-typed-data --data <json>`. Locked-wallet detection +
// auto-unlock matches kya_lib.py behaviour.

use crate::address::{validate_address, validate_signature};
use crate::error::{ErrorKind, KyaError, Result};
use crate::output;
use serde_json::{json, Value};
use std::process::{Command, Stdio};

/// Hints used to decide "wallet needs unlock" vs "command actually failed".
const LOCK_HINTS: &[&str] = &[
    "locked",
    "unlocked",
    "unauthoriz",
    "token required",
    "missing token",
    "invalid token",
    "session expired",
    "no session",
    "--token",
];

fn awp_wallet_bin() -> Result<String> {
    if which("awp-wallet").is_some() {
        return Ok("awp-wallet".to_string());
    }
    Err(KyaError::new(
        ErrorKind::WalletNotConfigured,
        "awp-wallet CLI not found in PATH",
    )
    .with_hint(
        "Install awp-wallet from https://github.com/awp-core/awp-wallet, then re-run.",
    ))
}

fn which(name: &str) -> Option<String> {
    let path_var = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&path_var) {
        let candidate = dir.join(name);
        if candidate.is_file() {
            return Some(candidate.to_string_lossy().into_owned());
        }
        // Windows: also try .exe
        let exe = dir.join(format!("{name}.exe"));
        if exe.is_file() {
            return Some(exe.to_string_lossy().into_owned());
        }
    }
    None
}

fn looks_like_lock_error(stderr: &str, stdout: &str) -> bool {
    let blob = format!("{stderr}\n{stdout}").to_lowercase();
    LOCK_HINTS.iter().any(|h| blob.contains(h))
}

fn raw_exec(args: &[&str]) -> Result<(i32, String, String)> {
    let bin = awp_wallet_bin()?;
    let out = Command::new(&bin)
        .args(args)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .map_err(|e| {
            KyaError::new(
                ErrorKind::WalletNotConfigured,
                format!("awp-wallet spawn failed: {e}"),
            )
        })?;
    Ok((
        out.status.code().unwrap_or(-1),
        String::from_utf8_lossy(&out.stdout).trim().to_string(),
        String::from_utf8_lossy(&out.stderr).trim().to_string(),
    ))
}

fn extract_unlock_token(stdout: &str) -> Option<String> {
    let raw = stdout.trim();
    if raw.is_empty() {
        return None;
    }
    // Try JSON shapes used by various awp-wallet versions.
    if let Ok(Value::Object(map)) = serde_json::from_str::<Value>(raw) {
        for key in ["sessionToken", "token", "accessToken"] {
            if let Some(Value::String(s)) = map.get(key) {
                if !s.trim().is_empty() {
                    return Some(s.trim().to_string());
                }
            }
        }
        for (k, v) in &map {
            if let Value::String(s) = v {
                if !s.trim().is_empty() && k.to_lowercase().contains("token") {
                    return Some(s.trim().to_string());
                }
            }
        }
        return None;
    }
    // Very-old `unlock --raw` form.
    Some(raw.to_string())
}

pub fn unlock(scope: &str, duration_sec: u64) -> Result<String> {
    output::step(
        "wallet.unlock",
        json!({ "scope": scope, "duration_sec": duration_sec }),
    );
    let dur = duration_sec.to_string();
    let (code, stdout, stderr) =
        raw_exec(&["unlock", "--scope", scope, "--duration", &dur])?;
    if code != 0 {
        let msg = if !stderr.is_empty() { stderr } else { stdout };
        return Err(KyaError::new(
            ErrorKind::WalletLocked,
            format!("awp-wallet unlock failed: {msg}"),
        )
        .with_hint("If your wallet is not initialised, run `awp-wallet init` first."));
    }
    let token = extract_unlock_token(&stdout).ok_or_else(|| {
        KyaError::new(
            ErrorKind::WalletInvalidOutput,
            format!("awp-wallet unlock returned no token (stdout={stdout:?})"),
        )
    })?;
    std::env::set_var("AWP_WALLET_TOKEN", &token);
    output::info("wallet unlocked", json!({ "scope": scope }));
    Ok(token)
}

fn call_with_autounlock(
    args: &[&str],
    token: &str,
    purpose: &str,
) -> Result<String> {
    let env_token = std::env::var("AWP_WALLET_TOKEN").unwrap_or_default();
    let attempt = if !token.is_empty() { token } else { &env_token };
    let mut first = args.to_vec();
    if !attempt.is_empty() {
        first.push("--token");
        first.push(attempt);
    }
    let (code, stdout, stderr) = raw_exec(&first)?;
    if code == 0 {
        return Ok(stdout);
    }
    if !looks_like_lock_error(&stderr, &stdout) {
        let msg = if !stderr.is_empty() { stderr } else { stdout };
        return Err(KyaError::new(
            ErrorKind::WalletNotConfigured,
            format!("awp-wallet {} failed during {purpose}: {msg}", args[0]),
        ));
    }

    output::info(
        "awp-wallet locked; unlocking automatically",
        json!({ "purpose": purpose }),
    );
    let fresh = unlock("transfer", 3600)?;
    let mut second = args.to_vec();
    second.push("--token");
    second.push(&fresh);
    let (code2, stdout2, stderr2) = raw_exec(&second)?;
    if code2 != 0 {
        let msg = if !stderr2.is_empty() { stderr2 } else { stdout2 };
        return Err(KyaError::new(
            ErrorKind::WalletLocked,
            format!(
                "awp-wallet {} still failed after unlock during {purpose}: {msg}",
                args[0]
            ),
        ));
    }
    Ok(stdout2)
}

pub fn get_address(token: &str) -> Result<String> {
    let stdout = call_with_autounlock(&["receive"], token, "get_wallet_address")?;
    let v: Value = serde_json::from_str(&stdout).map_err(|_| {
        KyaError::new(
            ErrorKind::WalletInvalidOutput,
            format!("awp-wallet receive returned non-JSON output: {stdout:?}"),
        )
    })?;
    let addr = v.get("eoaAddress").and_then(|x| x.as_str()).unwrap_or("");
    validate_address(addr, "wallet address")
}

pub fn sign_typed_data(typed_data: &Value, token: &str) -> Result<String> {
    let json_str = serde_json::to_string(typed_data)?;
    let stdout = call_with_autounlock(
        &["sign-typed-data", "--data", &json_str],
        token,
        "sign_typed_data",
    )?;
    let v: Value = serde_json::from_str(&stdout).map_err(|_| {
        KyaError::new(
            ErrorKind::WalletInvalidOutput,
            format!("awp-wallet sign-typed-data returned non-JSON output: {stdout:?}"),
        )
    })?;
    let sig = v.get("signature").and_then(|x| x.as_str()).unwrap_or("");
    validate_signature(sig)?;
    Ok(sig.to_string())
}

pub fn is_present() -> bool {
    which("awp-wallet").is_some()
}
