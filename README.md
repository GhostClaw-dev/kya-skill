# kya-skill

> Sign and submit [KYA (Know Your Agent)](https://kya.link) attestations
> from your IDE. No more copying EIP-712 JSON between browser and terminal.

[![tests](https://img.shields.io/badge/tests-16%20passing-brightgreen)](./scripts/test_kya_lib.py)
[![python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![license](https://img.shields.io/badge/license-MIT-blue)](./LICENSE)

`kya-skill` is a [Cursor / Claude Code Agent Skill](https://docs.cursor.com/agent/skills)
that drives the entire KYA flow — Twitter claim, KYC initiation, generic
EIP-712 signing — by calling `awp-wallet sign-typed-data` locally. Your IDE
agent picks up the skill from a single GitHub URL.

---

## TL;DR — paste this into Cursor / Claude Code chat

```
Use the kya skill from https://github.com/GhostClaw-dev/kya-skill to claim my X account
for an agent. Install it first if it's missing:

git clone https://github.com/GhostClaw-dev/kya-skill ~/.cursor/skills/kya-skill

Then run:

KYA_API_BASE=https://kya.link \
python3 ~/.cursor/skills/kya-skill/scripts/sign-claim.py
```

Your agent will:

1. `git clone` this repo into `~/.cursor/skills/kya-skill/` (skip if exists)
2. Read `SKILL.md`, locate `scripts/sign-claim.py`
3. Resolve your agent EOA via `awp-wallet receive`
4. Sign the EIP-712 `Action(twitter_prepare)` payload
5. Print the claim text and an `https://twitter.com/intent/tweet?text=…` URL
6. Wait for you to publish the tweet, then take the URL on stdin
7. Sign `Action(twitter_claim)`, POST to KYA, poll until `active`

**Browser switches: 0. Manual copy-paste: 0.**

---

## What it replaces

The old "Manual sign with awp-wallet" dialog on KYA web required:

| Step | Action | Window |
|---|---|---|
| 1 | Copy EIP-712 typed-data JSON from browser | browser |
| 2 | Save to `typed.json` in terminal | terminal |
| 3 | `awp-wallet sign-typed-data --file typed.json` | terminal |
| 4 | Copy `0x…130hex` signature | terminal |
| 5 | Paste back into browser dialog | browser |
| 6 | Repeat for the second signature (claim) | × 2 |

After installing this skill, the same flow becomes one prompt in your IDE.

---

## Installation

### Option A · `git clone` (recommended)

```bash
git clone https://github.com/GhostClaw-dev/kya-skill ~/.cursor/skills/kya-skill
```

The skill is auto-discovered next time you reload Cursor / Claude Code.

### Option B · Inside the IDE itself

Just paste the prompt at the top of this README into chat. Most IDE agents
can run `git clone` themselves; they will self-install on first use.

### Update

```bash
cd ~/.cursor/skills/kya-skill && git pull
```

---

## Requirements

- **`python3` 3.9+** (stdlib only, no `pip install` required)
- **[`awp-wallet`](https://github.com/awp-core/awp-wallet) CLI** on `PATH`
- A KYA endpoint reachable from your machine (default `https://kya.link`)

| Env var | Required | Default | Notes |
|---|---|---|---|
| `KYA_API_BASE` | for claim flows | — | e.g. `https://kya.link` |
| `KYA_KYC_BASE` | for KYC flow | — | usually same host as `KYA_API_BASE` |
| `KYA_CHAIN_ID` | no | `8453` | EIP-712 domain `chainId` (Base mainnet) |
| `AWP_WALLET_TOKEN` | no | — | only legacy `awp-wallet` versions need it |

### Wallet lock? The skill handles it.

If your `awp-wallet` is a legacy version (or just timed out its session),
**you don't have to run `awp-wallet unlock` manually**. When a signing call
returns a "locked / token required" error, the skill automatically runs
`awp-wallet unlock --scope transfer --duration 3600`, caches the token in
`AWP_WALLET_TOKEN` for the remainder of the process, and retries once.

- Already have a token? Export `AWP_WALLET_TOKEN=<tok>` before the skill
  runs — it will be used first, and auto-unlock only kicks in if the token
  is rejected.
- The skill never sees your password or private key. Unlock prompts happen
  exclusively inside the official `awp-wallet` CLI.

---

## Scripts

| Script | What it does |
|---|---|
| [`scripts/sign-claim.py`](./scripts/sign-claim.py) | Twitter claim end-to-end: sign prepare → print claim text → take tweet URL → sign claim → poll attestation |
| [`scripts/sign-kyc.py`](./scripts/sign-kyc.py) | KYC initiation: sign `KycInit` → create Didit session → poll until terminal status |
| [`scripts/sign-action.py`](./scripts/sign-action.py) | Single-action signer: reads `--action / --agent / --timestamp / --nonce` (plus `--owner` for `kyc_init`), rebuilds KYA typed-data, prints the `0x` signature. Used by KYA web's Manual Sign dialog so users never copy a JSON blob. |
| [`scripts/sign.py`](./scripts/sign.py) | Generic EIP-712 signer: any typed-data JSON → `0x` signature (fallback only) |
| [`scripts/kya_lib.py`](./scripts/kya_lib.py) | Shared library: typed-data builders, `awp-wallet` bridge, KYA HTTP client |
| [`scripts/test_kya_lib.py`](./scripts/test_kya_lib.py) | 22 unit tests for the shared lib (validation, typed-data, HTTP, poller, wallet bridge, unlock/auto-retry) |
| [`scripts/test_sign_action.py`](./scripts/test_sign_action.py) | 7 subprocess tests for `sign-action.py`, use a fake `awp-wallet` on `PATH` |

Read [`SKILL.md`](./SKILL.md) for the full command reference, magic-link
convention, and security notes.

---

## How "paste a GitHub URL → auto-sign" actually works

KYA web's `/claim` and `/kyc` wizards have a **Skill mode** card in the left
rail. Clicking *Copy prompt →* puts a complete one-shot instruction onto
your clipboard:

```
Use the kya skill from https://github.com/GhostClaw-dev/kya-skill to run
the full Twitter claim flow for me.

If the skill isn't installed locally yet:
  git clone https://github.com/GhostClaw-dev/kya-skill ~/.cursor/skills/kya-skill

Then:
  KYA_API_BASE=<your-base> \
  python3 ~/.cursor/skills/kya-skill/scripts/sign-claim.py \
    --chain-id 8453 --agent 0x…
```

You paste it into Cursor / Claude Code chat. The agent recognizes the URL,
clones the repo if needed, then executes the script — driving `awp-wallet`
for the EIP-712 signature and the KYA API for prepare/claim/poll.

KYA web meanwhile keeps polling
`GET /v1/agents/:address/attestations` and surfaces the new attestation as
soon as the script completes. **Both halves can run on different machines**
(skill on your dev box, web on a colleague's screen) because the only thing
they share is the agent address.

---

## Security

- **No raw key access**: every signature is delegated to `awp-wallet
  sign-typed-data`. The skill process never sees a private key.
- **Hardcoded typed-data shape**: the skill rebuilds `domain` / `types` /
  `primaryType` from constants in `kya_lib.py`. A malicious caller cannot
  trick the skill into signing a payload that looks like KYA but is
  actually a different contract call.
- **Server is the source of truth**: KYA's backend re-recovers the signer
  with `viem.recoverTypedDataAddress` on every request and burns the nonce
  exactly once.
- **Tests guard the contract**: `scripts/test_kya_lib.py` and the matching
  `web/lib/eip712.test.ts` / `api/src/crypto/eip712.ts` test pin the field
  order & types — any drift breaks CI on at least two of the three sides.

---

## Development

```bash
# Run the bundled unit tests (stdlib only, no pip install):
python3 scripts/test_kya_lib.py

# Or via unittest:
python3 -m unittest discover -s scripts -p 'test_*.py'
```

Want to add a new flow (e.g. `bind-worknet`, `revoke-attestation`)? Drop a
new `scripts/<flow>.py` that imports from `kya_lib`, register it in
`SKILL.md` under a fresh `S<n>` heading, and bump the version banner.

---

## License

MIT — see [`LICENSE`](./LICENSE).
