#!/usr/bin/env python3
"""
Publish CIP-171 on-chain bytecode verification for Quorum contracts.

CIP-171 links deployed script hashes to their source code on-chain so anyone
can independently verify that the contracts match the reviewed source without
trusting the deployer.

After running this, anyone can:
  1. Clone the repo at the pinned commit
  2. Run: cd identity_registry && aiken build
  3. Compare the resulting script hashes with what's on-chain
  4. They will match — cryptographic proof the source is honest

The verification record is published as transaction metadata (label 1984)
and lives permanently on the Cardano blockchain.

Prerequisites:
  pip install PyCardano requests
  export BLOCKFROST_PROJECT_ID="preprod..."
  export DEPLOY_SIGNING_KEY="<32-byte Ed25519 hex>"
  export DEPLOY_KEY_HASH="<vkey hash>"
  export DEPLOY_COLLATERAL_REF="<txhash#index>"
  cd identity_registry && aiken build   # produces real plutus.json

Usage:
  python3 scripts/verify.py
  python3 scripts/verify.py --dry-run   # print metadata without submitting
"""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
CIP171_METADATA_LABEL = 1984
REPO_URL = "https://github.com/jluong2/Quorum_ADA"

# ─── Dependencies ──────────────────────────────────────────

try:
    import requests
except ImportError:
    sys.exit("Missing: pip install requests")

try:
    from pycardano import (
        Address,
        AuxiliaryData,
        Metadata,
        Network,
        PaymentSigningKey,
        PaymentVerificationKey,
        Transaction,
        TransactionBody,
        TransactionId,
        TransactionInput,
        TransactionOutput,
        TransactionWitnessSet,
        VerificationKeyHash,
        VerificationKeyWitness,
    )
except ImportError:
    sys.exit("Missing: pip install PyCardano")

sys.path.insert(0, str(REPO_ROOT))
from fos_agent.signing import pkh_to_enterprise_address


# ─── Config ────────────────────────────────────────────────

BLOCKFROST_PROJECT_ID = os.environ.get("BLOCKFROST_PROJECT_ID", "")
NETWORK_STR           = os.environ.get("CARDANO_NETWORK", "preprod")
DEPLOY_SIGNING_KEY    = os.environ.get("DEPLOY_SIGNING_KEY", "")
DEPLOY_KEY_HASH       = os.environ.get("DEPLOY_KEY_HASH", "")

BLOCKFROST_URL = {
    "mainnet": "https://cardano-mainnet.blockfrost.io/api/v0",
    "preprod": "https://cardano-preprod.blockfrost.io/api/v0",
    "preview":  "https://cardano-preview.blockfrost.io/api/v0",
}.get(NETWORK_STR, "https://cardano-preprod.blockfrost.io/api/v0")

NETWORK = Network.MAINNET if NETWORK_STR == "mainnet" else Network.TESTNET
FEE_ESTIMATE = 300_000   # 0.3 ADA — metadata txs are small


# ─── Helpers ───────────────────────────────────────────────

def chunk_str(s: str, size: int = 64) -> "str | list[str]":
    """Split strings longer than 64 bytes into a list of chunks.
    Cardano metadata enforces a 64-byte limit per string value."""
    encoded = s.encode("utf-8")
    if len(encoded) <= size:
        return s
    chunks, pos = [], 0
    while pos < len(encoded):
        # Find the largest slice that decodes cleanly within size bytes
        end = min(pos + size, len(encoded))
        while end > pos:
            try:
                chunk = encoded[pos:end].decode("utf-8")
                break
            except UnicodeDecodeError:
                end -= 1
        chunks.append(chunk)
        pos += len(chunk.encode("utf-8"))
    return chunks


def get_git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=REPO_ROOT, check=True,
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def get_aiken_version() -> str:
    try:
        result = subprocess.run(
            ["aiken", "--version"],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )
        return result.stdout.strip().split("\n")[0]
    except Exception:
        return "unknown"


def load_script_hashes() -> dict[str, str]:
    """Read real script hashes from compiled plutus.json."""
    path = REPO_ROOT / "identity_registry" / "plutus.json"
    if not path.exists():
        sys.exit(
            "plutus.json not found.\n"
            "Run: cd identity_registry && aiken build"
        )
    data = json.loads(path.read_text())
    validators = {v["title"]: v["hash"] for v in data.get("validators", [])}
    if not validators:
        sys.exit("No validators found in plutus.json — run: aiken build")

    # Detect mock hashes (from test setup)
    for name, h in validators.items():
        if "mock" in h.lower():
            sys.exit(
                f"plutus.json contains mock hashes ('{h}').\n"
                "Run the real Aiken compiler: cd identity_registry && aiken build"
            )
    return validators


def build_cip171_metadata(
    script_hashes: dict[str, str],
    commit: str,
    aiken_version: str,
) -> dict:
    """
    Build CIP-171 compliant metadata (label 1984).

    Each script gets a verification record mapping its on-chain hash to:
      - source repository (chunked if > 64 chars)
      - pinned git commit
      - compiler name and version
      - source file path within the repo
    """
    source_files = {
        "identity_registry": "identity_registry/validators/identity_registry.ak",
        "governance":         "identity_registry/validators/governance.ak",
        "treasury":           "identity_registry/validators/treasury.ak",
        "fos_types":          "identity_registry/lib/fos_types.ak",
    }

    records = {}
    for name, script_hash in script_hashes.items():
        records[script_hash] = {
            "repo":     chunk_str(REPO_URL),
            "commit":   commit,
            "compiler": "aiken",
            "version":  chunk_str(aiken_version),
            "file":     chunk_str(source_files.get(name, f"identity_registry/validators/{name}.ak")),
            "name":     name,
        }

    return {
        CIP171_METADATA_LABEL: {
            "v":       1,
            "network": NETWORK_STR,
            "scripts": records,
        }
    }


# ─── Blockfrost helpers ────────────────────────────────────

def bf_get(path: str) -> dict:
    url = f"{BLOCKFROST_URL}/{path.lstrip('/')}"
    resp = requests.get(url, headers={"project_id": BLOCKFROST_PROJECT_ID}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def bf_post_cbor(cbor_bytes: bytes) -> str:
    url = f"{BLOCKFROST_URL}/tx/submit"
    resp = requests.post(
        url,
        headers={"project_id": BLOCKFROST_PROJECT_ID, "Content-Type": "application/cbor"},
        data=cbor_bytes,
        timeout=30,
    )
    if not resp.ok:
        sys.exit(f"Blockfrost submit failed: {resp.status_code} {resp.text}")
    return resp.json()


def get_wallet_utxos(address: str) -> list[dict]:
    return bf_get(f"addresses/{address}/utxos")


# ─── Transaction builder ───────────────────────────────────

def build_metadata_tx(
    metadata: dict,
    signing_key_hex: str,
    deployer_addr: str,
    utxos: list[dict],
) -> bytes:
    """Build and sign a metadata-only transaction. Returns raw CBOR bytes."""
    sk  = PaymentSigningKey.from_primitive(bytes.fromhex(signing_key_hex))
    vk  = PaymentVerificationKey.from_signing_key(sk)

    # Pick largest UTxO to cover fee + min change
    fund_utxo = max(
        utxos,
        key=lambda u: int(next(
            (a["quantity"] for a in u["amount"] if a["unit"] == "lovelace"), "0"
        )),
    )
    fund_ref      = fund_utxo["tx_hash"] + "#" + str(fund_utxo["output_index"])
    fund_lovelace = int(next(a["quantity"] for a in fund_utxo["amount"] if a["unit"] == "lovelace"))
    change        = fund_lovelace - FEE_ESTIMATE

    if change < 1_000_000:
        sys.exit(
            f"Insufficient funds: need at least {(FEE_ESTIMATE + 1_000_000) / 1e6:.2f} ADA, "
            f"have {fund_lovelace / 1e6:.2f} ADA."
        )

    tx_hash_raw, tx_index = fund_ref.split("#")
    tx_input  = TransactionInput(TransactionId(bytes.fromhex(tx_hash_raw)), int(tx_index))
    tx_output = TransactionOutput(Address.from_primitive(deployer_addr), change)

    aux_data = AuxiliaryData(Metadata(metadata))

    body = TransactionBody(
        inputs=[tx_input],
        outputs=[tx_output],
        fee=FEE_ESTIMATE,
        auxiliary_data_hash=aux_data.hash(),
    )
    sig         = sk.sign(TransactionId(body.hash()).payload)
    witness_set = TransactionWitnessSet(vkey_witnesses=[VerificationKeyWitness(vk, sig)])
    tx          = Transaction(body=body, witness_set=witness_set, auxiliary_data=aux_data)
    return tx.to_cbor()


# ─── Main ──────────────────────────────────────────────────

def main():
    dry_run = "--dry-run" in sys.argv

    print("=" * 60)
    print("  Quorum — CIP-171 On-Chain Verification")
    print(f"  Network: {NETWORK_STR}")
    print("=" * 60)

    # Validate env vars (not needed for dry run)
    if not dry_run:
        missing = [v for v in ["BLOCKFROST_PROJECT_ID", "DEPLOY_SIGNING_KEY", "DEPLOY_KEY_HASH"]
                   if not os.environ.get(v)]
        if missing:
            sys.exit(f"Missing env vars: {', '.join(missing)}\n{__doc__}")

    # Gather facts
    print("\n── Gathering verification data ──────────────────────")
    script_hashes = load_script_hashes()
    commit        = get_git_commit()
    aiken_version = get_aiken_version()

    print(f"  Git commit:    {commit}")
    print(f"  Aiken version: {aiken_version}")
    print(f"  Scripts found: {len(script_hashes)}")
    for name, h in script_hashes.items():
        print(f"    {name}: {h}")

    # Build metadata
    metadata = build_cip171_metadata(script_hashes, commit, aiken_version)

    print("\n── CIP-171 Metadata (label 1984) ────────────────────")
    print(json.dumps(metadata, indent=2))

    if dry_run:
        print("\n── Dry run — not submitting ─────────────────────────")
        print("  Remove --dry-run to publish on-chain.")
        return

    # Check wallet
    deployer_addr = pkh_to_enterprise_address(DEPLOY_KEY_HASH, NETWORK_STR)
    print(f"\n── Deployer wallet: {deployer_addr}")
    utxos = get_wallet_utxos(deployer_addr)
    total = sum(int(a["quantity"]) for u in utxos for a in u["amount"] if a["unit"] == "lovelace")
    print(f"  Balance: {total / 1e6:.2f} ADA across {len(utxos)} UTxO(s)")

    # Build and submit
    print("\n── Submitting verification transaction ──────────────")
    tx_cbor = build_metadata_tx(metadata, DEPLOY_SIGNING_KEY, deployer_addr, utxos)
    tx_hash = bf_post_cbor(tx_cbor)
    print(f"  Submitted: {tx_hash}")
    print(f"\n  View on-chain:")
    if NETWORK_STR == "mainnet":
        print(f"  https://cardanoscan.io/transaction/{tx_hash}")
    else:
        print(f"  https://preprod.cardanoscan.io/transaction/{tx_hash}")

    print("\n── Verification published ───────────────────────────")
    print("  Anyone can now verify your contracts by:")
    print(f"  1. git clone {REPO_URL}")
    print(f"  2. git checkout {commit}")
    print("  3. cd identity_registry && aiken build")
    print("  4. Compare plutus.json hashes with the on-chain metadata")
    print("     They will match.\n")


if __name__ == "__main__":
    main()
