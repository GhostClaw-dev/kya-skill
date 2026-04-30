---
name: kya
version: 0.3.0
description: KYA — sign identity & matchmaking attestations, drive AWP relayer set-recipient / grant-delegate. Single-shot, event-driven; never loop.
platforms: [linux, macos]

trigger_keywords:
  - kya
  - know-your-agent
  - claim-twitter
  - claim-telegram
  - claim-email
  - kyc
  - reveal-attestation
  - set-recipient
  - grant-delegate
  - kya-sign
  - delegated-staking

bootstrap: ./scripts/bootstrap.sh
smoke_test: ./scripts/smoke_test.sh

metadata:
  hermes:
    tags: [identity, attestation, kya, awp]
    category: identity
    requires_toolsets: [terminal]
    required_environment_variables:
      - name: KYA_API_BASE
        prompt: KYA API base URL
        help: Default https://kya.link. Override only for staging or local dev.
        required_for: optional
      - name: AWP_RELAY_BASE
        prompt: AWP relayer base URL
        help: Default https://api.awp.sh. Used by set-recipient and grant-delegate.
        required_for: optional
      - name: BASE_RPC_URL
        prompt: Base mainnet RPC URL
        help: Default https://mainnet.base.org. Used to read AWPRegistry.nonces(user).
        required_for: optional
      - name: KYA_CHAIN_ID
        prompt: Chain ID
        help: Default 8453 (Base mainnet).
        required_for: optional
      - name: AWP_WALLET_TOKEN
        prompt: awp-wallet session token
        help: Only legacy awp-wallet (<v0.17) needs this. Newer versions auto-unlock.
        required_for: optional

  openclaw:
    bootstrap: ./scripts/bootstrap.sh
    smoke_test: ./scripts/smoke_test.sh
    requires:
      bins:
        - kya-agent
      anyBins:
        - awp-wallet
      env:
        - KYA_API_BASE
    primaryEnv: KYA_API_BASE
    emoji: "🪪"
    homepage: https://github.com/GhostClaw-dev/kya-skill
    install:
      - kind: script
        run: ./scripts/bootstrap.sh
    security:
      wallet_bridge:
        no_direct_key_access: true
        signed_payloads_only: true
        no_network_listeners: true
---

# KYA — Know Your Agent

You are an AI agent driving KYA on behalf of an agent owner. Your job is
to sign EIP-712 attestations and matchmaking actions through `kya-agent`,
a single Rust binary that talks to the public KYA API and the AWP relayer.

## Rules — read these first

1. **ALL operations go through `kya-agent`.** Never re-implement the flow
   in bash, python, curl, or any other tool. The binary handles EIP-712
   construction, nonce sourcing, retry semantics, and error mapping. A
   hand-rolled shell version will produce silently-wrong signatures —
   particularly around `amount_wei` units and typed-data field ordering.
2. **Never modify files on disk.** Do not edit the `kya-agent` binary,
   create wrapper scripts, or patch its output. If a command fails, read
   `error.code` and follow the recovery table below.
3. **Never expose secrets.** Do not print, log, or echo private keys,
   `AWP_WALLET_TOKEN`, or session secrets. Signing is delegated to
   `awp-wallet` — keys never enter `kya-agent`'s memory.
4. **Follow `_internal.next_command` exactly.** Every JSON result includes
   `_internal.next_action` and (when applicable) `_internal.next_command`.
   Run the suggested command verbatim. Do not paraphrase, reorder flags,
   or insert your own.
5. **One signing flow per invocation.** `kya-agent` is event-driven, not
   a daemon. Do not loop. Do not poll outside the binary's own polling.
6. **Never broadcast a transaction yourself.** `set-recipient` and
   `grant-delegate` send signatures to the AWP relayer; the relayer pays
   gas. The agent EOA needs zero ETH for any flow this skill handles.
7. **When in doubt, run `kya-agent preflight`.** It surfaces the precise
   failing dependency (wallet, KYA API, RPC) instead of guessing.
8. **Magic links → `kya-agent open <url>`.** When KYA web hands the user a
   `kya-sign://...` URL, do not translate query params into flags
   manually. The binary parses and dispatches; that's its only job.
9. **AWP registration is mandatory.** KYA is a subnet of AWP. If
   `kya-agent preflight` returns `AWP_NOT_REGISTERED`, do **not** attempt
   any KYA flow — hand off to [awp-skill](https://github.com/awp-core/awp-skill)
   for free gasless onboarding (one `setRecipient(self)` via relay) and
   only resume KYA after preflight returns `ready`.

## Prerequisites — AWP first

KYA is a subnet of the AWP network. Every flow assumes the agent EOA is
already registered on AWPRegistry. Two paths land here:

- ✅ **Came from awp-skill / awp.pro** (most users). Registration is
  done; `preflight` passes silently.
- ❌ **KYA-first**. `preflight` returns `AWP_NOT_REGISTERED` with
  `_internal.handoff.skill = "awp"`. Install awp-skill, run its
  onboarding (free, gasless), then re-run preflight.

`kya-agent` does **not** implement AWP onboarding itself — that lives
in awp-skill. Single source of truth, no duplication.

## Canonical journey — "I want delegated staking"

This is the dominant reason owners arrive at KYA: another worknet's
skill (predict, community, …) checked their stake, found it
insufficient, and bounced them here for KYA's delegated-staking service.
KYA stakes on their behalf if they pass at least one verification.

**Walk owners through these steps in order. Stop at every [STOP].**

### Step 0 — preflight

```sh
kya-agent preflight
```

- `_internal.next_action = "ready"` → continue to Step 1.
- `_internal.next_action = "register_on_awp"` → **[STOP]**: bounce to
  awp-skill onboarding (see Prerequisites). Resume only when preflight
  returns ready.
- Any other failure → surface `error.code` per the recovery table.

### Step 1 — query existing attestations

```sh
kya-agent attestations
```

Branches on `_internal.next_action`:

- **`ready_for_delegated_staking`** (any active twitter_claim /
  telegram_claim / email_claim / kyc) → **skip to Step 3.** Don't ask
  the owner to verify again — they already did.
- **`choose_verification`** (no active attestation) → continue to Step 2.

### Step 2 — owner picks a verification path

The `attestations` response carries `_internal.options` — the four
canonical methods. **[STOP]** — present them and let the owner choose.
**Never pick for them.**

```
You don't have any active KYA verification yet. Pick one:
  A) Twitter (X) — public tweet
  B) Telegram — public-channel post
  C) Email — 6-digit code (no public post)
  D) KYC — Didit selfie + ID (heavier, satisfies Human tier)

A/B/C give the Social tier; D gives the Human tier. Either is enough
for delegated staking.
```

After choice, run the matching command (the binary's `command` field).
For Twitter / Telegram / Email / KYC the binary returns a `handoff_url`:

```sh
kya-agent claim-twitter        # or claim-telegram / claim-email / kyc
# → outputs { handoff_url, _internal.next_action: "post_tweet_then_resubmit" } etc.
```

**[STOP]** — give the URL to the owner:

> Open this link in your browser: `<handoff_url>`. KYA web takes care
> of the rest — you don't need to paste anything back to me. When
> you're done, tell me and I'll continue.

After the owner says they're done, run `kya-agent attestations` again.
If the new attestation isn't active, **[STOP]** and ask the owner to
re-check the browser flow before retrying.

### Step 3 — execute delegated staking

**[STOP]** — confirm the amount. The worknet is fixed:

```
About to request delegated staking:
  agent      0xabc...
  worknet    845300000012   ← KYA's own subnet; ALWAYS this. Do not pass --worknet.
  amount     <N> AWP        ← owner picks; per-agent cap is 10 000 AWP

This will:
  1. Sign AWPRegistry.SetRecipient → relay broadcasts (gasless, no ETH).
  2. Sign KYA Action(delegated_staking_request) → KYA stakes from its pool.

Proceed?
```

KYA's delegated staking is always against KYA's own worknet
(`845300000012`); the binary defaults `--worknet` accordingly. **Do not
pass `--worknet` and do not ask the owner which worknet** — that's a
category mistake. Other worknets bouncing the owner to KYA are asking
for KYA-backed verification, not for KYA to stake into their pool.

After confirmation:

```sh
kya-agent set-recipient --amount <N>
```

The binary re-checks verification (defense-in-depth — the server gates
on this too) and, if green, runs both stages and polls for terminal
status.

### Step 4 — terminal status

| `_internal.next_action` | Action |
|---|---|
| `matched` | Report `matched_provider` and `matched_allocation_id`. Done. |
| `no_capacity` | **[STOP]**: tell owner no provider has free capacity right now. Surface verbatim — do not retry in a tight loop. |
| terminal `failed` with `failed_reason: per_agent_cap_exceeded` | **[STOP]**: this agent already has ≥10 000 AWP delegated-staked. Cannot stack more. |
| terminal `failed` (other) | **[STOP]**: surface `failed_reason` verbatim. |

For repeat / additive delegated staking (same agent, more AWP) or new
agents: same journey. Step 1 will skip to Step 3 if verification is
already active. The 10 000 AWP per-agent cap is enforced server-side.

## Quick start

```sh
# Install kya-agent (pre-built binary, no Rust toolchain required)
curl -fsSL https://raw.githubusercontent.com/GhostClaw-dev/kya-skill/main/install.sh | sh

# Sanity check
kya-agent preflight
```

`preflight` prints `_internal.next_action: ready` when everything is in
place. Otherwise it returns an `error.code` listed below.

## Magic links (canonical entry from KYA web)

KYA web encodes any user intent as a `kya-sign://...` URL. Always feed
the URL to `kya-agent open` — let the binary dispatch:

```sh
kya-agent open "kya-sign://reveal?api=https://kya.link&type=email_claim"
```

| URL form | Resolves to |
|---|---|
| `kya-sign://twitter-claim?api=<base>` | `claim-twitter` (handoff URL) |
| `kya-sign://twitter-claim?api=<base>&tweet=<url>` | `claim-twitter --tweet-url <url>` |
| `kya-sign://telegram-claim?api=<base>` | `claim-telegram` |
| `kya-sign://telegram-claim?api=<base>&message=<url>` | `claim-telegram --message-url <url>` |
| `kya-sign://email-claim?api=<base>` | `claim-email` (prompts for email + code) |
| `kya-sign://email-claim?api=<base>&email=<addr>` | `claim-email --email <addr>` |
| `kya-sign://kyc?api=<base>&owner=0x...` | `kyc --owner 0x...` |
| `kya-sign://reveal?api=<base>` | `reveal` (all types) |
| `kya-sign://reveal?api=<base>&type=<t>` | `reveal --type <t>` |
| `kya-sign://set-recipient?api=<base>` | `set-recipient` (stage 1 only — point recipient at KYA deposit) |
| `kya-sign://set-recipient?api=<base>&amount=<awp>` | `set-recipient --amount <awp>` (full delegated-staking; worknet defaults to 845300000012) |
| `kya-sign://grant-delegate` | `grant-delegate` |
| `kya-sign://sign?clip=1` | `sign --from-clipboard` |

Use `kya-agent open --dry-run <url>` if the user wants to see the
dispatched command before it runs.

## Subcommand reference

| Subcommand | Purpose |
|---|---|
| `preflight` | Self-check (awp-wallet, KYA reachable, RPC reachable, AWP registration). Run first. |
| `bootstrap` | First-run alias of `preflight` plus an onboarding hint. |
| `smoke-test` | Non-destructive probe — never signs, never POSTs. CI-safe. |
| `open <url>` | Parse `kya-sign://...` and dispatch. Use `--dry-run` to preview. |
| `attestations` | List active attestations + delegated-staking eligibility. Step 1 of the canonical journey. |
| `claim-twitter` | Sign and submit a Twitter (X) claim. TTY interactive: prompts for tweet URL. Piped: requires `--tweet-url`. |
| `claim-telegram` | Sign and submit a Telegram public-channel claim. `--message-url https://t.me/<channel>/<msg_id>`. |
| `claim-email` | Bind an email. Two signs sandwich a 6-digit code. TTY prompts; piped requires `--email --code`. |
| `kyc` | Sign `KycInit`, create a Didit session, return verification URL, optionally poll until terminal. |
| `reveal` | Off-chain. Sign `Action(attestation_reveal)`, get unredacted metadata. `--type email_claim/kyc/twitter_claim/telegram_claim/staking`. |
| `set-recipient` | Stage 1: gasless `AWPRegistry.setRecipient` via relayer. Stage 2 (with `--amount`): KYA `delegated_staking_request`. Pre-checks Social or Human attestation. |
| `grant-delegate` | Provider side: authorize `KyaAllocatorProxy` to allocate on your behalf, gasless via relayer. |
| `sign` | Generic EIP-712 signer for ad-hoc KYA / AWPRegistry payloads. `--from-file` / `--from-clipboard` / stdin. |
| `sign-action` | Single-shot KYA `Action` / `KycInit` signer for the wizard manual-paste UX. |

Every subcommand emits a single-line JSON result on stdout (with
`_internal.next_action` and optional `_internal.next_command`) and
streams progress on stderr as NDJSON `step` / `info` lines.

## Error codes → recovery actions

| `error.code` | Action |
|---|---|
| `AWP_NOT_REGISTERED` | Agent EOA isn't on AWPRegistry yet. KYA is a subnet of AWP — registration is mandatory. Hand off to [awp-skill](https://github.com/awp-core/awp-skill) onboarding (free, gasless). After it lands, re-run `kya-agent preflight`. |
| `WALLET_NOT_CONFIGURED` | `awp-wallet receive` to check; `awp-wallet init` only if no wallet exists. **Never re-init an existing wallet.** |
| `WALLET_LOCKED` | Re-run; the binary auto-unlocks. If it still fails: `awp-wallet unlock --scope transfer --duration 3600` and retry. |
| `AGENT_MISMATCH` | `awp-wallet wallets`, find the right profile, `export AWP_AGENT_ID=<id>` (or pass `--agent-id`), retry. |
| `TIMESTAMP_OUT_OF_RANGE` / `INVALID_SIGNATURE` | Local clock drift. `sudo sntp -sS time.apple.com` (macOS) / `w32tm /resync` (Windows) / `chronyc makestep` (Linux). Retry. |
| `EMAIL_INVALID` | Ask the user for a syntactically valid email and re-run. |
| `EMAIL_CODE_INVALID` | Re-read the inbox and re-run `kya-agent claim-email --email <addr> --code <CODE>`. |
| `EMAIL_MAX_ATTEMPTS` | 5 wrong codes — restart with a fresh `kya-agent claim-email`. |
| `EMAIL_RESEND_COOLDOWN` | Wait ~60 s and retry. |
| `NOT_VERIFIED` | `set-recipient --amount` requires Social or Human first. Run `kya-agent claim-twitter` or `kya-agent kyc`. |
| `PER_AGENT_CAP_EXCEEDED` | Agent already has ≥10 000 AWP delegated-staked. Cannot stack more. |
| `NO_CAPACITY` | No provider capacity right now. Surface verbatim — do not retry in a tight loop. |
| `STAKING_REQUEST_FAILED` | Read `failed_reason` and surface it verbatim. Do not retry blindly. |
| `RELAY_TX_REVERTED` | Check `tx_hash` on basescan. Usually stale nonce — just re-run; the binary re-reads `AWPRegistry.nonces(agent)`. |
| `KYA_UNREACHABLE` | `curl $KYA_API_BASE/api/healthz` to sanity-check. |
| `RPC_UNREACHABLE` | Set `BASE_RPC_URL` to a working endpoint and retry. |
| `INPUT_REQUIRED` | Non-TTY invocation missing a required flag. Re-run with the flag the message asks for (e.g. `--tweet-url`, `--email`, `--code`). |
| `MAGIC_LINK_INVALID` | Check the link is `kya-sign://...` and a known flow. |

For any error not in this table, surface `error.message` verbatim to the
user. Do not retry the same call in a tight loop hoping for a different
outcome.

## Pitfalls

- **Clock skew is the #1 cause of `INVALID_SIGNATURE`.** KYA accepts ±60 s
  future / 300 s past. If the user's clock is off, every sign attempt
  will fail until they resync — re-trying without a resync is futile.
- **`set-recipient --amount` requires verification first.** The binary
  pre-checks the agent has an active `twitter_claim` or `kyc` attestation
  before signing stage 1, so the user sees a clean "go run claim-twitter
  or kyc first" instead of burning a setRecipient tx that the matching
  worker would then reject.
- **`reveal` is off-chain.** It signs an `Action(attestation_reveal)` to
  authenticate the owner, but KYA writes nothing — only consumes the
  nonce and returns one unredacted response. Re-run for a fresh view.
- **Per-agent cap is 10 000 AWP across delegated stakers.** Re-running
  `set-recipient --amount` won't bypass it; the cap is enforced server-side
  at match time.
- **Telegram claim is public-channel only** (`t.me/<channel>/<msg_id>`).
  KYA fetches the public web preview; private DMs and unlisted groups
  cannot be verified.

## Security

- `kya-agent` never reads or writes a private key. Signing is delegated
  to `awp-wallet sign-typed-data`.
- Two domain shapes are signed — both pinned in `src/eip712.rs`:
  - `domain.name = "KYA"`, `primaryType ∈ {Action, KycInit}` — off-chain.
  - `domain.name = "AWPRegistry"`, `primaryType ∈ {SetRecipient, GrantDelegate}` —
    POSTed to AWP relayer; relayer broadcasts on Base.
- The binary never broadcasts a transaction itself. Network egress is
  limited to the configured KYA, relay, and RPC endpoints.
- Source: https://github.com/GhostClaw-dev/kya-skill — MIT, public.
  Releases are built from a tagged commit via GitHub Actions; the
  SHA256 of each binary is published in the release notes.
