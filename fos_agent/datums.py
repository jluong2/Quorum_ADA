"""
CBOR serialization of FOS datums — the inverse of types.py deserialization.

Used by transaction builders to produce correct inline datum hex for:
  - CastVote         (appends vote to GovernanceDatum)
  - ExecuteProposal  (Voting → Executed)
  - ExpireProposal   (Voting → Expired)
  - Initial deploy   (RegistryDatum, TreasuryDatum)

Encoding mirrors fos_types.ak:
  record           → Constr(0, fields)   cbor2 tag 121
  enum variant N   → Constr(N, [])       cbor2 tag 121+N (or 1280+N-7 for N≥7)
  ByteArray        → bytes
  Int              → int
  List<a>          → list
  Bool True/False  → Constr(1/0, [])
"""

from __future__ import annotations

try:
    import cbor2
    _CBOR_AVAILABLE = True
except ImportError:
    _CBOR_AVAILABLE = False

from .types import (
    GovernanceDatum,
    NativeToken,
    OffChainDecisionAction,
    OutputReference,
    ProposalAction,
    RegistryDatum,
    RegistryMember,
    RotateAdminAction,
    TreasuryDatum,
    TreasuryTransferAction,
    UpdateRegistryMemberAction,
    VoteRecord,
)


def _constr(n: int, fields: list):
    assert _CBOR_AVAILABLE, "pip install cbor2"
    tag = 121 + n if n <= 6 else 1280 + (n - 7)
    return cbor2.CBORTag(tag, fields)


def _encode(val) -> bytes:
    assert _CBOR_AVAILABLE, "pip install cbor2"
    return cbor2.dumps(val)


# ─── Internal encoders ────────────────────────────────────

def _encode_member(m: RegistryMember):
    # Option<VerificationKeyHash>: Some(v) = Constr(0,[bytes]), None = Constr(1,[])
    delegate_cbor = (
        _constr(0, [bytes.fromhex(m.delegate)]) if m.delegate is not None
        else _constr(1, [])
    )
    return _constr(0, [
        bytes.fromhex(m.key_hash),
        _constr(m.role, []),
        m.joined_at,
        _constr(m.status, []),
        delegate_cbor,
    ])


def _encode_output_ref(ref: OutputReference):
    tx_id = _constr(0, [bytes.fromhex(ref.tx_hash)])
    return _constr(0, [tx_id, ref.output_index])


def _encode_vote(v: VoteRecord):
    return _constr(0, [
        bytes.fromhex(v.voter),
        _constr(1 if v.approve else 0, []),
    ])


def _encode_native_token(t: NativeToken):
    return _constr(0, [
        bytes.fromhex(t.policy_id),
        t.asset_name.encode("utf-8") if not all(c in "0123456789abcdefABCDEF" for c in t.asset_name) or len(t.asset_name) % 2 != 0
        else bytes.fromhex(t.asset_name),
        t.quantity,
    ])


def _encode_action(a: ProposalAction):
    if isinstance(a, TreasuryTransferAction):
        return _constr(0, [
            bytes.fromhex(a.recipient),
            a.lovelace,
            a.memo.encode("utf-8"),
            [_encode_native_token(t) for t in a.tokens],
        ])
    if isinstance(a, RotateAdminAction):
        return _constr(1, [bytes.fromhex(a.new_admin)])
    if isinstance(a, UpdateRegistryMemberAction):
        return _constr(2, [
            bytes.fromhex(a.target_key),
            _constr(a.new_role, []),
            _constr(a.new_status, []),
        ])
    # OffChainDecisionAction
    return _constr(3, [a.memo.encode("utf-8")])


# ─── Public serializers ───────────────────────────────────

def registry_datum_cbor_hex(d: RegistryDatum) -> str:
    """Serialize a RegistryDatum to inline datum CBOR hex."""
    data = _constr(0, [
        [_encode_member(m) for m in d.members],
        bytes.fromhex(d.admin),
        d.version,
        bytes.fromhex(d.governance_script_hash) if d.governance_script_hash else b"",
    ])
    return _encode(data).hex()


def governance_datum_cbor_hex(d: GovernanceDatum) -> str:
    """Serialize a GovernanceDatum to inline datum CBOR hex."""
    data = _constr(0, [
        bytes.fromhex(d.proposer),
        d.description.encode("utf-8"),
        _encode_action(d.action),
        [_encode_vote(v) for v in d.votes],
        _constr(d.status, []),
        d.vote_deadline,
        d.execute_after,
        d.quorum,
        _encode_output_ref(d.registry_ref),
        d.registry_version,
        d.deposit,
    ])
    return _encode(data).hex()


def treasury_datum_cbor_hex(d: TreasuryDatum) -> str:
    """Serialize a TreasuryDatum to inline datum CBOR hex."""
    data = _constr(0, [
        bytes.fromhex(d.governance_script_hash),
        d.max_transfer_lovelace,
    ])
    return _encode(data).hex()
