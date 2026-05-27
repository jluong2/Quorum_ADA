"""
CIP-95 DRep (Delegated Representative) integration for the FOS governance agent.

The Quorum agent registers as a Cardano DRep using the agent's verification key
hash as the DRep credential.  Once registered, ADA holders can delegate their
on-chain voting power to the agent.

CIP-95:   https://cips.cardano.org/cip/CIP-0095
CIP-119:  https://cips.cardano.org/cip/CIP-0119  (DRep metadata anchor)

This module:
  - Builds DRep registration/retirement certificate data structures
  - Queries the agent's DRep status and delegators via Blockfrost
  - Returns typed results — signing is handled by signing.py / the wallet

Requires:  BLOCKFROST_PROJECT_ID + FOS_AGENT_KEY_HASH env vars.
Optional:  PyCardano (for bech32 DRep ID derivation).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import config


# ─── Types ────────────────────────────────────────────────

@dataclass
class DRepInfo:
    """Current on-chain DRep status."""
    drep_id: str           # bech32 drep1… or hex key hash
    registered: bool
    delegator_count: int
    voting_power: int      # lovelace delegated
    anchor_url: str = ""
    anchor_hash: str = ""
    last_active_epoch: Optional[int] = None
    is_active: bool = False


@dataclass
class DRepRegistrationTx:
    """Descriptor for a DRep registration certificate transaction."""
    drep_key_hash: str
    anchor_url: str
    anchor_hash: str        # blake2b-256 of metadata JSON, hex
    deposit_lovelace: int   # Cardano protocol minimum (500 ADA on mainnet)
    description: str = ""


@dataclass
class DRepVote:
    """A single governance vote cast by the DRep."""
    gov_action_id: str       # tx_hash#index of the governance action
    vote: str                # "yes" | "no" | "abstain"
    anchor_url: str = ""
    anchor_hash: str = ""


# ─── Constants ────────────────────────────────────────────

# CIP-95 DRep deposit: 500 ADA on mainnet, 2 ADA on preprod/preview
DREP_DEPOSIT_MAINNET = 500_000_000
DREP_DEPOSIT_PREPROD = 2_000_000


def _drep_deposit() -> int:
    return DREP_DEPOSIT_PREPROD if config.NETWORK != "mainnet" else DREP_DEPOSIT_MAINNET


# ─── DRep ID derivation ───────────────────────────────────

def drep_id_from_key_hash(key_hash: str) -> str:
    """
    Derive the bech32 DRep ID from a verification key hash.

    CIP-95: DRep credential = VerificationKey hash.
    bech32 prefix: drep1 (key hash credential).
    Falls back to hex if PyCardano is unavailable.
    """
    try:
        from pycardano import VerificationKeyHash
        from pycardano.crypto.bech32 import encode as bech32_encode
        vkh_bytes = bytes.fromhex(key_hash)
        return bech32_encode("drep", vkh_bytes)
    except Exception:
        return f"drep1_{key_hash[:16]}…"  # readable fallback


# ─── Blockfrost queries ───────────────────────────────────

def _bf_get(path: str) -> dict | list:
    project_id = config.BLOCKFROST_PROJECT_ID
    if not project_id:
        return _mock_drep_response(path)
    try:
        import requests
        url = f"{config.BLOCKFROST_URL.rstrip('/')}/{path.lstrip('/')}"
        resp = requests.get(
            url,
            headers={"project_id": project_id},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        raise RuntimeError(f"Blockfrost query failed: {e}") from e


def query_drep_status(key_hash: str) -> DRepInfo:
    """
    Query the current DRep registration status and voting power.
    Returns a DRepInfo with registered=False if the agent is not yet registered.
    """
    drep_id = drep_id_from_key_hash(key_hash)
    try:
        data = _bf_get(f"governance/dreps/{key_hash}")
        return DRepInfo(
            drep_id=drep_id,
            registered=True,
            delegator_count=int(data.get("amount", {}).get("delegator_count", 0)),
            voting_power=int(data.get("amount", {}).get("lovelace", 0)),
            anchor_url=data.get("anchor", {}).get("url", ""),
            anchor_hash=data.get("anchor", {}).get("data_hash", ""),
            last_active_epoch=data.get("last_active_epoch"),
            is_active=data.get("active", False),
        )
    except RuntimeError:
        # 404 = not registered
        return DRepInfo(
            drep_id=drep_id,
            registered=False,
            delegator_count=0,
            voting_power=0,
        )


def query_drep_votes(key_hash: str, limit: int = 10) -> list[dict]:
    """Return the most recent governance votes cast by this DRep."""
    try:
        data = _bf_get(f"governance/dreps/{key_hash}/votes?order=desc&count={limit}")
        return list(data) if isinstance(data, list) else []
    except RuntimeError:
        return []


# ─── Registration certificate builder ────────────────────

def build_drep_registration(
    key_hash: str,
    anchor_url: str,
    anchor_hash: str,
) -> DRepRegistrationTx:
    """
    Build a DRep registration descriptor.

    The caller passes this to signing.py (PyCardano) which assembles the
    CIP-95 DRep registration certificate and submits it to the chain.

    anchor_url:  URL where the CIP-119 metadata JSON is hosted
    anchor_hash: blake2b-256 hash of that JSON (hex)
    """
    return DRepRegistrationTx(
        drep_key_hash=key_hash,
        anchor_url=anchor_url,
        anchor_hash=anchor_hash,
        deposit_lovelace=_drep_deposit(),
        description=(
            f"Register FOS agent as DRep — "
            f"anchor: {anchor_url[:40]}…"
            if len(anchor_url) > 40 else anchor_url
        ),
    )


def build_drep_retirement(key_hash: str) -> DRepRegistrationTx:
    """Build a DRep retirement certificate descriptor."""
    return DRepRegistrationTx(
        drep_key_hash=key_hash,
        anchor_url="",
        anchor_hash="",
        deposit_lovelace=0,
        description="Retire FOS agent DRep",
    )


# ─── CIP-119 metadata helper ─────────────────────────────

def generate_drep_metadata(
    name: str = "Quorum FOS Governance Agent",
    motivation: str = "Autonomous on-chain governance operator for Quorum protocol DAOs.",
    rationale: str = "",
    references: list[dict] | None = None,
) -> dict:
    """
    Generate a CIP-119 compliant DRep metadata object.

    Upload the returned JSON to IPFS or a public HTTPS endpoint, then pass
    the URL + blake2b-256 hash to build_drep_registration().
    """
    return {
        "@context": {
            "@language": "en-us",
            "CIP100": "https://github.com/cardano-foundation/CIPs/blob/master/CIP-0100/README.md#",
            "CIP119": "https://github.com/cardano-foundation/CIPs/blob/master/CIP-0119/README.md#",
            "hashAlgorithm": "CIP100:hashAlgorithm",
            "body": {
                "@id": "CIP119:body",
                "@context": {
                    "givenName":   "CIP119:givenName",
                    "motivations": "CIP119:motivations",
                    "objectives":  "CIP119:objectives",
                    "qualifications": "CIP119:qualifications",
                    "references":  {"@id": "CIP119:references", "@container": "@set"},
                },
            },
        },
        "hashAlgorithm": "blake2b-256",
        "body": {
            "givenName": name,
            "motivations": motivation,
            "objectives": (
                "Evaluate Quorum governance proposals according to the eight safety rules "
                "encoded in the agent system prompt; vote yes when rules are satisfied, "
                "no (or abstain) when they are not."
            ),
            "qualifications": (
                "Powered by Claude (Anthropic). "
                "Reads on-chain state via Blockfrost. "
                "Open-source at github.com/jluong2/Quorum_ADA."
            ),
            "references": references or [
                {
                    "@type": "Other",
                    "label": "Quorum Protocol Source",
                    "uri": "https://github.com/jluong2/Quorum_ADA",
                },
            ],
        },
    }


# ─── Mock data ────────────────────────────────────────────

def _mock_drep_response(path: str) -> dict | list:
    if "votes" in path:
        return [
            {
                "tx_hash": "aaaa" + "0" * 60,
                "cert_index": 0,
                "vote": "yes",
                "gov_action_tx_hash": "bbbb" + "0" * 60,
                "gov_action_index": 0,
            }
        ]
    return {
        "drep_id": "drep1_mock",
        "hex": "00" * 28,
        "amount": {"lovelace": "1200000000", "delegator_count": 3},
        "active": True,
        "last_active_epoch": 480,
        "anchor": {
            "url": "https://example.com/drep-metadata.json",
            "data_hash": "aa" * 32,
        },
    }
