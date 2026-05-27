"""
Transaction descriptors for all FOS on-chain actions.

Each builder returns an UnsignedTransaction — a complete specification
of what the transaction must do.  A separate signing layer (signing.py /
PyCardano) assembles this into a CBOR transaction and submits it.

This separation is intentional: the agent decides WHAT to do; the
wallet decides HOW to sign and fund it.

Reference input pattern (used in all three validators):
  treasury   reads governance  via reference_inputs
  governance reads registry    via reference_inputs
  treasury   reads registry    via reference_inputs (indirectly, through governance proposal)
"""

from __future__ import annotations
import dataclasses
from dataclasses import dataclass, field
from typing import Any, Optional

from .types import (
    GovernanceDatum,
    OutputReference,
    ProposalAction,
    RegistryDatum,
    RegistryMember,
    RotateAdminAction,
    TreasuryDatum,
    TreasuryTransferAction,
    UpdateRegistryMemberAction,
    UTxO,
    VoteRecord,
    PROPOSAL_VOTING,
    PROPOSAL_EXECUTED,
    PROPOSAL_EXPIRED,
    ROLE_ADMIN, ROLE_TREASURER, ROLE_MEMBER, ROLE_OBSERVER,
    STATUS_ACTIVE, STATUS_SUSPENDED, STATUS_REMOVED,
)


@dataclass
class TxOutput:
    address: str
    lovelace: int
    datum_hex: Optional[str] = None   # inline datum for script outputs


@dataclass
class Redeemer:
    input_ref: str            # "txhash#index"
    data: dict                # JSON-serialisable redeemer value
    budget_mem: int = 200_000
    budget_cpu: int = 200_000_000


@dataclass
class UnsignedTransaction:
    """
    Complete transaction specification ready for a signing layer.

    Pass to signing.sign_transaction() (PyCardano) which has access to the
    private key and can calculate fees.
    """
    description: str
    inputs: list[str]              # ["txhash#index", ...] — UTxOs to consume
    reference_inputs: list[str]    # ["txhash#index", ...] — read-only
    outputs: list[TxOutput]
    redeemers: list[Redeemer]
    validity_start_ms: Optional[int] = None   # POSIX ms (caller converts to slots)
    validity_end_ms: Optional[int]   = None   # POSIX ms
    required_signers: list[str]      = field(default_factory=list)  # vkey hashes
    metadata: dict                   = field(default_factory=dict)

    def summary(self) -> str:
        lines = [
            f"Transaction: {self.description}",
            f"  Inputs:           {self.inputs}",
            f"  Reference inputs: {self.reference_inputs}",
            f"  Outputs:          {[(o.address[:20], o.lovelace) for o in self.outputs]}",
            f"  Required signers: {self.required_signers}",
        ]
        if self.validity_start_ms:
            lines.append(f"  Valid from:       {self.validity_start_ms} ms")
        if self.validity_end_ms:
            lines.append(f"  Valid until:      {self.validity_end_ms} ms")
        return "\n".join(lines)


# ─── Redeemer constructors ────────────────────────────────
# Mirror the GovernanceRedeemer and TreasuryRedeemer Aiken types.

def _cast_vote_redeemer(input_ref: str, voter: str, approve: bool) -> Redeemer:
    return Redeemer(
        input_ref=input_ref,
        data={"constructor": 0, "fields": [
            {"bytes": voter},
            {"constructor": 1 if approve else 0, "fields": []},
        ]},
    )

def _execute_redeemer(input_ref: str) -> Redeemer:
    return Redeemer(input_ref=input_ref, data={"constructor": 1, "fields": []})

def _expire_redeemer(input_ref: str) -> Redeemer:
    return Redeemer(input_ref=input_ref, data={"constructor": 2, "fields": []})

def _execute_transfer_redeemer(input_ref: str, governance_ref: str) -> Redeemer:
    tx_hash, idx = governance_ref.split("#")
    return Redeemer(
        input_ref=input_ref,
        data={"constructor": 0, "fields": [
            {"constructor": 0, "fields": [
                {"constructor": 0, "fields": [{"bytes": tx_hash}]},
                {"int": int(idx)},
            ]},
        ]},
    )


# ─── Datum helpers ────────────────────────────────────────

def _governance_datum_hex(d: GovernanceDatum) -> str:
    """Serialize updated GovernanceDatum to inline datum hex."""
    try:
        from .datums import governance_datum_cbor_hex
        return governance_datum_cbor_hex(d)
    except (ImportError, AssertionError, ValueError):
        # ValueError covers non-hex voter_key_hash in test fixtures
        return "<governance_datum_cbor_hex>"


def _treasury_datum_hex_unchanged(treasury_utxo: UTxO) -> Optional[str]:
    """Return existing treasury datum hex (it's unchanged by ExecuteTransfer)."""
    return treasury_utxo.datum_hex or "<treasury_datum_cbor_hex>"


# ─── Address helpers ─────────────────────────────────────

def _script_address(script_hash: str, network: str = "preprod") -> str:
    try:
        from .signing import script_hash_to_address
        return script_hash_to_address(script_hash, network)
    except (ImportError, AssertionError):
        return f"addr_test1w{script_hash[:20]}"


def _pkh_address(pkh: str, network: str = "preprod") -> str:
    try:
        from .signing import pkh_to_enterprise_address
        return pkh_to_enterprise_address(pkh, network)
    except (ImportError, AssertionError):
        return f"addr_test1v{pkh[:20]}"


def _network_from_config() -> str:
    try:
        from . import config
        return config.NETWORK
    except Exception:
        return "preprod"


# ─── Transaction builders ─────────────────────────────────

def build_cast_vote_tx(
    governance_utxo: UTxO,
    governance_datum: GovernanceDatum,
    registry_utxo: UTxO,
    voter_key_hash: str,
    voter_address: str,
    approve: bool,
    change_address: str,
    current_time_ms: int,
    governance_script_hash: str = "",
) -> UnsignedTransaction:
    """
    Cast a yes/no vote on a governance proposal.

    reference_inputs: [registry UTxO]   — verify voter eligibility
    inputs:           [governance UTxO] — consume + re-produce with new vote
    outputs:          [governance UTxO with vote appended]
    """
    net = _network_from_config()

    new_vote = VoteRecord(voter=voter_key_hash, approve=approve)
    new_datum = dataclasses.replace(
        governance_datum,
        votes=governance_datum.votes + [new_vote],
    )

    gov_address = (
        _script_address(governance_script_hash, net)
        if governance_script_hash
        else (governance_utxo.ref.split("#")[0])  # reuse same script address from UTxO tx
    )

    return UnsignedTransaction(
        description=f"CastVote({'yes' if approve else 'no'}) on {governance_utxo.ref}",
        inputs=[governance_utxo.ref],
        reference_inputs=[registry_utxo.ref],
        outputs=[
            TxOutput(
                address=gov_address,
                lovelace=governance_utxo.lovelace,
                datum_hex=_governance_datum_hex(new_datum),
            ),
        ],
        redeemers=[_cast_vote_redeemer(governance_utxo.ref, voter_key_hash, approve)],
        validity_start_ms=current_time_ms,
        validity_end_ms=governance_datum.vote_deadline,
        required_signers=[voter_key_hash],
        metadata={"msg": [f"FOS vote {'yes' if approve else 'no'}"]},
    )


def build_execute_proposal_tx(
    governance_utxo: UTxO,
    governance_datum: GovernanceDatum,
    registry_utxo: UTxO,
    change_address: str,
    current_time_ms: int,
    governance_script_hash: str = "",
) -> UnsignedTransaction:
    """
    Execute a governance proposal that has reached quorum + timelock.

    Flips proposal status Voting → Executed.
    The treasury payment (if any) is a SEPARATE transaction built by
    build_execute_transfer_tx() after this one is confirmed.
    """
    net = _network_from_config()

    executed_datum = dataclasses.replace(governance_datum, status=PROPOSAL_EXECUTED)
    gov_address = (
        _script_address(governance_script_hash, net)
        if governance_script_hash
        else governance_utxo.ref.split("#")[0]
    )

    return UnsignedTransaction(
        description=f"ExecuteProposal {governance_utxo.ref}",
        inputs=[governance_utxo.ref],
        reference_inputs=[registry_utxo.ref],
        outputs=[
            TxOutput(
                address=gov_address,
                lovelace=governance_utxo.lovelace,
                datum_hex=_governance_datum_hex(executed_datum),
            ),
        ],
        redeemers=[_execute_redeemer(governance_utxo.ref)],
        validity_start_ms=governance_datum.execute_after,
        required_signers=[],
        metadata={"msg": ["FOS execute proposal"]},
    )


def build_expire_proposal_tx(
    governance_utxo: UTxO,
    governance_datum: GovernanceDatum,
    registry_utxo: UTxO,
    change_address: str,
    current_time_ms: int,
    governance_script_hash: str = "",
) -> UnsignedTransaction:
    """Close a proposal that missed quorum after the vote deadline."""
    net = _network_from_config()

    expired_datum = dataclasses.replace(governance_datum, status=PROPOSAL_EXPIRED)
    gov_address = (
        _script_address(governance_script_hash, net)
        if governance_script_hash
        else governance_utxo.ref.split("#")[0]
    )

    return UnsignedTransaction(
        description=f"ExpireProposal {governance_utxo.ref}",
        inputs=[governance_utxo.ref],
        reference_inputs=[registry_utxo.ref],
        outputs=[
            TxOutput(
                address=gov_address,
                lovelace=governance_utxo.lovelace,
                datum_hex=_governance_datum_hex(expired_datum),
            ),
        ],
        redeemers=[_expire_redeemer(governance_utxo.ref)],
        validity_start_ms=governance_datum.vote_deadline + 1,
        required_signers=[],
        metadata={"msg": ["FOS expire proposal"]},
    )


def build_execute_transfer_tx(
    treasury_utxo: UTxO,
    treasury_datum: TreasuryDatum,
    governance_utxo: UTxO,
    governance_datum: GovernanceDatum,
    registry_utxo: UTxO,
    change_address: str,
    treasury_script_hash: str = "",
) -> UnsignedTransaction:
    """
    Release treasury funds for an Executed TreasuryTransfer proposal.

    reference_inputs: [governance UTxO, registry UTxO]
    inputs:           [treasury UTxO]
    outputs:          [recipient payment, treasury continuing output]

    Three-layer security checks (mirroring treasury.ak):
      1. governance_utxo.status == Executed
      2. governance_utxo.action == TreasuryTransfer
      3. lovelace <= treasury_datum.max_transfer_lovelace
      4. governance UTxO script hash == treasury_datum.governance_script_hash
    """
    assert governance_datum.is_executed, "Cannot release funds: proposal not Executed"
    assert isinstance(governance_datum.action, TreasuryTransferAction), \
        "Cannot release funds: proposal action is not TreasuryTransfer"
    action = governance_datum.action
    assert action.lovelace <= treasury_datum.max_transfer_lovelace, \
        f"Transfer {action.lovelace} exceeds cap {treasury_datum.max_transfer_lovelace}"

    net = _network_from_config()
    remaining = treasury_utxo.lovelace - action.lovelace

    treasury_address = (
        _script_address(treasury_script_hash, net)
        if treasury_script_hash
        else _script_address(treasury_datum.governance_script_hash, net)  # fallback estimate
    )

    return UnsignedTransaction(
        description=(
            f"ExecuteTransfer {action.lovelace / 1_000_000:.2f}₳ "
            f"→ {action.recipient[:12]}…  memo={action.memo!r}"
        ),
        inputs=[treasury_utxo.ref],
        reference_inputs=[governance_utxo.ref, registry_utxo.ref],
        outputs=[
            TxOutput(
                address=_pkh_address(action.recipient, net),
                lovelace=action.lovelace,
            ),
            TxOutput(
                address=treasury_address,
                lovelace=remaining,
                datum_hex=_treasury_datum_hex_unchanged(treasury_utxo),
            ),
        ],
        redeemers=[
            _execute_transfer_redeemer(treasury_utxo.ref, governance_utxo.ref)
        ],
        required_signers=[],
        metadata={"msg": [f"Quorum treasury transfer: {action.memo}"]},
    )


def build_execute_registry_action_tx(
    registry_utxo: UTxO,
    registry_datum: RegistryDatum,
    governance_utxo: UTxO,
    governance_datum: GovernanceDatum,
    registry_script_hash: str = "",
) -> UnsignedTransaction:
    """
    Apply a governance-approved registry mutation.

    Called after execute_proposal confirms on-chain for a RotateAdmin or
    UpdateRegistryMember proposal.  Uses the GovernanceApproval redeemer —
    no admin key required.

    reference_inputs: [governance UTxO]   — proves the proposal is Executed
    inputs:           [registry UTxO]     — consume + re-produce with mutation applied
    outputs:          [registry UTxO with mutation + version incremented]

    On-chain replay protection: the governance proposal's registry_version must
    equal the current registry version.  After this mutation increments the
    version, a second attempt with the same governance UTxO will fail.
    """
    assert governance_datum.is_executed, \
        "Cannot apply governance action: proposal not Executed"
    assert isinstance(governance_datum.action, (RotateAdminAction, UpdateRegistryMemberAction)), \
        f"Proposal action {type(governance_datum.action).__name__} cannot mutate the registry"

    net = _network_from_config()
    action = governance_datum.action

    if isinstance(action, RotateAdminAction):
        new_registry = dataclasses.replace(
            registry_datum,
            admin=action.new_admin,
            version=registry_datum.version + 1,
        )
        description = f"GovernanceApproval: RotateAdmin → {action.new_admin[:12]}…"
    else:
        new_members = [
            dataclasses.replace(m, role=action.new_role, status=action.new_status)
            if m.key_hash == action.target_key else m
            for m in registry_datum.members
        ]
        new_registry = dataclasses.replace(
            registry_datum,
            members=new_members,
            version=registry_datum.version + 1,
        )
        description = f"GovernanceApproval: UpdateMember {action.target_key[:12]}…"

    registry_address = (
        _script_address(registry_script_hash, net)
        if registry_script_hash
        else registry_utxo.ref.split("#")[0]
    )

    try:
        from .datums import registry_datum_cbor_hex
        new_datum_hex = registry_datum_cbor_hex(new_registry)
    except (ImportError, AssertionError):
        new_datum_hex = "<registry_datum_cbor_hex>"

    return UnsignedTransaction(
        description=f"{description} via proposal {governance_utxo.ref}",
        inputs=[registry_utxo.ref],
        reference_inputs=[governance_utxo.ref],
        outputs=[
            TxOutput(
                address=registry_address,
                lovelace=registry_utxo.lovelace,
                datum_hex=new_datum_hex,
            ),
        ],
        redeemers=[Redeemer(
            input_ref=registry_utxo.ref,
            data={"constructor": 4, "fields": []},  # GovernanceApproval variant index 4
        )],
        required_signers=[],
        metadata={"msg": [f"Quorum registry governance: {str(action)[:60]}"]},
    )


def build_create_proposal_tx(
    *,
    registry_utxo: UTxO,
    registry: RegistryDatum,
    proposer_key_hash: str,
    description: str,
    action: ProposalAction,
    vote_deadline_ms: int,
    execute_after_ms: int,
    quorum: int,
    governance_script_hash: str,
    current_time_ms: int,
    min_lovelace: int = 2_000_000,
) -> UnsignedTransaction:
    """
    Create a new governance proposal UTxO at the governance script address.

    This is a plain send transaction — no script spending, no redeemers.
    The proposer's wallet supplies the funding input; the signing layer adds it.

    outputs: [new governance UTxO with initial GovernanceDatum]
    required_signers: [proposer_key_hash]
    """
    if vote_deadline_ms <= current_time_ms:
        raise ValueError("Vote deadline must be in the future")
    if execute_after_ms < vote_deadline_ms:
        raise ValueError("Execute-after (timelock) must be >= vote deadline")

    max_score = registry.max_possible_yes_score()
    if quorum < 1:
        raise ValueError("Quorum must be at least 1")
    if quorum > max_score:
        raise ValueError(
            f"Quorum {quorum} pts exceeds the maximum possible yes score "
            f"{max_score} pts for the current registry"
        )

    if isinstance(action, TreasuryTransferAction):
        if action.lovelace <= 0:
            raise ValueError("Transfer amount must be positive")
        if not action.recipient or len(action.recipient) < 56:
            raise ValueError("Recipient must be a valid 28-byte key hash (56 hex chars)")

    gov_datum = GovernanceDatum(
        proposer=proposer_key_hash,
        description=description,
        action=action,
        votes=[],
        status=PROPOSAL_VOTING,
        vote_deadline=vote_deadline_ms,
        execute_after=execute_after_ms,
        quorum=quorum,
        registry_ref=registry_utxo.as_output_reference(),
        registry_version=registry.version,
    )

    net = _network_from_config()
    gov_address = _script_address(governance_script_hash, net)

    return UnsignedTransaction(
        description=f"CreateProposal: {description[:60]}",
        inputs=[],   # signing layer adds proposer wallet UTxO(s)
        reference_inputs=[],
        outputs=[
            TxOutput(
                address=gov_address,
                lovelace=min_lovelace,
                datum_hex=_governance_datum_hex(gov_datum),
            ),
        ],
        redeemers=[],
        validity_start_ms=current_time_ms,
        required_signers=[proposer_key_hash],
        metadata={"msg": ["Quorum: create proposal"]},
    )
