"""
PyCardano-based signing layer for FOS transactions.

Two responsibilities:
  1. Address derivation  — script hash / pkh → bech32  (used by transactions.py)
  2. Transaction signing — UnsignedTransaction → submitted tx hash

Flow for Plutus transactions:
  build (transactions.py) → evaluate (Blockfrost) → sign → submit

Requires: pip install PyCardano cbor2 requests
"""

from __future__ import annotations
import math
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
        TransactionId,
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
# Cardano preprod/mainnet as of 2025 (query /epochs/latest/parameters for live values)
_MIN_FEE_CONSTANT = 155_381   # lovelace
_MIN_FEE_PER_BYTE = 44        # lovelace / byte
_MEM_PRICE        = 0.0577    # lovelace / mem unit
_STEP_PRICE       = 0.0000721 # lovelace / step
_FEE_BUFFER       = 100_000   # safety margin


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


# ─── Slot conversion ──────────────────────────────────────
# Post-Shelley, 1 slot = 1 second on all networks.
# Fetch current slot + block time from Blockfrost to derive target slot.

def posix_ms_to_slot(
    posix_ms: int,
    blockfrost_url: str,
    project_id: str,
) -> int:
    """Convert a POSIX millisecond timestamp to an approximate Cardano slot number.

    Uses Blockfrost /blocks/latest to anchor the conversion — accurate to
    within a few slots of when the function is called.
    """
    try:
        import requests
        resp = requests.get(
            f"{blockfrost_url}/blocks/latest",
            headers={"project_id": project_id},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        current_slot = int(data["slot"])
        current_time_s = int(data["time"])  # unix seconds
        target_s = posix_ms / 1000
        return current_slot + int(target_s - current_time_s)
    except Exception:
        # Fallback: rough preprod approximation (slot 0 ≈ 2022-04-01)
        PREPROD_SHELLEY_SLOT   = 86400
        PREPROD_SHELLEY_UNIX_S = 1648771200
        return PREPROD_SHELLEY_SLOT + int(posix_ms / 1000 - PREPROD_SHELLEY_UNIX_S)


# ─── Redeemer encoding ────────────────────────────────────

def _cbor_from_redeemer_dict(d) -> object:
    """Recursively convert a redeemer dict to cbor2 primitives."""
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
    return int(
        _MIN_FEE_CONSTANT
        + _MIN_FEE_PER_BYTE * tx_bytes
        + math.ceil(_MEM_PRICE * total_mem)
        + math.ceil(_STEP_PRICE * total_steps)
        + _FEE_BUFFER
    )


# ─── Blockfrost execution unit evaluation ─────────────────

def evaluate_execution_units(
    tx_cbor_hex: str,
    blockfrost_url: str,
    project_id: str,
) -> dict[str, dict]:
    """
    Submit a draft transaction to Blockfrost /utils/txs/evaluate and return
    real execution units for each redeemer.

    Returns: {"spend:0": {"mem": N, "steps": N}, ...}
    Returns {} on error (caller falls back to placeholder budgets).
    """
    try:
        import requests
        resp = requests.post(
            f"{blockfrost_url}/utils/txs/evaluate",
            headers={
                "project_id": project_id,
                "Content-Type": "application/cbor",
            },
            data=bytes.fromhex(tx_cbor_hex),
            timeout=30,
        )
        if not resp.ok:
            return {}
        data = resp.json()
        result = {}
        for key, val in data.get("result", {}).items():
            # key = "spend:0", "spend:1", etc.
            mem   = val.get("memory", 0)
            steps = val.get("steps", 0)
            result[key] = {"mem": mem, "steps": steps}
        return result
    except Exception:
        return {}


# ─── Core signing ─────────────────────────────────────────

def _parse_ref(ref: str) -> "TransactionInput":
    h, i = ref.split("#")
    return TransactionInput(TransactionId(bytes.fromhex(h)), int(i))


def _build_output(out: TxOutput) -> "TransactionOutput":
    addr = Address.from_primitive(out.address)
    if out.datum_hex:
        raw = RawPlutusData(cbor_primitive=_cbor2.loads(bytes.fromhex(out.datum_hex)))
        return TransactionOutput(addr, out.lovelace, datum=raw)
    return TransactionOutput(addr, out.lovelace)


def sign_transaction(
    unsigned_tx: UnsignedTransaction,
    signing_key_hex: str,
    *,
    plutus_scripts_hex: list[str],
    collateral_ref: str,
    change_address: str,
    change_lovelace: int,
    network: str = "preprod",
    blockfrost_url: str = "",
    project_id: str = "",
    evaluated_units: Optional[dict] = None,
) -> str:
    """
    Assemble and sign an UnsignedTransaction. Returns signed CBOR hex.

    Args:
        unsigned_tx:       Transaction descriptor from transactions.py.
        signing_key_hex:   Hex-encoded 32-byte Ed25519 private key.
        plutus_scripts_hex: Compiled script CBORs from plutus.json.
        collateral_ref:    UTxO for Plutus collateral ("txhash#index").
        change_address:    Bech32 address for change output.
        change_lovelace:   Lovelace for the change output.
        network:           "preprod" | "mainnet" | "preview"
        blockfrost_url:    Used for slot conversion. Optional.
        project_id:        Blockfrost project ID. Optional.
        evaluated_units:   Pre-fetched execution units from evaluate_execution_units().
                           If None and blockfrost_url is set, will evaluate automatically.
    """
    assert _PYCARDANO, "pip install PyCardano"
    net = _net(network)

    sk = PaymentSigningKey.from_primitive(bytes.fromhex(signing_key_hex))
    vk = PaymentVerificationKey.from_signing_key(sk)

    # ── Convert ms validity range → slot numbers ──────────
    def _to_slot(ms: Optional[int]) -> Optional[int]:
        if ms is None:
            return None
        if blockfrost_url and project_id:
            return posix_ms_to_slot(ms, blockfrost_url, project_id)
        # Fallback: preprod rough approximation
        return posix_ms_to_slot(ms, "", "")

    validity_start = _to_slot(unsigned_tx.validity_start_ms)
    ttl            = _to_slot(unsigned_tx.validity_end_ms)

    # ── Parse inputs ──────────────────────────────────────
    inputs     = sorted([_parse_ref(r) for r in unsigned_tx.inputs],
                        key=lambda x: (bytes(x.transaction_id.payload), x.index))
    ref_inputs = [_parse_ref(r) for r in unsigned_tx.reference_inputs] or None
    collateral = [_parse_ref(collateral_ref)] if collateral_ref else None

    # ── Build outputs ─────────────────────────────────────
    outputs = [_build_output(o) for o in unsigned_tx.outputs]
    if change_lovelace > 0:
        outputs.append(TransactionOutput(Address.from_primitive(change_address), change_lovelace))

    # ── Build redeemers ───────────────────────────────────
    sorted_input_refs = [
        f"{bytes(inp.transaction_id.payload).hex()}#{inp.index}"
        for inp in inputs
    ]
    redeemers = []
    for i, red in enumerate(unsigned_tx.redeemers):
        idx = sorted_input_refs.index(red.input_ref)
        raw_primitive = _cbor_from_redeemer_dict(red.data)
        cbor_data = RawPlutusData(cbor_primitive=raw_primitive)

        # Use evaluated units if available
        unit_key = f"spend:{idx}"
        if evaluated_units and unit_key in evaluated_units:
            mem   = evaluated_units[unit_key]["mem"]
            steps = evaluated_units[unit_key]["steps"]
        else:
            mem, steps = red.budget_mem, red.budget_cpu

        redeemers.append(CardanoRedeemer(
            tag=RedeemerTag.SPEND,
            index=idx,
            data=cbor_data,
            ex_units=ExecutionUnits(memory=mem, steps=steps),
        ))

    scripts     = [PlutusV2Script(bytes.fromhex(s)) for s in plutus_scripts_hex] or None
    req_signers = [VerificationKeyHash(bytes.fromhex(kh))
                   for kh in unsigned_tx.required_signers] or None

    aux_data = None
    if unsigned_tx.metadata:
        aux_data = AuxiliaryData(Metadata({674: unsigned_tx.metadata}))

    def _build_body(fee: int) -> "TransactionBody":
        return TransactionBody(
            inputs=inputs,
            outputs=outputs,
            fee=fee,
            reference_inputs=ref_inputs,
            collateral=collateral,
            required_signers=req_signers,
            validity_start=validity_start,
            ttl=ttl,
        )

    # ── Two-pass fee estimation ───────────────────────────
    total_mem   = sum(r.ex_units.memory for r in redeemers)
    total_steps = sum(r.ex_units.steps  for r in redeemers)
    draft_fee   = _estimate_fee(len(_build_body(0).to_cbor()), total_mem, total_steps)
    fee         = _estimate_fee(len(_build_body(draft_fee).to_cbor()), total_mem, total_steps)

    tx_body = _build_body(fee)

    # ── Sign ──────────────────────────────────────────────
    sig = sk.sign(TransactionId(tx_body.hash()).payload)
    witness_set = TransactionWitnessSet(
        vkey_witnesses=[VerificationKeyWitness(vk, sig)],
        plutus_v2_scripts=scripts,
        redeemers=redeemers or None,
    )

    return Transaction(
        body=tx_body,
        witness_set=witness_set,
        valid=True,
        auxiliary_data=aux_data,
    ).to_cbor_hex()


# ─── End-to-end: evaluate → sign → return CBOR ───────────

def build_signed_transaction(
    unsigned_tx: UnsignedTransaction,
    signing_key_hex: str,
    plutus_scripts_hex: list[str],
    collateral_ref: str,
    change_address: str,
    change_lovelace: int,
    blockfrost_url: str,
    project_id: str,
    network: str = "preprod",
) -> str:
    """
    Full pipeline: evaluate execution units (via Blockfrost), then sign.
    Returns signed transaction CBOR hex ready for /tx/submit.
    """
    # Phase 1: sign with placeholder units to get an evaluable draft
    draft_cbor = sign_transaction(
        unsigned_tx,
        signing_key_hex,
        plutus_scripts_hex=plutus_scripts_hex,
        collateral_ref=collateral_ref,
        change_address=change_address,
        change_lovelace=change_lovelace,
        network=network,
        blockfrost_url=blockfrost_url,
        project_id=project_id,
        evaluated_units=None,
    )

    # Phase 2: get real execution units
    evaluated = evaluate_execution_units(draft_cbor, blockfrost_url, project_id)

    if not evaluated:
        # No evaluation available — use placeholder (works for non-Plutus or mock)
        return draft_cbor

    # Phase 3: rebuild with real units for accurate fee
    return sign_transaction(
        unsigned_tx,
        signing_key_hex,
        plutus_scripts_hex=plutus_scripts_hex,
        collateral_ref=collateral_ref,
        change_address=change_address,
        change_lovelace=change_lovelace,
        network=network,
        blockfrost_url=blockfrost_url,
        project_id=project_id,
        evaluated_units=evaluated,
    )
