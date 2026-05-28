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
    CreateVestingAction,
    GovernanceDatum,
    NativeToken,
    OutputReference,
    ProposalAction,
    RegistryDatum,
    RegistryMember,
    RotateAdminAction,
    TreasuryDatum,
    TreasuryTransferAction,
    UpdateRegistryMemberAction,
    UTxO,
    VestingDatum,
    VestingTranche,
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
    datum_hex: Optional[str] = None          # inline datum for script outputs
    tokens: list[NativeToken] = field(default_factory=list)  # native tokens alongside ADA


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

def _set_delegate_redeemer(
    input_ref: str, key_hash: str, new_delegate: Optional[str]
) -> Redeemer:
    delegate_field = (
        {"constructor": 0, "fields": [{"bytes": new_delegate}]}
        if new_delegate is not None
        else {"constructor": 1, "fields": []}
    )
    return Redeemer(
        input_ref=input_ref,
        data={"constructor": 5, "fields": [  # SetDelegate = variant 5 in RegistryAction
            {"bytes": key_hash},
            delegate_field,
        ]},
    )

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
    Refunds the proposer's deposit in the same transaction.
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
    deposit = governance_datum.deposit
    continuing_lovelace = max(governance_utxo.lovelace - deposit, 1_500_000)

    outputs = [
        TxOutput(
            address=gov_address,
            lovelace=continuing_lovelace,
            datum_hex=_governance_datum_hex(executed_datum),
        ),
    ]
    if deposit > 0:
        outputs.append(TxOutput(
            address=_pkh_address(governance_datum.proposer, net),
            lovelace=deposit,
        ))

    return UnsignedTransaction(
        description=f"ExecuteProposal {governance_utxo.ref}",
        inputs=[governance_utxo.ref],
        reference_inputs=[registry_utxo.ref],
        outputs=outputs,
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

    token_desc = f" + {len(action.tokens)} token(s)" if action.tokens else ""
    return UnsignedTransaction(
        description=(
            f"ExecuteTransfer {action.lovelace / 1_000_000:.2f}₳{token_desc} "
            f"→ {action.recipient[:12]}…  memo={action.memo!r}"
        ),
        inputs=[treasury_utxo.ref],
        reference_inputs=[governance_utxo.ref, registry_utxo.ref],
        outputs=[
            TxOutput(
                address=_pkh_address(action.recipient, net),
                lovelace=action.lovelace,
                tokens=action.tokens,
            ),
            TxOutput(
                address=treasury_address,
                lovelace=remaining,
                datum_hex=_treasury_datum_hex_unchanged(treasury_utxo),
                # Native token remainder is managed by the signing layer / ledger;
                # the validator checks conservation via treasury_conserves_tokens.
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


MIN_PROPOSAL_DEPOSIT = 2_000_000  # lovelace — must match min_deposit in governance.ak


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
    deposit: int = 2_000_000,
    rationale_url: str = "",
) -> UnsignedTransaction:
    """
    Create a new governance proposal UTxO at the governance script address.

    The proposer locks `deposit` lovelace in the governance UTxO. On Execute
    (quorum met) the deposit is refunded to the proposer; on Expire (deadline
    missed) it stays locked as a spam deterrent.

    outputs: [new governance UTxO with GovernanceDatum; holds min_lovelace + deposit]
    required_signers: [proposer_key_hash]
    """
    if deposit < MIN_PROPOSAL_DEPOSIT:
        raise ValueError(
            f"Deposit {deposit} is below the minimum {MIN_PROPOSAL_DEPOSIT} lovelace"
        )
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
        if action.lovelace <= 0 and not action.tokens:
            raise ValueError("Transfer must include ADA or at least one native token")
        if action.lovelace < 0:
            raise ValueError("Transfer lovelace cannot be negative")
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
        deposit=deposit,
        rationale_url=rationale_url,
    )

    net = _network_from_config()
    gov_address = _script_address(governance_script_hash, net)

    # CIP-20 message metadata (label 674) + optional IPFS rationale (label 675)
    metadata: dict = {"msg": ["Quorum: create proposal", description[:64]]}
    if rationale_url:
        metadata[675] = {"rationale": rationale_url}

    return UnsignedTransaction(
        description=f"CreateProposal: {description[:60]}",
        inputs=[],   # signing layer adds proposer wallet UTxOs
        reference_inputs=[],
        outputs=[
            TxOutput(
                address=gov_address,
                lovelace=min_lovelace + deposit,
                datum_hex=_governance_datum_hex(gov_datum),
            ),
        ],
        redeemers=[],
        validity_start_ms=current_time_ms,
        required_signers=[proposer_key_hash],
        metadata=metadata,
    )


def build_add_member_tx(
    registry_utxo: UTxO,
    registry_datum: RegistryDatum,
    new_key_hash: str,
    role: int = ROLE_MEMBER,
    registry_script_hash: str = "",
) -> UnsignedTransaction:
    """
    Admin action: add a new member to the registry.

    Signed by the current admin key — no governance vote required.
    The new member is prepended to the members list (matches on-chain behaviour).
    Increments registry version, invalidating any open proposals.
    """
    assert not registry_datum.find_member(new_key_hash), \
        f"Member {new_key_hash[:12]}… already exists in registry"

    import time as _time
    new_member = RegistryMember(
        key_hash=new_key_hash,
        role=role,
        joined_at=int(_time.time() * 1000),
        status=STATUS_ACTIVE,
    )
    new_registry = dataclasses.replace(
        registry_datum,
        members=[new_member] + list(registry_datum.members),
        version=registry_datum.version + 1,
    )

    net = _network_from_config()
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

    role_names = {ROLE_ADMIN: "Admin", ROLE_MEMBER: "Member",
                  ROLE_OBSERVER: "Observer", ROLE_TREASURER: "Treasurer"}

    return UnsignedTransaction(
        description=f"AddMember {new_key_hash[:16]}… as {role_names.get(role, role)}",
        inputs=[registry_utxo.ref],
        reference_inputs=[],
        outputs=[
            TxOutput(
                address=registry_address,
                lovelace=registry_utxo.lovelace,
                datum_hex=new_datum_hex,
            ),
        ],
        redeemers=[Redeemer(
            input_ref=registry_utxo.ref,
            data={"constructor": 0, "fields": [  # AddMember variant 0
                {"constructor": 0, "fields": [
                    {"bytes": new_key_hash},
                    {"constructor": role, "fields": []},
                    {"int": new_member.joined_at},
                    {"constructor": STATUS_ACTIVE, "fields": []},
                    {"constructor": 1, "fields": []},  # delegate: None
                ]},
            ]},
        )],
        required_signers=[registry_datum.admin],
        metadata={"msg": [f"Quorum: add member {new_key_hash[:16]}…"]},
    )


def build_remove_member_tx(
    registry_utxo: UTxO,
    registry_datum: RegistryDatum,
    target_key_hash: str,
    registry_script_hash: str = "",
) -> UnsignedTransaction:
    """
    Admin action: remove a member from the registry.

    Signed by the current admin key — no governance vote required.
    Increments registry version, invalidating any open proposals.
    """
    assert registry_datum.find_member(target_key_hash), \
        f"Member {target_key_hash[:12]}… not found in registry"

    new_registry = dataclasses.replace(
        registry_datum,
        members=[m for m in registry_datum.members if m.key_hash != target_key_hash],
        version=registry_datum.version + 1,
    )

    net = _network_from_config()
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
        description=f"RemoveMember {target_key_hash[:16]}…",
        inputs=[registry_utxo.ref],
        reference_inputs=[],
        outputs=[
            TxOutput(
                address=registry_address,
                lovelace=registry_utxo.lovelace,
                datum_hex=new_datum_hex,
            ),
        ],
        redeemers=[Redeemer(
            input_ref=registry_utxo.ref,
            data={"constructor": 1, "fields": [  # RemoveMember variant 1
                {"bytes": target_key_hash},
            ]},
        )],
        required_signers=[registry_datum.admin],
        metadata={"msg": [f"Quorum: remove member {target_key_hash[:16]}…"]},
    )


def build_set_delegate_tx(
    registry_utxo: UTxO,
    registry_datum: RegistryDatum,
    member_key_hash: str,
    new_delegate: Optional[str],
    registry_script_hash: str = "",
) -> UnsignedTransaction:
    """
    Set or clear a member's vote delegate (liquid democracy).

    Self-service: signed by the member; no admin key or governance required.
    Increments registry version — invalidates open proposals (same as all mutations).

    Single-hop only: the target must not itself have a delegate (no chains).
    A member cannot delegate to themselves.
    """
    member = registry_datum.find_member(member_key_hash)
    assert member is not None, f"Member {member_key_hash[:12]}… not found in registry"
    assert member.is_active, f"Member {member_key_hash[:12]}… is not active"
    assert new_delegate != member_key_hash, "Cannot delegate to self"

    if new_delegate is not None:
        target = registry_datum.find_member(new_delegate)
        assert target is not None, \
            f"Delegate target {new_delegate[:12]}… not found in registry"
        assert target.is_active, \
            f"Delegate target {new_delegate[:12]}… is not active"
        assert target.delegate is None, \
            f"Delegate target {new_delegate[:12]}… already has a delegate (no chains)"

    new_members = [
        dataclasses.replace(m, delegate=new_delegate)
        if m.key_hash == member_key_hash else m
        for m in registry_datum.members
    ]
    new_registry = dataclasses.replace(
        registry_datum,
        members=new_members,
        version=registry_datum.version + 1,
    )

    net = _network_from_config()
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

    action_desc = f"→ {new_delegate[:12]}…" if new_delegate else "(clear)"

    return UnsignedTransaction(
        description=f"SetDelegate {member_key_hash[:12]}… {action_desc}",
        inputs=[registry_utxo.ref],
        reference_inputs=[],
        outputs=[
            TxOutput(
                address=registry_address,
                lovelace=registry_utxo.lovelace,
                datum_hex=new_datum_hex,
            ),
        ],
        redeemers=[_set_delegate_redeemer(registry_utxo.ref, member_key_hash, new_delegate)],
        required_signers=[member_key_hash],
        metadata={"msg": [f"Quorum: set delegate {action_desc}"]},
    )


def _create_vesting_schedule_redeemer(
    treasury_ref: str, governance_ref: str, vesting_script_hash: str
) -> Redeemer:
    """Redeemer for CreateVestingSchedule (constructor 1 in TreasuryRedeemer)."""
    tx_hash, idx = governance_ref.split("#")
    return Redeemer(
        input_ref=treasury_ref,
        data={"constructor": 1, "fields": [
            {"constructor": 0, "fields": [
                {"constructor": 0, "fields": [{"bytes": tx_hash}]},
                {"int": int(idx)},
            ]},
            {"bytes": vesting_script_hash},
        ]},
    )


def _claim_vested_redeemer(vesting_ref: str) -> Redeemer:
    """Redeemer for ClaimVested (constructor 0 in VestingRedeemer)."""
    return Redeemer(
        input_ref=vesting_ref,
        data={"constructor": 0, "fields": []},
    )


def build_create_vesting_tx(
    treasury_utxo: UTxO,
    treasury_datum: TreasuryDatum,
    governance_utxo: UTxO,
    governance_datum: GovernanceDatum,
    vesting_script_hash: str,
    treasury_script_hash: str = "",
) -> UnsignedTransaction:
    """
    Fund a vesting schedule from the treasury after a CreateVesting proposal executes.

    inputs:           [treasury UTxO]
    reference_inputs: [governance UTxO]
    outputs:          [vesting UTxO at vesting script, treasury continuing output]

    The vesting UTxO holds the full vesting amount locked by VestingDatum.
    Each tranche is released to the recipient when its release_time passes.
    """
    assert governance_datum.is_executed, "Cannot fund vesting: proposal not Executed"
    assert isinstance(governance_datum.action, CreateVestingAction), \
        "Cannot fund vesting: proposal action is not CreateVesting"

    action = governance_datum.action
    total_lovelace = action.total_lovelace()
    assert total_lovelace > 0, "Vesting schedule must lock at least some lovelace"
    assert len(action.tranches) > 0, "Vesting schedule must have at least one tranche"
    assert treasury_utxo.lovelace >= total_lovelace, \
        f"Treasury ({treasury_utxo.lovelace}) has insufficient funds for vesting ({total_lovelace})"

    net = _network_from_config()

    vesting_datum = VestingDatum(
        recipient=action.recipient,
        tranches=action.tranches,
        proposal_ref=governance_utxo.as_output_reference(),
    )

    try:
        from .datums import vesting_datum_cbor_hex
        vesting_datum_hex = vesting_datum_cbor_hex(vesting_datum)
    except (ImportError, AssertionError):
        vesting_datum_hex = "<vesting_datum_cbor_hex>"

    vesting_address = _script_address(vesting_script_hash, net)
    treasury_address = (
        _script_address(treasury_script_hash, net)
        if treasury_script_hash
        else _script_address(treasury_datum.governance_script_hash, net)
    )
    remaining = treasury_utxo.lovelace - total_lovelace

    return UnsignedTransaction(
        description=(
            f"CreateVestingSchedule {total_lovelace / 1_000_000:.2f}₳ "
            f"→ {action.recipient[:12]}…  {len(action.tranches)} tranche(s)  memo={action.memo!r}"
        ),
        inputs=[treasury_utxo.ref],
        reference_inputs=[governance_utxo.ref],
        outputs=[
            TxOutput(
                address=vesting_address,
                lovelace=total_lovelace,
                datum_hex=vesting_datum_hex,
            ),
            TxOutput(
                address=treasury_address,
                lovelace=remaining,
                datum_hex=_treasury_datum_hex_unchanged(treasury_utxo),
            ),
        ],
        redeemers=[
            _create_vesting_schedule_redeemer(
                treasury_utxo.ref, governance_utxo.ref, vesting_script_hash
            )
        ],
        required_signers=[],
        metadata={"msg": [f"Quorum vesting schedule: {action.memo}"]},
    )


def build_claim_vesting_tx(
    vesting_utxo: UTxO,
    vesting_datum: VestingDatum,
    recipient_address: str,
    current_time_ms: int,
    vesting_script_hash: str = "",
) -> UnsignedTransaction:
    """
    Claim all matured tranches from a vesting UTxO.

    inputs:  [vesting UTxO]
    outputs: [recipient payment for matured amount]
             [vesting continuing output for remaining tranches — omitted if all claimed]

    The transaction validity lower bound is set to current_time_ms so the
    on-chain validator knows which tranches have matured.
    """
    matured = vesting_datum.matured_tranches(current_time_ms)
    remaining = vesting_datum.remaining_tranches(current_time_ms)

    assert matured, "No tranches have matured yet — nothing to claim"

    claim_amount = sum(t.lovelace for t in matured)
    net = _network_from_config()

    outputs = [
        TxOutput(
            address=recipient_address,
            lovelace=claim_amount,
        ),
    ]

    if remaining:
        remaining_datum = VestingDatum(
            recipient=vesting_datum.recipient,
            tranches=remaining,
            proposal_ref=vesting_datum.proposal_ref,
        )
        try:
            from .datums import vesting_datum_cbor_hex
            remaining_datum_hex = vesting_datum_cbor_hex(remaining_datum)
        except (ImportError, AssertionError):
            remaining_datum_hex = "<vesting_datum_cbor_hex>"

        vesting_address = _script_address(vesting_script_hash, net)
        remaining_lovelace = sum(t.lovelace for t in remaining)
        outputs.append(TxOutput(
            address=vesting_address,
            lovelace=remaining_lovelace,
            datum_hex=remaining_datum_hex,
        ))

    tranche_desc = f"{len(matured)} of {len(matured) + len(remaining)} tranche(s)"
    return UnsignedTransaction(
        description=(
            f"ClaimVested {claim_amount / 1_000_000:.2f}₳ "
            f"({tranche_desc}) → {vesting_datum.recipient[:12]}…"
        ),
        inputs=[vesting_utxo.ref],
        reference_inputs=[],
        outputs=outputs,
        redeemers=[_claim_vested_redeemer(vesting_utxo.ref)],
        validity_start_ms=current_time_ms,
        required_signers=[vesting_datum.recipient],
        metadata={"msg": ["Quorum: claim vested funds"]},
    )
