#!/usr/bin/env python3
"""
Deploy the Quorum contracts to Cardano preprod testnet.

Steps:
  1. Read identity_registry/plutus.json for compiled script CBORs + hashes
  2. Derive bech32 script addresses via PyCardano
  3. Lock the initial UTxOs:
       - Registry UTxO:  RegistryDatum with founding member(s)
       - Treasury UTxO:  TreasuryDatum with governance_script_hash + spending cap
  4. Print env vars to export for the Quorum operator agent

Prerequisites:
  pip install PyCardano cbor2 requests
  export BLOCKFROST_PROJECT_ID="preprod..."
  export DEPLOY_SIGNING_KEY="<hex-encoded 32-byte Ed25519 private key>"
  export DEPLOY_KEY_HASH="<hex vkey hash of DEPLOY_SIGNING_KEY>"
  export DEPLOY_COLLATERAL_REF="<txhash#index of a 5+ ADA wallet UTxO>"

Usage:
  python3 scripts/deploy.py
  # Then follow the printed instructions to set env vars and fund the agent key.
"""

import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

# ─── Load requirements ─────────────────────────────────────

try:
    import cbor2
except ImportError:
    sys.exit("Missing: pip install cbor2")

try:
    import requests
except ImportError:
    sys.exit("Missing: pip install requests")

try:
    from pycardano import (
        Address,
        AuxiliaryData,
        ExecutionUnits,
        Metadata,
        Network,
        PaymentSigningKey,
        PaymentVerificationKey,
        PlutusV2Script,
        RawPlutusData,
        Redeemer as CardanoRedeemer,
        RedeemerTag,
        ScriptHash,
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

# Add repo root to path so fos_agent is importable
sys.path.insert(0, str(REPO_ROOT))
from fos_agent.types import RegistryDatum, RegistryMember, TreasuryDatum
from fos_agent.datums import registry_datum_cbor_hex, treasury_datum_cbor_hex
from fos_agent.signing import script_hash_to_address, pkh_to_enterprise_address


# ─── Config ───────────────────────────────────────────────

BLOCKFROST_PROJECT_ID = os.environ.get("BLOCKFROST_PROJECT_ID", "")
NETWORK_STR           = os.environ.get("CARDANO_NETWORK", "preprod")
DEPLOY_SIGNING_KEY    = os.environ.get("DEPLOY_SIGNING_KEY", "")
DEPLOY_KEY_HASH       = os.environ.get("DEPLOY_KEY_HASH", "")
DEPLOY_COLLATERAL_REF = os.environ.get("DEPLOY_COLLATERAL_REF", "")

BLOCKFROST_URL = {
    "mainnet": "https://cardano-mainnet.blockfrost.io/api/v0",
    "preprod": "https://cardano-preprod.blockfrost.io/api/v0",
    "preview":  "https://cardano-preview.blockfrost.io/api/v0",
}.get(NETWORK_STR, "https://cardano-preprod.blockfrost.io/api/v0")

NETWORK = Network.MAINNET if NETWORK_STR == "mainnet" else Network.TESTNET

# Initial registry: the deployer key becomes the first Admin
INITIAL_MEMBERS = []  # populated from DEPLOY_KEY_HASH below
INITIAL_TREASURY_LOVELACE    = 50_000_000   # 50 ADA initial funding
MAX_TRANSFER_LOVELACE        = 10_000_000   # 10 ADA per proposal
REGISTRY_MIN_ADA             = 3_000_000    # 3 ADA min UTxO
TREASURY_FUNDING             = INITIAL_TREASURY_LOVELACE + 2_000_000  # + min ADA

FEE_ESTIMATE = 500_000   # 0.5 ADA estimated fee per transaction


# ─── Blockfrost helpers ────────────────────────────────────

def bf_get(path: str) -> dict:
    url = f"{BLOCKFROST_URL}/{path.lstrip('/')}"
    resp = requests.get(url, headers={"project_id": BLOCKFROST_PROJECT_ID}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def bf_post_cbor(path: str, cbor_bytes: bytes) -> dict:
    url = f"{BLOCKFROST_URL}/{path.lstrip('/')}"
    resp = requests.post(
        url,
        headers={"project_id": BLOCKFROST_PROJECT_ID, "Content-Type": "application/cbor"},
        data=cbor_bytes,
        timeout=30,
    )
    if not resp.ok:
        print(f"  Blockfrost error: {resp.status_code} {resp.text}")
        resp.raise_for_status()
    return resp.json()


def get_utxos(address: str) -> list[dict]:
    return bf_get(f"addresses/{address}/utxos")


def get_latest_slot() -> int:
    return int(bf_get("blocks/latest")["slot"])


def wait_for_tx(tx_hash: str, max_wait_s: int = 120) -> bool:
    print(f"  Waiting for {tx_hash[:12]}… to confirm", end="", flush=True)
    for _ in range(max_wait_s // 5):
        time.sleep(5)
        print(".", end="", flush=True)
        try:
            bf_get(f"txs/{tx_hash}")
            print(" confirmed!")
            return True
        except requests.HTTPError:
            pass
    print(" timed out!")
    return False


# ─── Transaction helpers ───────────────────────────────────

def parse_ref(ref: str) -> TransactionInput:
    h, i = ref.split("#")
    return TransactionInput(TransactionId(bytes.fromhex(h)), int(i))


def build_and_sign(
    inputs: list[str],
    outputs: list[TransactionOutput],
    signing_key_hex: str,
    fee: int = FEE_ESTIMATE,
    validity_start: int = None,
    ttl: int = None,
) -> str:
    """Build a simple (no-script) transaction and return CBOR hex."""
    sk = PaymentSigningKey.from_primitive(bytes.fromhex(signing_key_hex))
    vk = PaymentVerificationKey.from_signing_key(sk)

    sorted_inputs = sorted(
        [parse_ref(r) for r in inputs],
        key=lambda x: (bytes(x.transaction_id.payload), x.index),
    )

    body = TransactionBody(
        inputs=sorted_inputs,
        outputs=outputs,
        fee=fee,
        validity_start=validity_start,
        ttl=ttl,
    )
    tx_hash = TransactionId(body.hash())
    sig = sk.sign(tx_hash.payload)
    witness_set = TransactionWitnessSet(
        vkey_witnesses=[VerificationKeyWitness(vk, sig)]
    )
    tx = Transaction(body=body, witness_set=witness_set, valid=True)
    return tx.to_cbor_hex()


# ─── Plutus.json reader ────────────────────────────────────

def load_plutus_json() -> dict:
    path = REPO_ROOT / "identity_registry" / "plutus.json"
    if not path.exists():
        sys.exit(
            f"plutus.json not found at {path}\n"
            "Run: cd identity_registry && aiken build"
        )
    return json.loads(path.read_text())


def extract_validators(plutus: dict) -> dict[str, dict]:
    """Return {title: {hash, compiledCode}} from plutus.json."""
    return {v["title"]: v for v in plutus.get("validators", [])}


# ─── Deploy steps ──────────────────────────────────────────

def step1_read_contracts() -> dict[str, dict]:
    print("\n── Step 1: Read compiled contracts ──────────────────")
    plutus = load_plutus_json()
    validators = extract_validators(plutus)
    print(f"  Found {len(validators)} validator(s):")
    for name, v in validators.items():
        print(f"    {name}  hash={v['hash']}")
    return validators


def step2_derive_addresses(validators: dict[str, dict]) -> dict[str, str]:
    print("\n── Step 2: Derive script addresses ──────────────────")
    addresses = {}
    for name, v in validators.items():
        addr = script_hash_to_address(v["hash"], NETWORK_STR)
        addresses[name] = addr
        print(f"  {name}:")
        print(f"    hash:    {v['hash']}")
        print(f"    address: {addr}")
    return addresses


def step3_check_wallet() -> tuple[str, list[dict]]:
    """Return deployer bech32 address and its UTxOs."""
    print("\n── Step 3: Check deployer wallet ─────────────────────")
    deployer_addr = pkh_to_enterprise_address(DEPLOY_KEY_HASH, NETWORK_STR)
    print(f"  Deployer address: {deployer_addr}")
    utxos = get_utxos(deployer_addr)
    total = sum(
        int(a["quantity"])
        for u in utxos
        for a in u.get("amount", [])
        if a["unit"] == "lovelace"
    )
    print(f"  UTxOs: {len(utxos)}, total ADA: {total / 1_000_000:.2f}")
    needed = REGISTRY_MIN_ADA + TREASURY_FUNDING + FEE_ESTIMATE * 2
    if total < needed:
        print(f"\n  ⚠  Need at least {needed / 1_000_000:.2f} ADA")
        print(f"     Fund {deployer_addr}")
        print(f"     via https://docs.cardano.org/cardano-testnets/tools/faucet/")
        sys.exit(1)
    return deployer_addr, utxos


def step4_deploy_registry(
    validators: dict,
    deployer_addr: str,
    deployer_utxos: list[dict],
    registry_address: str,
    governance_script_hash: str,
) -> str:
    """Lock the initial RegistryDatum UTxO at the registry script address."""
    print("\n── Step 4: Deploy identity registry ─────────────────")

    now_ms = int(time.time() * 1000)
    founding_member = RegistryMember(
        key_hash=DEPLOY_KEY_HASH,
        role=0,          # Admin
        joined_at=now_ms,
        status=0,        # Active
    )
    initial_registry = RegistryDatum(
        members=[founding_member],
        admin=DEPLOY_KEY_HASH,
        version=1,
        governance_script_hash=governance_script_hash,
    )
    datum_hex = registry_datum_cbor_hex(initial_registry)
    datum_bytes = bytes.fromhex(datum_hex)

    # Pick a UTxO to fund the registry output
    fund_utxo = max(
        deployer_utxos,
        key=lambda u: int(next(
            (a["quantity"] for a in u["amount"] if a["unit"] == "lovelace"), "0"
        )),
    )
    fund_ref = f"{fund_utxo['tx_hash']}#{fund_utxo['output_index']}"
    fund_lovelace = int(next(
        a["quantity"] for a in fund_utxo["amount"] if a["unit"] == "lovelace"
    ))
    change = fund_lovelace - REGISTRY_MIN_ADA - FEE_ESTIMATE

    # Build outputs
    registry_output = TransactionOutput(
        Address.from_primitive(registry_address),
        REGISTRY_MIN_ADA,
        datum=RawPlutusData(cbor_primitive=cbor2.loads(datum_bytes)),
    )
    change_output = TransactionOutput(
        Address.from_primitive(deployer_addr),
        change,
    )

    cbor_hex = build_and_sign(
        inputs=[fund_ref],
        outputs=[registry_output, change_output],
        signing_key_hex=DEPLOY_SIGNING_KEY,
    )
    tx_hash = bf_post_cbor("tx/submit", bytes.fromhex(cbor_hex))
    print(f"  Registry UTxO submitted: {tx_hash}")
    wait_for_tx(tx_hash)
    return f"{tx_hash}#0"


def step5_deploy_treasury(
    validators: dict,
    deployer_addr: str,
    deployer_utxos: list[dict],
    treasury_address: str,
    governance_script_hash: str,
) -> str:
    """Lock the initial TreasuryDatum UTxO with ADA at the treasury script address."""
    print("\n── Step 5: Deploy treasury ───────────────────────────")

    treasury_datum = TreasuryDatum(
        governance_script_hash=governance_script_hash,
        max_transfer_lovelace=MAX_TRANSFER_LOVELACE,
    )
    datum_hex = treasury_datum_cbor_hex(treasury_datum)
    datum_bytes = bytes.fromhex(datum_hex)

    fund_utxo = max(
        deployer_utxos,
        key=lambda u: int(next(
            (a["quantity"] for a in u["amount"] if a["unit"] == "lovelace"), "0"
        )),
    )
    fund_ref = f"{fund_utxo['tx_hash']}#{fund_utxo['output_index']}"
    fund_lovelace = int(next(
        a["quantity"] for a in fund_utxo["amount"] if a["unit"] == "lovelace"
    ))
    change = fund_lovelace - TREASURY_FUNDING - FEE_ESTIMATE

    treasury_output = TransactionOutput(
        Address.from_primitive(treasury_address),
        TREASURY_FUNDING,
        datum=RawPlutusData(cbor_primitive=cbor2.loads(datum_bytes)),
    )
    change_output = TransactionOutput(
        Address.from_primitive(deployer_addr),
        change,
    )

    cbor_hex = build_and_sign(
        inputs=[fund_ref],
        outputs=[treasury_output, change_output],
        signing_key_hex=DEPLOY_SIGNING_KEY,
    )
    tx_hash = bf_post_cbor("tx/submit", bytes.fromhex(cbor_hex))
    print(f"  Treasury UTxO submitted: {tx_hash}")
    wait_for_tx(tx_hash)
    return f"{tx_hash}#0"


def step6_print_env_vars(validators: dict, addresses: dict) -> None:
    registry_hash   = validators.get("identity_registry", {}).get("hash", "???")
    governance_hash = validators.get("governance",         {}).get("hash", "???")
    treasury_hash   = validators.get("treasury",           {}).get("hash", "???")

    print("\n── Step 6: Environment variables ─────────────────────")
    print("  Add these to your shell profile or .env file:\n")
    print(f'  export BLOCKFROST_PROJECT_ID="{BLOCKFROST_PROJECT_ID}"')
    print(f'  export CARDANO_NETWORK="{NETWORK_STR}"')
    print(f'  export FOS_REGISTRY_SCRIPT_HASH="{registry_hash}"')
    print(f'  export FOS_GOVERNANCE_SCRIPT_HASH="{governance_hash}"')
    print(f'  export FOS_TREASURY_SCRIPT_HASH="{treasury_hash}"')
    print(f'  export FOS_AGENT_KEY_HASH="{DEPLOY_KEY_HASH}"')
    print(f'  export FOS_AGENT_SIGNING_KEY="<your-32-byte-hex-private-key>"')
    print(f'  export FOS_MAX_AUTO_TRANSFER_LOVELACE="{MAX_TRANSFER_LOVELACE}"')
    print(f'  export FOS_AUTONOMOUS_MODE="false"')
    print()
    print("  Script addresses:")
    for name, addr in addresses.items():
        print(f"    {name}: {addr}")
    print()
    print("  Run the agent:")
    print('  python3 -c "from fos_agent import run_fos_agent; '
          'run_fos_agent(\'Check FOS state\')"')


# ─── Main ─────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("  Quorum Preprod Deployment")
    print(f"  Network: {NETWORK_STR}")
    print("=" * 60)

    # Validate required env vars
    missing = [v for v in ["BLOCKFROST_PROJECT_ID", "DEPLOY_SIGNING_KEY",
                            "DEPLOY_KEY_HASH", "DEPLOY_COLLATERAL_REF"]
               if not os.environ.get(v)]
    if missing:
        print(f"\nMissing required env vars: {', '.join(missing)}")
        print(__doc__)
        sys.exit(1)

    validators = step1_read_contracts()
    addresses  = step2_derive_addresses(validators)

    # Determine governance hash (needed for TreasuryDatum)
    governance_hash = validators.get("governance", {}).get("hash", "")
    if not governance_hash:
        sys.exit("governance validator not found in plutus.json")

    deployer_addr, deployer_utxos = step3_check_wallet()

    registry_ref = step4_deploy_registry(
        validators=validators,
        deployer_addr=deployer_addr,
        deployer_utxos=deployer_utxos,
        registry_address=addresses.get("identity_registry", ""),
        governance_script_hash=governance_hash,
    )
    print(f"  Registry UTxO: {registry_ref}")

    # Refresh UTxOs after first tx
    deployer_utxos = get_utxos(deployer_addr)

    treasury_ref = step5_deploy_treasury(
        validators=validators,
        deployer_addr=deployer_addr,
        deployer_utxos=deployer_utxos,
        treasury_address=addresses.get("treasury", ""),
        governance_script_hash=governance_hash,
    )
    print(f"  Treasury UTxO: {treasury_ref}")

    step6_print_env_vars(validators, addresses)
    print("\nDeployment complete.")


if __name__ == "__main__":
    main()
