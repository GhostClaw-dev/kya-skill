---
name: kya
version: 0.1.0
description: >
  KYA (Know Your Agent) — sign and submit identity attestations from your IDE.
  Use this skill when the user wants to claim an X (Twitter) account for an
  agent, run KYC for an agent, or sign any KYA EIP-712 payload using
  awp-wallet without copy-pasting JSON between browser and terminal.

  Handles the entire end-to-end flow:
    - Twitter claim: sign EIP-712 → call /v1/attestations/twitter/prepare →
      print the claim text and X intent URL → take the published tweet URL →
      sign and POST /v1/attestations/twitter/claim → poll attestation list
      until active or revoked.
    - KYC initiation: sign EIP-712 KycInit → POST /kyc/sessions → print Didit
      verification URL → poll the session until terminal status.
    - Generic signer: sign any EIP-712 typed-data JSON (from file, clipboard,
      or stdin) and emit the 0x signature for downstream tools.

  Trigger keywords: KYA, "Know Your Agent", kya-claim, kya-kyc, "claim X
  account for agent", "claim Twitter for agent", "Twitter claim KYA", "KYA
  Twitter sign", "agent X claim", "KYC for agent", "KYA sign", "sign KYA",
  "kya-sign://" (magic links from KYA web), "EIP-712 sign awp-wallet",
  "sign typed-data with awp-wallet" (when the typed-data domain.name is "KYA").

  NOT for: AWP allocations / staking / worknet management (use awp-skill),
  generic on-chain transactions, ERC-20 transfers, or any non-KYA EIP-712
  payload that is unrelated to agent identity attestations.
metadata:
  openclaw:
    requires:
      bins:
        - python3
      anyBins:
        - awp-wallet
      env:
        - KYA_API_BASE        # required for kya-claim flows; e.g. https://kya.link
        - KYA_KYC_BASE        # required for kya-kyc flows
        - KYA_CHAIN_ID        # optional; default 8453 (Base mainnet)
        - AWP_WALLET_TOKEN    # optional; only needed by older awp-wallet versions
    primaryEnv: KYA_API_BASE
    emoji: "🪪"
    homepage: https://github.com/GhostClaw-dev/kya-skill
    security:
      wallet_bridge:
        no_direct_key_access: true   # Uses awp-wallet sign-typed-data, never raw keys
        signed_payloads_only: true   # Only signs EIP-712 payloads with KYA-shaped domains
        no_network_listeners: true
---

# KYA — Know Your Agent

**Skill version: 0.1.0**

Skip the copy-paste-from-browser dance: this skill drives the KYA Twitter
Claim and KYC flows entirely from the user's IDE, using `awp-wallet` for the
EIP-712 signature. The KYA backend stays unchanged — every script just talks
to the public KYA HTTP endpoints with valid signed headers.

## When to use

Run a script from this skill whenever:

- The user pastes the URL `https://github.com/GhostClaw-dev/kya-skill` (or any
  message that mentions "the kya skill from github") into chat. This is the
  primary entry point — the KYA web "Skill mode · Copy prompt" button puts
  exactly this URL on the user's clipboard. If the skill isn't installed
  locally yet, run `git clone https://github.com/GhostClaw-dev/kya-skill
  ~/.cursor/skills/kya-skill` first, then proceed.
- The user is on the KYA web wizard (`/claim` or `/kyc`) and wants to skip
  the **Manual sign with awp-wallet** dialog. The dialog shows a
  one-click magic link / cursor prompt that resolves to one of the scripts
  here.
- The user pastes a `kya-sign://` magic link in chat.
- The user has KYA typed-data JSON and asks "please sign this with my wallet".

### Self-bootstrap from a GitHub URL

Cursor / Claude Code agents that don't have this skill installed should run:

```bash
git clone https://github.com/GhostClaw-dev/kya-skill ~/.cursor/skills/kya-skill
```

…and then re-read `SKILL.md` from the freshly cloned directory before
executing any script. This is the fully recommended way to use the skill —
the user only needs to paste a single GitHub URL plus environment variables.

## Requirements

- **Runtime**: `python3` (3.9+, stdlib only — no pip install)
- **Wallet**: `awp-wallet` CLI on `PATH`. Install from
  https://github.com/awp-core/awp-wallet (same binary used by awp-skill).
- **Env**:

  | Variable | Required | Default | Notes |
  |---|---|---|---|
  | `KYA_API_BASE` | for claim flows | — | e.g. `https://kya.link` |
  | `KYA_KYC_BASE` | for KYC flow | — | usually same host as `KYA_API_BASE` |
  | `KYA_CHAIN_ID` | no | `8453` | EIP-712 domain `chainId` (Base mainnet) |
  | `AWP_WALLET_TOKEN` | no | — | only legacy awp-wallet versions need it |

All scripts respect `--api-base` / `--chain-id` / `--token` to override env
values per invocation.

## Scripts

### S1 · Twitter claim end-to-end — `scripts/sign-claim.py`

Signs the two EIP-712 payloads, prints the claim text & X intent URL, takes
the published tweet URL on stdin (or via `--tweet-url`), submits the claim,
and polls until active.

```bash
# Interactive (prints claim text, waits for tweet URL on stdin):
KYA_API_BASE=https://kya.link \
python3 scripts/sign-claim.py

# Headless (already published the tweet):
python3 scripts/sign-claim.py \
  --tweet-url https://x.com/me/status/1234567890 \
  --no-poll
```

Outputs **JSON** on stdout when finished:

```json
{
  "agent_address": "0xabc...",
  "attestation_id": "att_01J...",
  "status": "active",
  "tweet_url": "https://x.com/me/status/1234567890",
  "metadata": { "twitter_handle": "alice_web3", "tweet_id": "1234567890" }
}
```

`stderr` carries human progress (`{"step": "sign.ok", ...}`, `{"info": "..."}`)
so wrappers can stream live status without parsing the final JSON line.

### S2 · KYC initiation — `scripts/sign-kyc.py`

Signs `KycInit`, creates a Didit session, prints the verification URL, then
polls the kyc-service until the session reaches a terminal status (Approved /
Declined / Abandoned / Expired).

```bash
KYA_KYC_BASE=https://kya.link \
python3 scripts/sign-kyc.py --owner 0xowner...
```

If the user wants to do the Didit selfie + ID step elsewhere (different
device), pass `--no-poll` so the script just prints the verification URL and
exits — they can come back to KYA web later to see the final attestation.

### S3 · Single-action signer — `scripts/sign-action.py` **(preferred for wizard dialogs)**

KYA web's `/claim` and `/kyc` wizards throw a `ManualSignatureRequiredError`
whenever the signer is in `manual` mode. The resulting dialog exposes a
**Copy prompt** button that embeds every parameter needed to reproduce the
wizard's typed-data (agent, nonce, timestamp, action — plus `owner` for
`kyc_init`). The user no longer has to copy a JSON blob; this script
rebuilds the typed-data from constants in `kya_lib` and asks
`awp-wallet` to sign it.

```bash
# Twitter claim step (prepare or claim):
python3 scripts/sign-action.py \
  --action twitter_prepare \
  --chain-id 8453 \
  --agent 0xabc... \
  --timestamp 1731000000 \
  --nonce bfc412331f93ca46e9ab9eae9986d165

# KYC init:
python3 scripts/sign-action.py \
  --action kyc_init \
  --chain-id 8453 \
  --agent 0xabc... \
  --owner 0xdef... \
  --timestamp 1731000000 \
  --nonce bfc412331f93ca46e9ab9eae9986d165
```

Outputs only the `0x` signature on stdout (logs on stderr). The user pastes
it back into KYA web's `03 Paste the 0x signature` field.

### S4 · Generic EIP-712 signer — `scripts/sign.py`

For any one-off KYA payload that doesn't fit S1 / S2 / S3 (e.g. KYA web shows a
custom typed-data in the manual-sign dialog).

```bash
# From file:
python3 scripts/sign.py --from-file typed.json

# From clipboard (user copied the JSON from KYA web):
python3 scripts/sign.py --from-clipboard

# From stdin (in a pipeline):
cat typed.json | python3 scripts/sign.py
```

Prints only the `0x...130hex` signature on stdout — easy to pipe into
`xsel`, append to a request body, etc.

## Magic link convention

KYA web encodes the user's intent as `kya-sign://<flow>?<query>`:

| Magic link | Skill action |
|---|---|
| `kya-sign://twitter-claim?api=<base>&chain=8453` | run `sign-claim.py --api-base <base> --chain-id 8453` |
| `kya-sign://twitter-claim?api=<base>&tweet=<url>` | run `sign-claim.py --api-base <base> --tweet-url <url>` |
| `kya-sign://kyc?api=<base>&owner=0x...` | run `sign-kyc.py --api-base <base> --owner 0x...` |
| `kya-sign://sign?clip=1` | run `sign.py --from-clipboard` |

When the user pastes such a URL in chat, this skill should:

1. Confirm the action with the user (one short sentence: "About to claim X
   account for agent `0xabc…` against `https://kya.link`. Proceed?").
2. Run the matching script with the encoded args.
3. Stream progress from stderr; report the final stdout JSON line back to
   the user.

## Security

- The skill never reads or writes a private key. Signing is delegated to
  `awp-wallet sign-typed-data`, which prompts the user for confirmation
  inside the wallet UI (per-wallet behavior).
- Every typed-data is constructed locally using the published KYA EIP-712
  schema (`web/lib/eip712.ts`, `api/src/crypto/eip712.ts`); nothing accepts
  arbitrary `domain.verifyingContract` from the wire.
- KYA's own backend re-recovers the signer with `viem.recoverTypedDataAddress`
  on every request, so a forged `agent_address` body claim is rejected
  before it reaches the database.
- `nonce` is 16 random bytes from `secrets.token_hex` (CSPRNG), regenerated
  per signed request, and rejected by KYA if reused for the same agent.
- `timestamp` is the local `time.time()` in unix seconds; KYA accepts a
  ±60 s future and 300 s past window. If the user's clock is wildly off,
  the script will surface `TIMESTAMP_OUT_OF_RANGE`.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `awp-wallet CLI not found in PATH` | binary not installed | install awp-wallet, restart shell |
| `[INVALID_SIGNATURE] twitter_prepare: ...` | clock skew | `w32tm /resync` (Windows) / `sudo sntp -sS time.apple.com` |
| `[AGENT_MISMATCH] ...` | `--agent` differs from the awp-wallet active EOA | omit `--agent` or switch wallets |
| `KYA API unreachable (...)` | wrong `KYA_API_BASE` / network | sanity-check with `curl <base>/api/healthz` |
| `aborted by user (no tweet URL provided)` | empty stdin in interactive mode | re-run and paste the URL when prompted, or pass `--tweet-url` |
