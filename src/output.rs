// stdout / stderr protocol.
//
// stdout: ONE single-line JSON object per invocation, on success or failure.
//         Must include `_internal.next_action` (and optionally next_command)
//         to drive the calling agent without re-reading SKILL.md.
// stderr: NDJSON progress/info lines: {"step": "..."} or {"info": "..."}.

use crate::error::KyaError;
use serde::Serialize;
use serde_json::{json, Value};
use std::io::Write;

/// Emit a `step` line on stderr.
pub fn step(name: &str, fields: Value) {
    emit_stderr("step", name, fields);
}

/// Emit an `info` line on stderr.
pub fn info(message: &str, fields: Value) {
    emit_stderr("info", message, fields);
}

fn emit_stderr(key: &str, value: &str, fields: Value) {
    let mut payload = json!({ key: value });
    if let Value::Object(extras) = fields {
        if let Value::Object(map) = &mut payload {
            for (k, v) in extras {
                map.insert(k, v);
            }
        }
    }
    // Ignore lock-poisoning; this runs before exit.
    let _ = writeln!(std::io::stderr(), "{}", serde_json::to_string(&payload).unwrap_or_default());
    let _ = std::io::stderr().flush();
}

/// Print the success terminal JSON. Caller composes the body; this helper
/// just stamps `_internal` and prints to stdout.
pub fn ok<T: Serialize>(body: T, next_action: &str, next_command: Option<&str>) {
    let mut value = serde_json::to_value(&body).unwrap_or_else(|_| json!({}));
    let internal = match next_command {
        Some(cmd) => json!({ "next_action": next_action, "next_command": cmd }),
        None => json!({ "next_action": next_action }),
    };
    if let Value::Object(map) = &mut value {
        map.insert("_internal".to_string(), internal);
    } else {
        value = json!({ "result": value, "_internal": internal });
    }
    println!("{}", value);
}

/// Print the error terminal JSON. Always also writes a stderr summary.
pub fn emit_error(e: &KyaError) {
    let mut err_obj = json!({
        "code": e.kind.as_code(),
        "message": e.message,
    });
    if let Some(sc) = &e.server_code {
        err_obj["server_code"] = json!(sc);
    }
    if let Some(h) = &e.hint {
        err_obj["hint"] = json!(h);
    }
    let payload = json!({
        "ok": false,
        "error": err_obj,
        "_internal": {
            "next_action": "see_error_table",
            "next_command": "kya-agent preflight"
        }
    });
    println!("{}", payload);
    let _ = writeln!(std::io::stderr(), "{}", json!({ "error": e.to_string() }));
}
