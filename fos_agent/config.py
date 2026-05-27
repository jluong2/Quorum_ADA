"""
Runtime configuration for the FOS agent.
All values come from environment variables — nothing is hardcoded.
Set FOS_*_SCRIPT_HASH after deploying identity_registry/ to Cardano.
"""

import os

# ─── Network ──────────────────────────────────────────────

NETWORK = os.environ.get("CARDANO_NETWORK", "preprod")  # mainnet | preprod | preview

BLOCKFROST_PROJECT_ID = os.environ.get("BLOCKFROST_PROJECT_ID", "")

BLOCKFROST_URLS = {
    "mainnet": "https://cardano-mainnet.blockfrost.io/api/v0",
    "preprod": "https://cardano-preprod.blockfrost.io/api/v0",
    "preview":  "https://cardano-preview.blockfrost.io/api/v0",
}
BLOCKFROST_URL = BLOCKFROST_URLS.get(NETWORK, BLOCKFROST_URLS["preprod"])

# ─── Deployed Contract Addresses ──────────────────────────
# Script hashes are in plutus.json after `aiken build`.
# Convert to bech32 addresses with cardano-cli or lucid.

REGISTRY_SCRIPT_HASH   = os.environ.get("FOS_REGISTRY_SCRIPT_HASH",   "")
GOVERNANCE_SCRIPT_HASH = os.environ.get("FOS_GOVERNANCE_SCRIPT_HASH", "")
TREASURY_SCRIPT_HASH   = os.environ.get("FOS_TREASURY_SCRIPT_HASH",   "")

# ─── Agent Identity ───────────────────────────────────────
# The agent operates as a registered FOS member.
# SIGNING_KEY is the hex-encoded private key — keep it in a secret manager.

FOS_AGENT_KEY_HASH    = os.environ.get("FOS_AGENT_KEY_HASH",    "")
FOS_AGENT_SIGNING_KEY = os.environ.get("FOS_AGENT_SIGNING_KEY", "")
FOS_COLLATERAL_REF    = os.environ.get("FOS_COLLATERAL_REF",    "")  # "txhash#index"

# ─── Safety Limits ────────────────────────────────────────
# The agent will never autonomously approve a transfer larger than this,
# regardless of what governance passed.  Set to 0 to require human
# confirmation for every transfer.

MAX_AUTO_TRANSFER_LOVELACE = int(
    os.environ.get("FOS_MAX_AUTO_TRANSFER_LOVELACE", "5000000")  # 5 ADA default
)

# When True, the agent submits transactions without asking for confirmation.
AUTONOMOUS_MODE = os.environ.get("FOS_AUTONOMOUS_MODE", "false").lower() == "true"


def is_configured() -> bool:
    """Return True if all required env vars are set."""
    return all([
        BLOCKFROST_PROJECT_ID,
        REGISTRY_SCRIPT_HASH,
        GOVERNANCE_SCRIPT_HASH,
        TREASURY_SCRIPT_HASH,
        FOS_AGENT_KEY_HASH,
    ])
