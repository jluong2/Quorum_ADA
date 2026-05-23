"""
PyCardano-based signing layer for FOS transactions.

Two responsibilities:
  1. Address derivation  — script hash / pkh → bech32  (used by transactions.py)
  2. Transaction signing — UnsignedTransaction → CBOR hex for Blockfrost submission

Requires: pip install PyCardano

Without PyCardano, address derivation returns mock bech32-ish strings that are
safe for testing but will be rejected by a real node.

Fee model (preprod / mainnet):
  fee = 155_381 + 44 * tx_bytes + ceil(mem * 577 / 1000) + ceil(steps * 721 / 1_000_000)
  This matches Cardano protocol parameters as of 2024; query Blockfrost
  /epochs/latest/parameters to get live values if they change.
"""

from __future__ import annotations
from typing import Optional

from .transactions import UnsignedTransaction, TxOutput, Redeemer

try:
    import cbor2 as _cbor2
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
        TransactionHash,
        TransactionInput,
        TransactionOutput,
        TransactionWitnessSet,
        VerificationKeyHash,
        VerificationKeyWitness,
    )
    _PYCARDANO = True
except ImportError:
    _PYCARDANO = False


# ─── Protocol fee parameters ──────────────────────────────
# Cardano preprod/mainnet as of 2024 (query /epochs/latest/parameters for live values)
_MIN_FEE_CONSTANT   = 155_381   # lovelace
_MIN_FEE_PER_BYTE   = 44        # lovelace / byte
_MEM_PRICE_PER_K    = 577       # lovelace / 1000 mem units
_STEP_PRICE_PER_M   = 721       # lovelace / 1_000_000 step units
_FEE_BUFFER         = 50_000    # safety margin


def _net(net_str: str):
    assert _PYCARDANO, "pip install PyCardano"
    return Network.MAINNET if net_str == "mainnet" else Network.TESTNET


# ─── Address derivation ───────────────────────────────────

def script_hash_to_address(script_hash: str, network: str = "preprod") -> str:
    """Convert a PlutusV2 script hash to its bech32 enterprise script address."""
    if not _PYCARDANO:
        prefix = "addr1w" if network == "mainnet" else "addr_test1w"
        return f"{prefix}{script_hash[:20]}"
    sh = ScriptHash(bytes.fromhex(script_hash))
    return str(Address(payment_part=sh, network=_net(network)))


def pkh_to_enterprise_address(pkh: str, network: str = "preprod") -> str:
    """Convert a verification key hash to its bech32 enterprise address."""
    if not _PYCARDANO:
        prefix = "addr1v" if network == "mainnet" else "addr_test1v"
        return f"{prefix}{pkh[:20]}"
    vkh = VerificationKeyHash(bytes.fromhex(pkh))
    return str(Address(payment_part=vkh, network=_net(network)))


# ─── Redeemer encoding ────────────────────────────────────

def _cbor_from_redeemer_dict(d) -> object:
    """Recursively convert a redeemer dict (constructor/fields/bytes/int) to cbor2 types."""
    if isinstance(d, dict):
        if "constructor" in d:
            n = d["constructor"]
            tag = 121 + n if n <= 6 else 1280 + (n - 7)
            fields = [_cbor_from_redeemer_dict(f) for f in d.get("fields", [])]
            return _cbor2.CBORTag(tag, fields)
        if "bytes" in d:
            return bytes.fromhex(d["bytes"])
        if "int" in d:
            return int(d["int"])
    if isinstance(d, list):
        return [_cbor_from_redeemer_dict(x) for x in d]
    return d


# ─── Fee estimation ───────────────────────────────────────

def _estimate_fee(tx_bytes: int, total_mem: int, total_steps: int) -> int:
    return (
        _MIN_FEE_CONSTANT
        + _MIN_FEE_PER_BYTE * tx_bytes
        + _MEM_PRICE_PER_K * total_mem // 1_000
        + _STEP_PRICE_PER_M * total_steps // 1_000_000
        + _FEE_BUFFER
    )


# ─── Transaction signing ──────────────────────────────────

def sign_transaction(
    unsigned_tx: UnsignedTransaction,
    signing_key_hex: str,
    *,
    plutus_scripts_hex: list[str],
    collateral_ref: str,
    change_address: str,
    change_lovelace: int,
    network: str = "preprod",
) -> str:
    """
    Assemble and sign an UnsignedTransaction into a CBOR hex string.

    Args:
        unsigned_tx:         Transaction descriptor from a builder in transactions.py.
        signing_key_hex:     Hex-encoded 32-byte Ed25519 private key scalar.
        plutus_scripts_hex:  List of compiled script CBORs (from plutus.json compiledCode).
        collateral_ref:      UTxO providing collateral for script execution ("txhash#index").
        change_address:      Bech32 address for the change output.
        change_lovelace:     Lovelace for the change output (caller calculates).
        network:             "preprod" | "mainnet" | "preview"

    Returns:
        Hex-encoded signed transaction CBOR, ready for Blockfrost /tx/submit.

    Note on execution units:
        The budget values in each Redeemer are used as-is.  For accurate
        budgets before first submission, use Blockfrost /utils/txs/evaluate
        on the draft unsigned transaction and update the budgets accordingly.
    """
    assert _PYCARDANO, "pip install PyCardano"
    net = _net(network)

    # ── Signing key ──────────────────────────────────────
    sk = PaymentSigningKey.from_primitive(bytes.fromhex(signing_key_hex))
    vk = PaymentVerificationKey.from_signing_key(sk)

    # ── Parse UTxO references ────────────────────────────
    def _parse_ref(ref: str) -> TransactionInput:
        h, i = ref.split("#")
        return TransactionInput(TransactionHash(bytes.fromhex(h)), int(i))

    # Inputs must be lexicographically sorted (Cardano ledger rule)
    inputs     = sorted([_parse_ref(r) for r in unsigned_tx.inputs],
                        key=lambda x: (bytes(x.transaction_id.payload), x.index))
    ref_inputs = [_parse_ref(r) for r in unsigned_tx.reference_inputs] or None
    collateral = [_parse_ref(collateral_ref)]

    # ── Build outputs ────────────────────────────────────
    def _build_output(out: TxOutput) -> TransactionOutput:
        addr = Address.from_primitive(out.address)
        if out.datum_hex:
            raw = RawPlutusData(cbor_primitive=_cbor2.loads(bytes.fromhex(out.datum_hex)))
            return TransactionOutput(addr, out.lovelace, datum=raw)
        return TransactionOutput(addr, out.lovelace)

    outputs = [_build_output(o) for o in unsigned_tx.outputs]
    if change_lovelace > 0:
        outputs.append(TransactionOutput(Address.from_primitive(change_address), change_lovelace))

    # ── Build redeemers ──────────────────────────────────
    # Index = position of the input being spent in the sorted input list.
    sorted_input_refs = [
        f"{bytes(inp.transaction_id.payload).hex()}#{inp.index}"
        for inp in inputs
    ]
    redeemers = []
    for red in unsigned_tx.redeemers:
        idx = sorted_input_refs.index(red.input_ref)
        raw_primitive = _cbor_from_redeemer_dict(red.data)
        cbor_bytes = _cbor2.dumps(raw_primitive)
        cbor_data = RawPlutusData(cbor_primitive=_cbor2.loads(cbor_bytes))
        redeemers.append(CardanoRedeemer(
            tag=RedeemerTag.SPEND,
            index=idx,
            data=cbor_data,
            ex_units=ExecutionUnits(memory=red.budget_mem, steps=red.budget_cpu),
        ))

    # ── Scripts ──────────────────────────────────────────
    scripts = [PlutusV2Script(bytes.fromhex(s)) for s in plutus_scripts_hex] or None

    # ── Required signers ─────────────────────────────────
    req_signers = [VerificationKeyHash(bytes.fromhex(kh))
                   for kh in unsigned_tx.required_signers] or None

    # ── Validity range (slots, not ms) ───────────────────
    # Caller is responsible for converting POSIX ms → slot numbers.
    # The builder stores raw ms; this layer passes them through as slot ints.
    # For preprod: slot ≈ (posix_ms - shelley_start_ms) / 1000
    validity_start = unsigned_tx.validity_start_ms
    ttl            = unsigned_tx.validity_end_ms

    # ── Metadata ─────────────────────────────────────────
    aux_data = None
    if unsigned_tx.metadata:
        aux_data = AuxiliaryData(Metadata({674: unsigned_tx.metadata}))

    # ── Draft body for fee estimation ─────────────────────
    draft_body = TransactionBody(
        inputs=inputs,
        outputs=outputs,
        fee=0,
        reference_inputs=ref_inputs,
        collateral=collateral,
        required_signers=req_signers,
        validity_start=validity_start,
        ttl=ttl,
    )
    total_mem   = sum(r.ex_units.memory for r in redeemers)
    total_steps = sum(r.ex_units.steps  for r in redeemers)
    fee = _estimate_fee(len(draft_body.to_cbor()), total_mem, total_steps)

    # ── Final body with fee ───────────────────────────────
    tx_body = TransactionBody(
        inputs=inputs,
        outputs=outputs,
        fee=fee,
        reference_inputs=ref_inputs,
        collateral=collateral,
        required_signers=req_signers,
        validity_start=validity_start,
        ttl=ttl,
    )

    # ── Sign ─────────────────────────────────────────────
    tx_hash = TransactionHash(tx_body.hash())
    signature = sk.sign(tx_hash.payload)
    witness_set = TransactionWitnessSet(
        vkey_witnesses=[VerificationKeyWitness(vk, signature)],
        plutus_v2_scripts=scripts,
        redeemers=redeemers or None,
    )

    tx = Transaction(body=tx_body, witness_set=witness_set, valid=True,
                     auxiliary_data=aux_data)
    return tx.to_cbor_hex()
