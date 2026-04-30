// Stable error code enum. Strings here MUST match the SKILL.md error table —
// agents key off them for recovery actions.

use serde_json::Value;
use std::fmt;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorKind {
    // Local environment
    WalletNotConfigured,
    WalletLocked,
    WalletInvalidOutput,
    AgentMismatch,
    InputRequired,
    MagicLinkInvalid,
    // EIP-712 / signing
    InvalidSignature,
    TimestampOutOfRange,
    // KYA API / business
    KyaUnreachable,
    EmailInvalid,
    EmailCodeInvalid,
    EmailMaxAttempts,
    EmailResendCooldown,
    NotVerified,
    PerAgentCapExceeded,
    NoCapacity,
    StakingRequestFailed,
    // AWP relay / chain
    RpcUnreachable,
    RelayUnreachable,
    RelayTxReverted,
    AwpNotRegistered,
    // Transport
    HttpError,
    // Catch-all
    KyaError,
    Internal,
}

impl ErrorKind {
    /// Stable string for stdout `error.code`. Do NOT rename.
    pub fn as_code(self) -> &'static str {
        match self {
            ErrorKind::WalletNotConfigured => "WALLET_NOT_CONFIGURED",
            ErrorKind::WalletLocked => "WALLET_LOCKED",
            ErrorKind::WalletInvalidOutput => "WALLET_INVALID_OUTPUT",
            ErrorKind::AgentMismatch => "AGENT_MISMATCH",
            ErrorKind::InputRequired => "INPUT_REQUIRED",
            ErrorKind::MagicLinkInvalid => "MAGIC_LINK_INVALID",
            ErrorKind::InvalidSignature => "INVALID_SIGNATURE",
            ErrorKind::TimestampOutOfRange => "TIMESTAMP_OUT_OF_RANGE",
            ErrorKind::KyaUnreachable => "KYA_UNREACHABLE",
            ErrorKind::EmailInvalid => "EMAIL_INVALID",
            ErrorKind::EmailCodeInvalid => "EMAIL_CODE_INVALID",
            ErrorKind::EmailMaxAttempts => "EMAIL_MAX_ATTEMPTS",
            ErrorKind::EmailResendCooldown => "EMAIL_RESEND_COOLDOWN",
            ErrorKind::NotVerified => "NOT_VERIFIED",
            ErrorKind::PerAgentCapExceeded => "PER_AGENT_CAP_EXCEEDED",
            ErrorKind::NoCapacity => "NO_CAPACITY",
            ErrorKind::StakingRequestFailed => "STAKING_REQUEST_FAILED",
            ErrorKind::RpcUnreachable => "RPC_UNREACHABLE",
            ErrorKind::RelayUnreachable => "RELAY_UNREACHABLE",
            ErrorKind::RelayTxReverted => "RELAY_TX_REVERTED",
            ErrorKind::AwpNotRegistered => "AWP_NOT_REGISTERED",
            ErrorKind::HttpError => "HTTP_ERROR",
            ErrorKind::KyaError => "KYA_ERROR",
            ErrorKind::Internal => "INTERNAL",
        }
    }

    /// Process exit code per SKILL.md.
    pub fn exit_code(self) -> i32 {
        match self {
            ErrorKind::InputRequired
            | ErrorKind::MagicLinkInvalid
            | ErrorKind::EmailInvalid => 2,
            ErrorKind::WalletNotConfigured
            | ErrorKind::WalletLocked
            | ErrorKind::WalletInvalidOutput
            | ErrorKind::AgentMismatch => 3,
            ErrorKind::KyaUnreachable
            | ErrorKind::EmailCodeInvalid
            | ErrorKind::EmailMaxAttempts
            | ErrorKind::EmailResendCooldown
            | ErrorKind::NotVerified
            | ErrorKind::PerAgentCapExceeded
            | ErrorKind::NoCapacity
            | ErrorKind::StakingRequestFailed
            | ErrorKind::InvalidSignature
            | ErrorKind::TimestampOutOfRange
            | ErrorKind::HttpError
            | ErrorKind::KyaError => 4,
            ErrorKind::RelayUnreachable | ErrorKind::RelayTxReverted => 5,
            ErrorKind::RpcUnreachable | ErrorKind::AwpNotRegistered => 6,
            ErrorKind::Internal => 1,
        }
    }
}

#[derive(Debug)]
pub struct KyaError {
    pub kind: ErrorKind,
    pub message: String,
    /// Server-side error.code as returned by KYA API, if any. Surfaced verbatim
    /// in stdout under `error.server_code` for SKILL.md error-table lookup.
    pub server_code: Option<String>,
    /// Recovery hint (one short sentence) for the calling agent.
    pub hint: Option<String>,
    /// Extra fields merged into the emitted `_internal` of the error JSON.
    /// Use this to carry `options`, `handoff`, `request_id`, etc. so the
    /// calling agent can branch without parsing freeform message text.
    pub extras: Option<Value>,
}

impl KyaError {
    pub fn new(kind: ErrorKind, message: impl Into<String>) -> Self {
        Self {
            kind,
            message: message.into(),
            server_code: None,
            hint: None,
            extras: None,
        }
    }

    pub fn with_hint(mut self, hint: impl Into<String>) -> Self {
        self.hint = Some(hint.into());
        self
    }

    pub fn with_server_code(mut self, code: impl Into<String>) -> Self {
        self.server_code = Some(code.into());
        self
    }

    pub fn with_extras(mut self, extras: Value) -> Self {
        self.extras = Some(extras);
        self
    }

    pub fn kind(&self) -> ErrorKind {
        self.kind
    }
}

impl fmt::Display for KyaError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "[{}] {}", self.kind.as_code(), self.message)
    }
}

impl std::error::Error for KyaError {}

impl From<reqwest::Error> for KyaError {
    fn from(e: reqwest::Error) -> Self {
        // Network-class failures land here. We classify in the call site via
        // KyaError::new — this conversion is the catch-all for places where
        // the caller doesn't care about subclass.
        KyaError::new(ErrorKind::HttpError, e.to_string())
    }
}

impl From<serde_json::Error> for KyaError {
    fn from(e: serde_json::Error) -> Self {
        KyaError::new(ErrorKind::Internal, format!("json: {e}"))
    }
}

impl From<std::io::Error> for KyaError {
    fn from(e: std::io::Error) -> Self {
        KyaError::new(ErrorKind::Internal, format!("io: {e}"))
    }
}

pub type Result<T> = std::result::Result<T, KyaError>;

/// Map a server-returned error.code (string) to our ErrorKind. Anything we
/// don't recognise stays as KyaError so SKILL.md's table can still match by
/// surfaced server_code.
pub fn map_server_code(code: &str) -> ErrorKind {
    match code {
        "EMAIL_INVALID" => ErrorKind::EmailInvalid,
        "EMAIL_CODE_INVALID" => ErrorKind::EmailCodeInvalid,
        "EMAIL_MAX_ATTEMPTS" => ErrorKind::EmailMaxAttempts,
        "EMAIL_RESEND_COOLDOWN" => ErrorKind::EmailResendCooldown,
        "AGENT_MISMATCH" => ErrorKind::AgentMismatch,
        "TIMESTAMP_OUT_OF_RANGE" => ErrorKind::TimestampOutOfRange,
        "INVALID_SIGNATURE" => ErrorKind::InvalidSignature,
        "PER_AGENT_CAP_EXCEEDED" => ErrorKind::PerAgentCapExceeded,
        "IDENTITY_REQUIRED" | "NOT_VERIFIED" => ErrorKind::NotVerified,
        _ => ErrorKind::KyaError,
    }
}
