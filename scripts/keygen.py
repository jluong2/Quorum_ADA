#!/usr/bin/env python3
"""
Generate a fresh Ed25519 key pair for Quorum preprod deployment.

Outputs:
  deploy.skey  — private key (KEEP SECRET, never commit)
  deploy.vkey  — verification key
  deploy.addr  — preprod bech32 address to fund from the faucet

Also prints the env var values needed for deploy.py.
"""

import sys
from pathlib import Path

try:
    from pycardano import (
        Address,
        Network,
        PaymentSigningKey,
        PaymentVerificationKey,
    )
except ImportError:
    sys.exit("Missing: pip install PyCardano")

REPO_ROOT = Path(__file__).parent.parent
OUT_DIR   = REPO_ROOT  # write keys next to the repo root, not inside it

def main():
    # Generate fresh key pair
    sk = PaymentSigningKey.generate()
    vk = PaymentVerificationKey.from_signing_key(sk)

    # Derive preprod enterprise address
    addr = Address(payment_part=vk.hash(), network=Network.TESTNET)

    # Paths
    skey_path = OUT_DIR / "deploy.skey"
    vkey_path = OUT_DIR / "deploy.vkey"
    addr_path = OUT_DIR / "deploy.addr"

    # Write files
    sk.save(str(skey_path))
    vk.save(str(vkey_path))
    addr_path.write_text(str(addr))

    # Extract raw 32-byte signing key hex (strip CBOR envelope)
    sk_cbor_hex = sk.to_primitive().hex()
    # PyCardano wraps in 5820 (CBOR bytestring header for 32 bytes)
    raw_sk_hex = sk_cbor_hex[4:] if sk_cbor_hex.startswith("5820") else sk_cbor_hex

    vk_hash = vk.hash().payload.hex()

    print("=" * 60)
    print("  Quorum Deployer Key Pair")
    print("=" * 60)
    print(f"\n  Address:  {addr}")
    print(f"  Key hash: {vk_hash}")
    print(f"\n  Files written:")
    print(f"    {skey_path}  ← KEEP SECRET")
    print(f"    {vkey_path}")
    print(f"    {addr_path}")
    print()
    print("─" * 60)
    print("  STEP 1 — Fund the address")
    print("─" * 60)
    print(f"\n  Go to: https://docs.cardano.org/cardano-testnets/tools/faucet/")
    print(f"  Network: Preprod")
    print(f"  Address: {addr}")
    print(f"  Request at least 100 ADA (covers treasury + fees + collateral)")
    print()
    print("─" * 60)
    print("  STEP 2 — Set env vars (after funding)")
    print("─" * 60)
    print()
    print(f'  export DEPLOY_SIGNING_KEY="{raw_sk_hex}"')
    print(f'  export DEPLOY_KEY_HASH="{vk_hash}"')
    print()
    print("  Then set BLOCKFROST_PROJECT_ID and DEPLOY_COLLATERAL_REF,")
    print("  and run: python3 scripts/deploy.py")
    print()
    print("  WARNING: deploy.skey contains your private key.")
    print("  Do not commit it to git. .gitignore already excludes *.skey.")


if __name__ == "__main__":
    main()
