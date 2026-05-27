"""
Python mirrors of fos_types.ak — the shared Aiken type library.

Includes CBOR deserialization so the agent can read on-chain datums
returned by Blockfrost and reason about them in Python.

Cardano PlutusData CBOR encoding for Aiken types:
  record           → Constr(0, [field0, field1, ...])
  enum variant N   → Constr(N, [])            (N=0 for first variant)
  ByteArray        → CBOR bytes
  Int              → CBOR integer
  List<a>          → CBOR array of a
  Bool True        → Constr(1, [])
  Bool False       → Constr(0, [])

cbor2 represents Constr(N) as CBORTag(121+N) for N=0..6,
and CBORTag(1280+N-7) for N>=7.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional, Union

try:
    import cbor2
    _CBOR_AVAILABLE = True
except ImportError:
    _CBOR_AVAILABLE = False


# ─── CBOR helpers ─────────────────────────────────────────

def _alt(val) -> int:
    """Constructor alternative index from a cbor2 CBORTag."""
    t = val.tag
    if 121 <= t <= 127:
        return t - 121
    if t >= 1280:
        return t - 1280 + 7
    raise ValueError(f"Unexpected PlutusData tag: {t}")


def _f(val) -> list:
    """Fields of a Constr value."""
    return val.value


def _decode(hex_str: str):
    assert _CBOR_AVAILABLE, "cbor2 is required: pip install cbor2"
    return cbor2.loads(bytes.fromhex(hex_str))


# ─── Role ─────────────────────────────────────────────────

ROLE_ADMIN, ROLE_TREASURER, ROLE_MEMBER, ROLE_OBSERVER = 0, 1, 2, 3
ROLE_NAMES   = {0: "Admin", 1: "Treasurer", 2: "Member", 3: "Observer"}
VOTE_WEIGHTS = {0: 3,       1: 2,           2: 1,        3: 0}


# ─── MemberStatus ─────────────────────────────────────────

STATUS_ACTIVE, STATUS_SUSPENDED, STATUS_REMOVED = 0, 1, 2
STATUS_NAMES = {0: "Active", 1: "Suspended", 2: "Removed"}


# ─── ProposalStatus ───────────────────────────────────────

PROPOSAL_VOTING, PROPOSAL_EXECUTED, PROPOSAL_EXPIRED = 0, 1, 2
PROPOSAL_STATUS_NAMES = {0: "Voting", 1: "Executed", 2: "Expired"}


# ─── RegistryMember ───────────────────────────────────────

@dataclass
class RegistryMember:
    key_hash: str   # hex VerificationKeyHash
    role: int
    joined_at: int  # POSIX ms block time
    status: int
    delegate: Optional[str] = None  # hex key hash of delegate (liquid democracy), or None

    @classmethod
    def from_cbor(cls, val) -> RegistryMember:
        f = _f(val)
        delegate = None
        if len(f) > 4:
            alt = _alt(f[4])
            if alt == 0:  # Some(value)
                delegate = bytes(_f(f[4])[0]).hex()
            # alt == 1 → None
        return cls(
            key_hash=bytes(f[0]).hex(),
            role=_alt(f[1]),
            joined_at=int(f[2]),
            status=_alt(f[3]),
            delegate=delegate,
        )

    @property
    def is_active(self) -> bool:
        return self.status == STATUS_ACTIVE

    @property
    def vote_weight(self) -> int:
        return VOTE_WEIGHTS.get(self.role, 0)

    @property
    def can_vote(self) -> bool:
        return self.is_active and self.vote_weight > 0

    def __str__(self) -> str:
        return (
            f"{self.key_hash[:12]}… "
            f"({ROLE_NAMES.get(self.role, '?')}, {STATUS_NAMES.get(self.status, '?')})"
        )


# ─── RegistryDatum ────────────────────────────────────────

@dataclass
class RegistryDatum:
    members: list[RegistryMember]
    admin: str   # hex
    version: int
    governance_script_hash: str = ""  # hex — governs which governance contract can mutate this registry

    @classmethod
    def from_cbor_hex(cls, hex_str: str) -> RegistryDatum:
        data = _decode(hex_str)
        f = _f(data)
        return cls(
            members=[RegistryMember.from_cbor(m) for m in f[0]],
            admin=bytes(f[1]).hex(),
            version=int(f[2]),
            governance_script_hash=bytes(f[3]).hex() if len(f) > 3 else "",
        )

    def find_member(self, key_hash: str) -> Optional[RegistryMember]:
        return next((m for m in self.members if m.key_hash == key_hash), None)

    def active_voting_members(self) -> list[RegistryMember]:
        return [m for m in self.members if m.can_vote]

    def max_possible_yes_score(self) -> int:
        return sum(m.vote_weight for m in self.active_voting_members())


# ─── ProposalAction ───────────────────────────────────────

@dataclass
class NativeToken:
    """Mirrors fos_types.NativeToken — a single native token component."""
    policy_id: str   # 28-byte hex
    asset_name: str  # hex or UTF-8 label
    quantity: int

    @classmethod
    def from_cbor(cls, val) -> NativeToken:
        f = _f(val)
        return cls(
            policy_id=bytes(f[0]).hex(),
            asset_name=bytes(f[1]).hex(),
            quantity=int(f[2]),
        )

    def asset_name_str(self) -> str:
        """Try to decode asset_name as UTF-8; fall back to hex."""
        try:
            return bytes.fromhex(self.asset_name).decode("utf-8")
        except Exception:
            return self.asset_name

    def __str__(self) -> str:
        return f"{self.quantity} {self.asset_name_str()} ({self.policy_id[:8]}…)"


@dataclass
class TreasuryTransferAction:
    recipient: str   # hex
    lovelace: int
    memo: str
    tokens: list[NativeToken] = field(default_factory=list)

    @classmethod
    def from_cbor_fields(cls, fields) -> TreasuryTransferAction:
        tokens = (
            [NativeToken.from_cbor(t) for t in fields[3]]
            if len(fields) > 3 else []
        )
        return cls(
            recipient=bytes(fields[0]).hex(),
            lovelace=int(fields[1]),
            memo=bytes(fields[2]).decode("utf-8", errors="replace"),
            tokens=tokens,
        )

    def __str__(self) -> str:
        ada = self.lovelace / 1_000_000
        tok = f" + {len(self.tokens)} token(s)" if self.tokens else ""
        return f"TreasuryTransfer → {self.recipient[:12]}… {ada:.2f}₳{tok}  memo={self.memo!r}"


@dataclass
class RotateAdminAction:
    new_admin: str

    @classmethod
    def from_cbor_fields(cls, fields) -> RotateAdminAction:
        return cls(new_admin=bytes(fields[0]).hex())

    def __str__(self) -> str:
        return f"RotateAdmin → {self.new_admin[:12]}…"


@dataclass
class UpdateRegistryMemberAction:
    target_key: str
    new_role: int
    new_status: int

    @classmethod
    def from_cbor_fields(cls, fields) -> UpdateRegistryMemberAction:
        return cls(
            target_key=bytes(fields[0]).hex(),
            new_role=_alt(fields[1]),
            new_status=_alt(fields[2]),
        )

    def __str__(self) -> str:
        return (
            f"UpdateMember {self.target_key[:12]}… → "
            f"{ROLE_NAMES.get(self.new_role, '?')}/{STATUS_NAMES.get(self.new_status, '?')}"
        )


@dataclass
class OffChainDecisionAction:
    memo: str

    @classmethod
    def from_cbor_fields(cls, fields) -> OffChainDecisionAction:
        return cls(memo=bytes(fields[0]).decode("utf-8", errors="replace"))

    def __str__(self) -> str:
        return f"OffChainDecision: {self.memo!r}"


ProposalAction = Union[
    TreasuryTransferAction,
    RotateAdminAction,
    UpdateRegistryMemberAction,
    OffChainDecisionAction,
]

# Mirrors governance.ak: high_value_lovelace constant
SUPERMAJORITY_HIGH_VALUE_LOVELACE = 10_000_000


def action_requires_supermajority(action: ProposalAction) -> bool:
    """True when the on-chain Execute arm will enforce a 2/3 supermajority."""
    if isinstance(action, RotateAdminAction):
        return True
    if isinstance(action, TreasuryTransferAction):
        return action.lovelace > SUPERMAJORITY_HIGH_VALUE_LOVELACE
    return False


def parse_proposal_action(val) -> ProposalAction:
    alt = _alt(val)
    f = _f(val)
    if alt == 0:
        return TreasuryTransferAction.from_cbor_fields(f)
    elif alt == 1:
        return RotateAdminAction.from_cbor_fields(f)
    elif alt == 2:
        return UpdateRegistryMemberAction.from_cbor_fields(f)
    else:
        return OffChainDecisionAction.from_cbor_fields(f)


# ─── VoteRecord ───────────────────────────────────────────

@dataclass
class VoteRecord:
    voter: str   # hex
    approve: bool

    @classmethod
    def from_cbor(cls, val) -> VoteRecord:
        f = _f(val)
        return cls(
            voter=bytes(f[0]).hex(),
            approve=(_alt(f[1]) == 1),  # Aiken Bool: True=Constr(1), False=Constr(0)
        )


# ─── OutputReference ──────────────────────────────────────

@dataclass
class OutputReference:
    tx_hash: str
    output_index: int

    @classmethod
    def from_cbor(cls, val) -> OutputReference:
        f = _f(val)                         # [TransactionId_constr, output_index]
        tx_id_fields = _f(f[0])             # TransactionId { hash: ByteArray }
        return cls(
            tx_hash=bytes(tx_id_fields[0]).hex(),
            output_index=int(f[1]),
        )

    def __str__(self) -> str:
        return f"{self.tx_hash}#{self.output_index}"


# ─── GovernanceDatum ──────────────────────────────────────

@dataclass
class GovernanceDatum:
    proposer: str
    description: str
    action: ProposalAction
    votes: list[VoteRecord]
    status: int
    vote_deadline: int   # POSIX ms
    execute_after: int   # POSIX ms
    quorum: int
    registry_ref: OutputReference
    registry_version: int
    deposit: int = 0     # lovelace locked by proposer; refunded on Execute, forfeited on Expire
    rationale_url: str = ""  # IPFS CID or URL (Python-only, stored in tx metadata not datum)

    @classmethod
    def from_cbor_hex(cls, hex_str: str) -> GovernanceDatum:
        data = _decode(hex_str)
        f = _f(data)
        return cls(
            proposer=bytes(f[0]).hex(),
            description=bytes(f[1]).decode("utf-8", errors="replace"),
            action=parse_proposal_action(f[2]),
            votes=[VoteRecord.from_cbor(v) for v in f[3]],
            status=_alt(f[4]),
            vote_deadline=int(f[5]),
            execute_after=int(f[6]),
            quorum=int(f[7]),
            registry_ref=OutputReference.from_cbor(f[8]),
            registry_version=int(f[9]),
            deposit=int(f[10]) if len(f) > 10 else 0,
        )

    @property
    def is_voting(self) -> bool:
        return self.status == PROPOSAL_VOTING

    @property
    def is_executed(self) -> bool:
        return self.status == PROPOSAL_EXECUTED

    @property
    def is_expired(self) -> bool:
        return self.status == PROPOSAL_EXPIRED

    def weighted_yes_score(self, registry: RegistryDatum) -> int:
        """Weighted yes vote count — mirrors count_weighted_yes in governance.ak.

        Liquid democracy: when a yes voter has delegators who did NOT vote
        directly, those delegators' weights are added to the voter's score.
        """
        direct_voters = {v.voter for v in self.votes}
        yes_voters = {v.voter for v in self.votes if v.approve}
        score = 0
        for voter_key in yes_voters:
            member = registry.find_member(voter_key)
            if not member or not member.is_active:
                continue
            score += member.vote_weight
            # Add weight from active delegators who haven't voted directly
            for m in registry.members:
                if (m.delegate == voter_key and m.is_active
                        and m.key_hash not in direct_voters):
                    score += m.vote_weight
        return score

    def supermajority_met(self, registry: RegistryDatum) -> bool:
        """True when yes_score * 3 >= max_possible_score * 2 (mirrors supermajority_met in governance.ak)."""
        yes = self.weighted_yes_score(registry)
        max_score = registry.max_possible_yes_score()
        return yes * 3 >= max_score * 2

    def quorum_met(self, registry: RegistryDatum) -> bool:
        yes = self.weighted_yes_score(registry)
        if yes < self.quorum:
            return False
        if action_requires_supermajority(self.action):
            return self.supermajority_met(registry)
        return True

    def already_voted(self, key_hash: str) -> bool:
        return any(v.voter == key_hash for v in self.votes)

    def status_label(self) -> str:
        return PROPOSAL_STATUS_NAMES.get(self.status, "Unknown")


# ─── TreasuryDatum ────────────────────────────────────────

@dataclass
class TreasuryDatum:
    governance_script_hash: str  # hex
    max_transfer_lovelace: int

    @classmethod
    def from_cbor_hex(cls, hex_str: str) -> TreasuryDatum:
        data = _decode(hex_str)
        f = _f(data)
        return cls(
            governance_script_hash=bytes(f[0]).hex(),
            max_transfer_lovelace=int(f[1]),
        )


# ─── UTxO ─────────────────────────────────────────────────

@dataclass
class UTxO:
    tx_hash: str
    output_index: int
    lovelace: int
    datum_hex: Optional[str] = None
    datum: Any = None              # parsed datum if available

    @property
    def ref(self) -> str:
        return f"{self.tx_hash}#{self.output_index}"

    def as_output_reference(self) -> OutputReference:
        return OutputReference(tx_hash=self.tx_hash, output_index=self.output_index)
