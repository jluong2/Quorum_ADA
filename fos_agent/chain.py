"""
Blockfrost client — reads the current on-chain state of all three FOS validators.

Returns typed Python objects (RegistryDatum, GovernanceDatum, TreasuryDatum)
so the agent can reason about them directly.

Requires:  pip install requests cbor2
Optional:  BLOCKFROST_PROJECT_ID env var (falls back to mock data if absent)
"""

from __future__ import annotations
import time
from dataclasses import dataclass
from typing import Optional

try:
    import requests
    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False

from .types import (
    GovernanceDatum,
    OutputReference,
    RegistryDatum,
    RegistryMember,
    TreasuryDatum,
    UTxO,
    VoteRecord,
    PROPOSAL_VOTING,
    PROPOSAL_EXECUTED,
    ROLE_MEMBER,
    STATUS_ACTIVE,
    TreasuryTransferAction,
    OffChainDecisionAction,
)


@dataclass
class FOSState:
    """Snapshot of all three FOS validator states at a point in time."""
    registry: RegistryDatum
    registry_utxo: UTxO                            # the UTxO holding the registry datum
    proposals: list[tuple[UTxO, GovernanceDatum]]  # (utxo, parsed datum)
    treasury_utxo: UTxO
    treasury_datum: TreasuryDatum
    current_time_ms: int   # POSIX milliseconds

    @property
    def active_proposals(self) -> list[tuple[UTxO, GovernanceDatum]]:
        return [(u, d) for u, d in self.proposals if d.is_voting]

    @property
    def executable_proposals(self) -> list[tuple[UTxO, GovernanceDatum]]:
        """Proposals that have reached quorum and cleared the timelock."""
        return [
            (u, d) for u, d in self.active_proposals
            if d.quorum_met(self.registry)
            and self.current_time_ms >= d.execute_after
        ]

    @property
    def expirable_proposals(self) -> list[tuple[UTxO, GovernanceDatum]]:
        """Voting proposals whose deadline has passed without reaching quorum."""
        return [
            (u, d) for u, d in self.active_proposals
            if self.current_time_ms > d.vote_deadline
            and not d.quorum_met(self.registry)
        ]

    @property
    def executed_proposals(self) -> list[tuple[UTxO, GovernanceDatum]]:
        """Executed proposals that haven't triggered a treasury payment yet."""
        return [(u, d) for u, d in self.proposals if d.is_executed]

    @property
    def unreachable_quorum_proposals(self) -> list[tuple[UTxO, GovernanceDatum]]:
        """Active proposals where quorum can never be reached — all remaining voters
        could vote yes and the threshold still wouldn't be met."""
        max_score = self.registry.max_possible_yes_score()
        return [
            (u, d) for u, d in self.active_proposals
            if max_score < d.quorum
            and self.current_time_ms <= d.vote_deadline
        ]


class BlockfrostClient:
    """
    Thin wrapper around the Blockfrost REST API.

    If BLOCKFROST_PROJECT_ID is not set, all reads return mock data
    so the agent can be developed and tested offline.
    """

    def __init__(self, project_id: str, base_url: str):
        self._project_id = project_id
        self._base_url = base_url.rstrip("/")
        self._mock_mode = not project_id

    def _get(self, path: str, params: dict = None) -> dict | list:
        if self._mock_mode:
            return self._mock_response(path)
        assert _REQUESTS_AVAILABLE, "pip install requests"
        url = f"{self._base_url}/{path.lstrip('/')}"
        resp = requests.get(
            url,
            headers={"project_id": self._project_id},
            params=params or {},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def get_utxos_at_script(self, script_hash: str) -> list[UTxO]:
        """Return all UTxOs locked at a script address, with inline datums."""
        address = self._script_hash_to_address(script_hash)
        raw = self._get(f"addresses/{address}/utxos")
        utxos = []
        for item in raw:
            lovelace = next(
                (int(a["quantity"]) for a in item.get("amount", []) if a["unit"] == "lovelace"),
                0,
            )
            datum_hex = item.get("inline_datum") or ""
            utxos.append(UTxO(
                tx_hash=item["tx_hash"],
                output_index=item["output_index"],
                lovelace=lovelace,
                datum_hex=datum_hex,
            ))
        return utxos

    def get_current_slot(self) -> int:
        data = self._get("blocks/latest")
        return int(data.get("slot", 0))

    def submit_tx(self, cbor_hex: str) -> str:
        """Submit a signed transaction. Returns tx hash."""
        if self._mock_mode:
            return "mock_tx_hash_" + cbor_hex[:8]
        assert _REQUESTS_AVAILABLE
        url = f"{self._base_url}/tx/submit"
        resp = requests.post(
            url,
            headers={
                "project_id": self._project_id,
                "Content-Type": "application/cbor",
            },
            data=bytes.fromhex(cbor_hex),
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def _script_hash_to_address(self, script_hash: str) -> str:
        """Convert script hash to bech32 address via Blockfrost."""
        if self._mock_mode:
            return f"addr_test1w{script_hash[:20]}"
        data = self._get(f"scripts/{script_hash}")
        return data.get("address", "")

    def _mock_response(self, path: str) -> dict | list:
        """Return plausible mock data for offline development."""
        if "utxos" in path:
            return _MOCK_UTXOS.get(path.split("/")[1], [])
        if "blocks/latest" in path:
            return {"slot": 12345678, "time": int(time.time())}
        return {}


# ─── High-level FOS state reader ──────────────────────────

def read_fos_state(
    client: BlockfrostClient,
    registry_script_hash: str,
    governance_script_hash: str,
    treasury_script_hash: str,
) -> FOSState:
    """
    Read and parse the current state of all three FOS validators.
    This is the primary entry point for the agent's state awareness.
    """
    # ── Identity registry (single UTxO) ──
    reg_utxos = client.get_utxos_at_script(registry_script_hash)
    assert reg_utxos, "No UTxO found at registry script — is it deployed?"
    reg_utxo = reg_utxos[0]
    registry = RegistryDatum.from_cbor_hex(reg_utxo.datum_hex)
    reg_utxo.datum = registry

    # ── Governance (one UTxO per proposal) ──
    gov_utxos = client.get_utxos_at_script(governance_script_hash)
    proposals = []
    for utxo in gov_utxos:
        if utxo.datum_hex:
            gov_datum = GovernanceDatum.from_cbor_hex(utxo.datum_hex)
            utxo.datum = gov_datum
            proposals.append((utxo, gov_datum))

    # ── Treasury (single config UTxO) ──
    treas_utxos = client.get_utxos_at_script(treasury_script_hash)
    assert treas_utxos, "No UTxO found at treasury script — is it deployed?"
    treas_utxo = treas_utxos[0]
    treas_datum = TreasuryDatum.from_cbor_hex(treas_utxo.datum_hex)
    treas_utxo.datum = treas_datum

    return FOSState(
        registry=registry,
        registry_utxo=reg_utxo,
        proposals=proposals,
        treasury_utxo=treas_utxo,
        treasury_datum=treas_datum,
        current_time_ms=int(time.time() * 1000),
    )


# ─── Demo state for offline UI development ────────────────

def mock_fos_state() -> FOSState:
    """
    Return a realistic FOSState with fake on-chain data.
    Used by the web UI when BLOCKFROST_PROJECT_ID is not set,
    so the dashboard renders without a deployed contract.
    """
    now = int(time.time() * 1000)

    # ── Registry ──────────────────────────────────────────
    members = [
        RegistryMember(key_hash="a1b2c3d4" * 7, role=0,  joined_at=now - 30*86400000, status=0),  # Admin
        RegistryMember(key_hash="b2c3d4e5" * 7, role=1,  joined_at=now - 20*86400000, status=0),  # Treasurer
        RegistryMember(key_hash="c3d4e5f6" * 7, role=2,  joined_at=now - 10*86400000, status=0),  # Member
        RegistryMember(key_hash="d4e5f6a7" * 7, role=2,  joined_at=now -  5*86400000, status=1),  # Member (suspended)
        RegistryMember(key_hash="e5f6a7b8" * 7, role=3,  joined_at=now -  2*86400000, status=0),  # Observer
    ]
    registry = RegistryDatum(members=members, admin="a1b2c3d4" * 7, version=3)
    reg_utxo = UTxO(
        tx_hash="reg0tx" + "0" * 58,
        output_index=0,
        lovelace=3_000_000,
        datum_hex="",
    )
    reg_utxo.datum = registry

    # ── Proposals ─────────────────────────────────────────
    gov_ref = OutputReference(tx_hash="reg0tx" + "0" * 58, output_index=0)

    # Proposal 1: active TreasuryTransfer, quorum partially met
    action1 = TreasuryTransferAction(
        recipient="c3d4e5f6" * 7,
        lovelace=2_000_000,
        memo="Q2 dev grant",
    )
    votes1 = [
        VoteRecord(voter="a1b2c3d4" * 7, approve=True),   # Admin = 3 pts
        VoteRecord(voter="c3d4e5f6" * 7, approve=True),   # Member = 1 pt
    ]
    gov1 = GovernanceDatum(
        proposer="b2c3d4e5" * 7,
        description="Q2 development grant — 2 ADA to core contributor",
        action=action1,
        votes=votes1,
        status=PROPOSAL_VOTING,
        vote_deadline=now + 2 * 86400000,   # 2 days
        execute_after=now + 3 * 86400000,   # 3 days
        quorum=4,
        registry_ref=gov_ref,
        registry_version=3,
    )
    utxo1 = UTxO(tx_hash="gov1tx0" + "0" * 57, output_index=0, lovelace=2_000_000, datum_hex="")
    utxo1.datum = gov1

    # Proposal 2: active OffChainDecision, quorum met + timelock cleared → executable
    action2 = OffChainDecisionAction(memo="Adopt MIT licence for all open-source repos")
    votes2 = [
        VoteRecord(voter="a1b2c3d4" * 7, approve=True),   # Admin = 3 pts
        VoteRecord(voter="b2c3d4e5" * 7, approve=True),   # Treasurer = 2 pts
    ]
    gov2 = GovernanceDatum(
        proposer="a1b2c3d4" * 7,
        description="Adopt MIT licence across all public repositories",
        action=action2,
        votes=votes2,
        status=PROPOSAL_VOTING,
        vote_deadline=now - 1 * 86400000,   # passed yesterday
        execute_after=now - 3600000,         # timelock cleared 1 h ago
        quorum=4,
        registry_ref=gov_ref,
        registry_version=3,
    )
    utxo2 = UTxO(tx_hash="gov2tx0" + "0" * 57, output_index=0, lovelace=2_000_000, datum_hex="")
    utxo2.datum = gov2

    # Proposal 3: Executed TreasuryTransfer awaiting fund release
    action3 = TreasuryTransferAction(recipient="b2c3d4e5" * 7, lovelace=1_500_000, memo="Infra reimbursement")
    gov3 = GovernanceDatum(
        proposer="a1b2c3d4" * 7,
        description="Infra cost reimbursement — 1.5 ADA",
        action=action3,
        votes=[VoteRecord(voter="a1b2c3d4" * 7, approve=True),
               VoteRecord(voter="b2c3d4e5" * 7, approve=True)],
        status=PROPOSAL_EXECUTED,
        vote_deadline=now - 5 * 86400000,
        execute_after=now - 4 * 86400000,
        quorum=4,
        registry_ref=gov_ref,
        registry_version=3,
    )
    utxo3 = UTxO(tx_hash="gov3tx0" + "0" * 57, output_index=0, lovelace=2_000_000, datum_hex="")
    utxo3.datum = gov3

    proposals = [(utxo1, gov1), (utxo2, gov2), (utxo3, gov3)]

    # ── Treasury ──────────────────────────────────────────
    treas_datum = TreasuryDatum(
        governance_script_hash="f0a1b2c3" * 7,
        max_transfer_lovelace=5_000_000,
    )
    treas_utxo = UTxO(
        tx_hash="treas0tx" + "0" * 56,
        output_index=0,
        lovelace=47_500_000,
        datum_hex="",
    )
    treas_utxo.datum = treas_datum

    return FOSState(
        registry=registry,
        registry_utxo=reg_utxo,
        proposals=proposals,
        treasury_utxo=treas_utxo,
        treasury_datum=treas_datum,
        current_time_ms=now,
    )
