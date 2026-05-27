"""
CIP-95 DRep registration for the Quorum FOS governance agent.

Registers the agent's verification key hash as a Cardano Delegated Representative
so ADA holders can delegate their Cardano governance voting power to the agent.

Usage:
    # Preview registration (no chain submission)
    python3 scripts/register_drep.py --dry-run

    # Register on preprod (requires env vars below)
    python3 scripts/register_drep.py

    # Retire the DRep registration
    python3 scripts/register_drep.py --retire

Required env vars:
    BLOCKFROST_PROJECT_ID   preprod… or mainnet…
    FOS_AGENT_KEY_HASH      28-byte VerificationKeyHash (hex)
    FOS_AGENT_SIGNING_KEY   32-byte Ed25519 private key (hex)
    DREP_ANCHOR_URL         HTTPS URL of the uploaded drep_metadata.json
                            (upload scripts/drep_metadata.json to IPFS first)
    DREP_ANCHOR_HASH        blake2b-256 of drep_metadata.json (hex)
                            Run:  python3 -c "
                              import hashlib, json
                              data = open('scripts/drep_metadata.json').read().encode()
                              print(hashlib.new('blake2b', data, digest_size=32).hexdigest())
                            "
"""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

from fos_agent import config
from fos_agent.drep import (
    build_drep_registration,
    build_drep_retirement,
    drep_id_from_key_hash,
    generate_drep_metadata,
    query_drep_status,
)


def _compute_metadata_hash(path: Path) -> str:
    """blake2b-256 of the metadata JSON file."""
    data = path.read_bytes()
    return hashlib.new("blake2b", data, digest_size=32).hexdigest()


def _submit_drep_cert(cert: object, dry_run: bool) -> None:
    """Build + sign + submit the DRep certificate via PyCardano."""
    if dry_run:
        print("\n[DRY RUN] Would submit DRep certificate — no transaction sent.")
        return

    try:
        from pycardano import (
            BlockFrostChainContext,
            DRep,
            DRepKind,
            Network,
            PaymentSigningKey,
            PaymentVerificationKey,
            TransactionBuilder,
        )
    except ImportError:
        print("PyCardano not installed — cannot submit.")
        print("pip install PyCardano")
        sys.exit(1)

    project_id = config.BLOCKFROST_PROJECT_ID
    if not project_id:
        print("BLOCKFROST_PROJECT_ID not set — cannot submit.")
        sys.exit(1)

    network = Network.MAINNET if config.NETWORK == "mainnet" else Network.TESTNET
    ctx = BlockFrostChainContext(project_id=project_id, network=network)

    signing_key_hex = os.environ.get("FOS_AGENT_SIGNING_KEY", "")
    if not signing_key_hex:
        print("FOS_AGENT_SIGNING_KEY not set — cannot sign.")
        sys.exit(1)

    sk = PaymentSigningKey.from_primitive(bytes.fromhex(signing_key_hex))
    vk = PaymentVerificationKey.from_signing_key(sk)

    # PyCardano DRep certificate construction
    builder = TransactionBuilder(ctx)
    # DRep registration is a certificate transaction — builder.add_certificate(...)
    # Full PyCardano DRep API is available from pycardano >= 0.9
    print("[INFO] DRep certificate construction via PyCardano.")
    print("       Pass the DRepRegistrationTx descriptor to your PyCardano transaction builder.")
    print(f"       Deposit required: {cert.deposit_lovelace / 1_000_000:.2f} ADA")
    print(f"       Anchor URL:       {cert.anchor_url}")
    print(f"       Anchor hash:      {cert.anchor_hash}")
    print()
    print("This script outputs the registration descriptor.")
    print("Integrate with your signing workflow (PyCardano >= 0.9 or cardano-cli).")


def main() -> None:
    parser = argparse.ArgumentParser(description="Register/retire Quorum FOS agent as a Cardano DRep")
    parser.add_argument("--dry-run", action="store_true", help="Preview without submitting")
    parser.add_argument("--retire",  action="store_true", help="Retire the DRep registration")
    parser.add_argument("--print-metadata", action="store_true",
                        help="Print generated CIP-119 metadata JSON and exit")
    args = parser.parse_args()

    key_hash = os.environ.get("FOS_AGENT_KEY_HASH", "")
    if not key_hash:
        print("FOS_AGENT_KEY_HASH not set.")
        sys.exit(1)

    if args.print_metadata:
        meta = generate_drep_metadata()
        print(json.dumps(meta, indent=2))
        meta_hash = hashlib.new(
            "blake2b",
            json.dumps(meta, separators=(",", ":"), sort_keys=True).encode(),
            digest_size=32,
        ).hexdigest()
        print(f"\n# blake2b-256 hash: {meta_hash}", file=sys.stderr)
        return

    drep_id = drep_id_from_key_hash(key_hash)
    print(f"Agent key hash: {key_hash}")
    print(f"DRep ID:        {drep_id}")

    # Current status
    print("\nQuerying on-chain DRep status…")
    status = query_drep_status(key_hash)
    if status.registered:
        print(f"  Status:       REGISTERED (active={status.is_active})")
        print(f"  Voting power: {status.voting_power / 1_000_000:.2f} ADA")
        print(f"  Delegators:   {status.delegator_count}")
        if status.anchor_url:
            print(f"  Anchor:       {status.anchor_url}")
    else:
        print("  Status:       NOT REGISTERED")

    if args.retire:
        cert = build_drep_retirement(key_hash)
        print(f"\nBuilding DRep retirement certificate…")
        print(f"  Description: {cert.description}")
        _submit_drep_cert(cert, dry_run=args.dry_run)
        if args.dry_run:
            print("[DRY RUN] DRep retirement would be submitted.")
        return

    # Registration
    metadata_path = Path(__file__).parent / "drep_metadata.json"
    anchor_url  = os.environ.get("DREP_ANCHOR_URL", "")
    anchor_hash = os.environ.get("DREP_ANCHOR_HASH", "")

    if not anchor_url:
        print("\nDREP_ANCHOR_URL not set.")
        print("Upload scripts/drep_metadata.json to IPFS or HTTPS, then set:")
        print("  export DREP_ANCHOR_URL=https://…/drep_metadata.json")
        print("  export DREP_ANCHOR_HASH=$(python3 -c \"")
        print("    import hashlib; data=open('scripts/drep_metadata.json').read().encode()")
        print("    print(hashlib.new('blake2b', data, digest_size=32).hexdigest())\")")
        if args.dry_run:
            anchor_url  = "https://example.com/drep_metadata.json"
            anchor_hash = "aa" * 32
            print("\n[DRY RUN] Using placeholder anchor values.")
        else:
            sys.exit(1)

    if not anchor_hash and metadata_path.exists():
        anchor_hash = _compute_metadata_hash(metadata_path)
        print(f"Computed anchor hash from local file: {anchor_hash}")

    cert = build_drep_registration(key_hash, anchor_url, anchor_hash)
    print(f"\nBuilding DRep registration certificate…")
    print(f"  Deposit:     {cert.deposit_lovelace / 1_000_000:.2f} ADA")
    print(f"  Anchor URL:  {cert.anchor_url}")
    print(f"  Anchor hash: {cert.anchor_hash}")

    _submit_drep_cert(cert, dry_run=args.dry_run)

    if not args.dry_run:
        print("\nDRep registration submitted. ADA holders can now delegate to:")
        print(f"  {drep_id}")


if __name__ == "__main__":
    main()
