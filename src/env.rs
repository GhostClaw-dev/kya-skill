// Endpoint defaults + env resolution. Mirrors kya_lib.py constants.

use std::env;

pub const DEFAULT_KYA_API_BASE: &str = "https://kya.link";
pub const DEFAULT_KYA_KYC_BASE: &str = "https://kya.link";
pub const DEFAULT_KYA_WEB_BASE: &str = "https://kya.link";
pub const DEFAULT_AWP_RELAY_BASE: &str = "https://api.awp.sh";
pub const DEFAULT_BASE_RPC_URL: &str = "https://mainnet.base.org";

pub const DEFAULT_KYA_WORKNET_ID: &str = "845300000012";

/// Hard-coded — skill only signs typed-data targeting these contracts.
pub const AWP_REGISTRY_ADDRESS: &str = "0x0000F34Ed3594F54faABbCb2Ec45738DDD1c001A";
pub const KYA_ALLOCATOR_PROXY_ADDRESS: &str = "0xD544E5A2EF9100d3BD2fB7CffD2a4f7C773a1963";

pub const KYA_DOMAIN_NAME: &str = "KYA";
pub const KYA_DOMAIN_VERSION: &str = "1";
pub const AWP_REGISTRY_DOMAIN_NAME: &str = "AWPRegistry";
pub const AWP_REGISTRY_DOMAIN_VERSION: &str = "1";

pub fn resolve_api_base(flag: &str) -> String {
    let s = if !flag.is_empty() {
        flag.to_string()
    } else {
        env::var("KYA_API_BASE").unwrap_or_default()
    };
    let s = s.trim_end_matches('/').to_string();
    if s.is_empty() {
        DEFAULT_KYA_API_BASE.to_string()
    } else {
        s
    }
}

pub fn resolve_web_base(flag: &str) -> String {
    let s = if !flag.is_empty() {
        flag.to_string()
    } else {
        env::var("KYA_WEB_BASE").unwrap_or_default()
    };
    let s = s.trim_end_matches('/').to_string();
    if s.is_empty() {
        DEFAULT_KYA_WEB_BASE.to_string()
    } else {
        s
    }
}

pub fn resolve_kyc_base() -> String {
    let s = env::var("KYA_KYC_BASE").unwrap_or_default();
    let s = s.trim_end_matches('/').to_string();
    if s.is_empty() {
        DEFAULT_KYA_KYC_BASE.to_string()
    } else {
        s
    }
}

pub fn resolve_relay_base() -> String {
    let s = env::var("AWP_RELAY_BASE").unwrap_or_default();
    let s = s.trim_end_matches('/').to_string();
    if s.is_empty() {
        DEFAULT_AWP_RELAY_BASE.to_string()
    } else {
        s
    }
}

pub fn resolve_rpc_url() -> String {
    let s = env::var("BASE_RPC_URL").unwrap_or_default();
    let s = s.trim_end_matches('/').to_string();
    if s.is_empty() {
        DEFAULT_BASE_RPC_URL.to_string()
    } else {
        s
    }
}
