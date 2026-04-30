// KYA HTTP client. Mirrors kya_lib.py endpoints 1:1.

use crate::error::{map_server_code, ErrorKind, KyaError, Result};
use crate::version::USER_AGENT;
use once_cell::sync::Lazy;
use reqwest::blocking::{Client, RequestBuilder, Response};
use reqwest::header::{ACCEPT, CONTENT_TYPE};
use reqwest::Method;
use serde_json::{json, Value};
use std::time::Duration;

static HTTP: Lazy<Client> = Lazy::new(|| {
    Client::builder()
        .user_agent(USER_AGENT)
        .timeout(Duration::from_secs(20))
        .build()
        .expect("reqwest client builds")
});

pub fn http() -> &'static Client {
    &HTTP
}

#[derive(Clone, Copy)]
pub struct SignedHeaders<'a> {
    pub signature: &'a str,
    pub timestamp: u64,
    pub nonce: &'a str,
}

fn signed(req: RequestBuilder, headers: Option<SignedHeaders<'_>>) -> RequestBuilder {
    if let Some(h) = headers {
        req.header("X-Agent-Signature", h.signature)
            .header("X-Agent-Timestamp", h.timestamp.to_string())
            .header("X-Agent-Nonce", h.nonce)
    } else {
        req
    }
}

fn read_response(resp: Result<Response>, action: &str) -> Result<(u16, Value)> {
    let resp = resp?;
    let status = resp.status();
    let body = resp.text().map_err(KyaError::from)?;
    let parsed: Value = if body.trim().is_empty() {
        json!({})
    } else {
        serde_json::from_str(&body).unwrap_or(json!({
            "error": { "code": "HTTP_ERROR", "message": &body[..body.len().min(200)] }
        }))
    };
    let _ = action;
    Ok((status.as_u16(), parsed))
}

fn check(status: u16, payload: Value, action: &str) -> Result<Value> {
    if (200..300).contains(&status) {
        return Ok(payload);
    }
    let err = payload.get("error").cloned().unwrap_or(json!({}));
    let code = err
        .get("code")
        .and_then(|x| x.as_str())
        .unwrap_or("HTTP_ERROR")
        .to_string();
    let msg = err
        .get("message")
        .and_then(|x| x.as_str())
        .unwrap_or(&format!("{action} failed with HTTP {status}"))
        .to_string();
    let kind = map_server_code(&code);
    Err(KyaError::new(kind, format!("{action}: {msg}")).with_server_code(code))
}

fn http_request(
    method: Method,
    url: &str,
    headers: Option<SignedHeaders<'_>>,
    body: Option<&Value>,
    action: &str,
) -> Result<Value> {
    let mut req = HTTP
        .request(method, url)
        .header(ACCEPT, "application/json");
    if let Some(b) = body {
        req = req.header(CONTENT_TYPE, "application/json").body(
            serde_json::to_vec(b).map_err(KyaError::from)?,
        );
    }
    req = signed(req, headers);
    let resp = req.send().map_err(|e| {
        KyaError::new(ErrorKind::KyaUnreachable, format!("KYA API unreachable ({url}): {e}"))
    });
    let (status, payload) = read_response(resp, action)?;
    check(status, payload, action)
}

pub fn prepare_twitter(
    api_base: &str,
    agent_address: &str,
    headers: SignedHeaders<'_>,
) -> Result<Value> {
    http_request(
        Method::POST,
        &format!("{api_base}/v1/attestations/twitter/prepare"),
        Some(headers),
        Some(&json!({ "agent_address": agent_address })),
        "twitter_prepare",
    )
}

pub fn claim_twitter(
    api_base: &str,
    agent_address: &str,
    tweet_url: &str,
    claim_nonce: &str,
    headers: SignedHeaders<'_>,
) -> Result<Value> {
    http_request(
        Method::POST,
        &format!("{api_base}/v1/attestations/twitter/claim"),
        Some(headers),
        Some(&json!({
            "agent_address": agent_address,
            "tweet_url": tweet_url,
            "nonce": claim_nonce,
        })),
        "twitter_claim",
    )
}

pub fn prepare_telegram(
    api_base: &str,
    agent_address: &str,
    headers: SignedHeaders<'_>,
) -> Result<Value> {
    http_request(
        Method::POST,
        &format!("{api_base}/v1/attestations/telegram/prepare"),
        Some(headers),
        Some(&json!({ "agent_address": agent_address })),
        "telegram_prepare",
    )
}

pub fn claim_telegram(
    api_base: &str,
    agent_address: &str,
    message_url: &str,
    claim_nonce: &str,
    headers: SignedHeaders<'_>,
) -> Result<Value> {
    http_request(
        Method::POST,
        &format!("{api_base}/v1/attestations/telegram/claim"),
        Some(headers),
        Some(&json!({
            "agent_address": agent_address,
            "message_url": message_url,
            "nonce": claim_nonce,
        })),
        "telegram_claim",
    )
}

pub fn prepare_email(
    api_base: &str,
    agent_address: &str,
    email: &str,
    headers: SignedHeaders<'_>,
) -> Result<Value> {
    http_request(
        Method::POST,
        &format!("{api_base}/v1/attestations/email/prepare"),
        Some(headers),
        Some(&json!({ "agent_address": agent_address, "email": email })),
        "email_prepare",
    )
}

pub fn confirm_email(
    api_base: &str,
    agent_address: &str,
    email: &str,
    code: &str,
    headers: SignedHeaders<'_>,
) -> Result<Value> {
    http_request(
        Method::POST,
        &format!("{api_base}/v1/attestations/email/confirm"),
        Some(headers),
        Some(&json!({
            "agent_address": agent_address,
            "email": email,
            "code": code,
        })),
        "email_confirm",
    )
}

pub fn list_attestations(
    api_base: &str,
    agent_address: &str,
    type_filter: Option<&str>,
) -> Result<Value> {
    let qs = type_filter
        .map(|t| format!("?type={t}"))
        .unwrap_or_default();
    http_request(
        Method::GET,
        &format!("{api_base}/v1/agents/{agent_address}/attestations{qs}"),
        None,
        None,
        "list_attestations",
    )
}

pub fn reveal_attestations(
    api_base: &str,
    agent_address: &str,
    type_filter: Option<&str>,
    headers: SignedHeaders<'_>,
) -> Result<Value> {
    let mut body = json!({ "agent_address": agent_address });
    if let Some(t) = type_filter {
        body["type"] = json!(t);
    }
    http_request(
        Method::POST,
        &format!("{api_base}/v1/agents/{agent_address}/attestations/reveal"),
        Some(headers),
        Some(&body),
        "attestation_reveal",
    )
}

pub fn deposit_address(api_base: &str, agent_address: &str, worknet_id: &str) -> Result<Value> {
    let qs = if worknet_id.is_empty() {
        String::new()
    } else {
        format!("?worknet_id={worknet_id}")
    };
    http_request(
        Method::GET,
        &format!("{api_base}/v1/agents/{agent_address}/deposit-address{qs}"),
        None,
        None,
        "deposit_address",
    )
}

pub fn request_delegated_staking(
    api_base: &str,
    agent_address: &str,
    amount_wei: &str,
    worknet_id: &str,
    headers: SignedHeaders<'_>,
) -> Result<Value> {
    if amount_wei.is_empty()
        || amount_wei == "0"
        || !amount_wei.chars().all(|c| c.is_ascii_digit())
    {
        return Err(KyaError::new(
            ErrorKind::InputRequired,
            format!("amount_wei must be a positive integer string, got {amount_wei:?}"),
        ));
    }
    http_request(
        Method::POST,
        &format!("{api_base}/v1/services/staking/request"),
        Some(headers),
        Some(&json!({
            "agent_address": agent_address,
            "amount_wei": amount_wei,
            "worknet_id": worknet_id,
        })),
        "delegated_staking_request",
    )
}

pub fn list_staking_requests(api_base: &str, agent_address: &str) -> Result<Vec<Value>> {
    let v = http_request(
        Method::GET,
        &format!(
            "{api_base}/v1/services/staking/requests?agent_address={agent_address}"
        ),
        None,
        None,
        "list_staking_requests",
    )?;
    Ok(v.get("items")
        .and_then(|x| x.as_array())
        .cloned()
        .unwrap_or_default())
}

pub fn kyc_create_session(
    kyc_base: &str,
    agent_address: &str,
    owner_address: &str,
    headers: SignedHeaders<'_>,
) -> Result<Value> {
    http_request(
        Method::POST,
        &format!("{kyc_base}/kyc/sessions"),
        Some(headers),
        Some(&json!({
            "agent_address": agent_address,
            "owner_address": owner_address,
        })),
        "kyc_create_session",
    )
}

pub fn kyc_get_session(kyc_base: &str, session_id: &str) -> Result<Value> {
    http_request(
        Method::GET,
        &format!("{kyc_base}/kyc/sessions/{session_id}"),
        None,
        None,
        "kyc_get_session",
    )
}

/// Lightweight reachability probe — used by preflight.
pub fn ping(api_base: &str) -> Result<()> {
    let url = format!("{api_base}/v1/health");
    let resp = HTTP
        .get(&url)
        .timeout(Duration::from_secs(5))
        .send()
        .map_err(|e| {
            KyaError::new(
                ErrorKind::KyaUnreachable,
                format!("KYA API unreachable ({api_base}): {e}"),
            )
        })?;
    let status = resp.status().as_u16();
    if (200..500).contains(&status) {
        // 4xx still proves the server is reachable.
        return Ok(());
    }
    Err(KyaError::new(
        ErrorKind::KyaUnreachable,
        format!("KYA API ping returned {status}"),
    ))
}
