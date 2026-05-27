"""
Quorum Operator Agent — Claude-powered autonomous operator of the Quorum
on-chain governance protocol on Cardano.

Responsibilities:
  - Monitor all three on-chain validators (identity, governance, treasury)
  - Vote on governance proposals according to decision rules
  - Execute proposals that have passed quorum + timelock
  - Release treasury funds for executed TreasuryTransfer proposals
  - Alert on anomalies: expiring proposals, treasury below threshold, etc.
  - Log every decision with its reasoning for audit purposes

Usage:
    from fos_agent import run_fos_agent
    run_fos_agent("Check Quorum state and take any pending actions")
    run_fos_agent("Vote yes on all active treasury proposals under 10 ADA")
"""

from __future__ import annotations
import json
import os
from datetime import datetime
from pathlib import Path

import anthropic

from . import config
from .chain import BlockfrostClient, FOSState, read_fos_state
from .transactions import (
    UnsignedTransaction,
    build_cast_vote_tx,
    build_execute_proposal_tx,
    build_expire_proposal_tx,
    build_execute_transfer_tx,
)
from .types import (
    GovernanceDatum,
    RegistryDatum,
    TreasuryDatum,
    TreasuryTransferAction,
    UTxO,
)


# ─── System Prompt ────────────────────────────────────────

FOS_SYSTEM_PROMPT = """You are an autonomous operator of Quorum, an on-chain governance protocol on Cardano.
You manage three on-chain validators deployed on Cardano:

  1. Identity Registry  — who belongs to the organisation (members, roles, status)
  2. Governance         — weighted voting on proposals with quorum + timelock
  3. Treasury           — ADA held in escrow, released only by Executed proposals

Your on-chain identity is a registered Quorum member. Every action you take
is recorded on the Cardano blockchain and is publicly auditable.

## Vote Weights
  Admin      → 3 points   Treasurer → 2 points
  Member     → 1 point    Observer  → 0 (cannot vote)

## Decision Rules — Voting

Vote YES only if ALL of the following are true:
  - Proposal action is TreasuryTransfer AND amount ≤ max_transfer_lovelace
  - Recipient is an Active member in the identity registry
  - No registry version mismatch (registry.version == proposal.registry_version)
  - You have not already voted on this proposal

Vote YES on OffChainDecision and RotateAdmin proposals only after explicitly
reviewing them and logging your reasoning.

Vote NO (or abstain) if any condition fails — do not silently skip.

## Decision Rules — Execution

Execute a proposal if ALL of the following are true:
  - status == Voting
  - current_time_ms >= execute_after  (timelock cleared)
  - weighted_yes_score >= quorum
  - registry.version == proposal.registry_version (registry unchanged)

## Decision Rules — Treasury Transfer

Call execute_treasury_transfer only if:
  - governance proposal status == Executed (already flipped by execute_proposal)
  - proposal.action == TreasuryTransfer
  - lovelace ≤ treasury.max_transfer_lovelace
  - governance_script_hash matches treasury.governance_script_hash

## Error Handling

- If registry version mismatches a proposal, log it and DO NOT vote or execute.
  The proposer must recreate the proposal against the current registry.
- If quorum is mathematically unreachable (active voters' max score < quorum),
  call expire_proposal immediately — do not wait for the deadline. The state
  report flags these under governance.unreachable_quorum so you never miss them.
- If the same error repeats after 2 tool calls, surface it to the user.

## Audit Log

Use write_audit_log for every decision — approved vote, rejected vote, execution,
expiry, or anomaly detection. Include: proposal ref, action, reasoning, timestamp.

## Confirmation

Unless running in autonomous mode, describe what you intend to do and wait
for the user to confirm before calling any transaction-building tool."""


# ─── Tools ───────────────────────────────────────────────

TOOLS = [
    {
        "name": "read_fos_state",
        "description": (
            "Read the current on-chain state of all three FOS validators: "
            "identity registry (members + roles), all governance proposals "
            "(status, votes, quorum), and treasury (balance, spending cap). "
            "Call this first before any other action."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "cast_vote",
        "description": "Cast a yes or no vote on a governance proposal.",
        "input_schema": {
            "type": "object",
            "properties": {
                "proposal_ref": {
                    "type": "string",
                    "description": "UTxO reference of the proposal, e.g. 'abc123#0'",
                },
                "approve": {
                    "type": "boolean",
                    "description": "True to vote yes, False to vote no",
                },
                "reasoning": {
                    "type": "string",
                    "description": "Why you are casting this vote (for audit log)",
                },
            },
            "required": ["proposal_ref", "approve", "reasoning"],
        },
    },
    {
        "name": "execute_proposal",
        "description": (
            "Execute a governance proposal that has reached quorum and cleared "
            "the timelock. Flips status Voting → Executed. "
            "For TreasuryTransfer proposals, call execute_treasury_transfer "
            "AFTER this transaction is confirmed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "proposal_ref": {"type": "string"},
                "reasoning": {"type": "string"},
            },
            "required": ["proposal_ref", "reasoning"],
        },
    },
    {
        "name": "expire_proposal",
        "description": "Close a proposal that missed quorum after the vote deadline.",
        "input_schema": {
            "type": "object",
            "properties": {
                "proposal_ref": {"type": "string"},
                "reasoning": {"type": "string"},
            },
            "required": ["proposal_ref", "reasoning"],
        },
    },
    {
        "name": "execute_treasury_transfer",
        "description": (
            "Release treasury funds for an Executed TreasuryTransfer proposal. "
            "The governance UTxO is supplied as a reference input — it is NOT spent. "
            "Must only be called after execute_proposal has been confirmed on-chain."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "governance_ref": {
                    "type": "string",
                    "description": "UTxO ref of the Executed governance proposal",
                },
                "reasoning": {"type": "string"},
            },
            "required": ["governance_ref", "reasoning"],
        },
    },
    {
        "name": "write_audit_log",
        "description": "Persist a structured audit entry for every FOS decision.",
        "input_schema": {
            "type": "object",
            "properties": {
                "event": {
                    "type": "string",
                    "enum": [
                        "vote_yes", "vote_no", "execute_proposal",
                        "expire_proposal", "treasury_transfer",
                        "anomaly", "skipped",
                    ],
                },
                "proposal_ref": {"type": "string"},
                "details": {"type": "string"},
            },
            "required": ["event", "details"],
        },
    },
]


# ─── Tool dispatcher ──────────────────────────────────────

class FOSAgent:
    """Stateful agent that holds the chain state between tool calls."""

    def __init__(self, client: anthropic.Anthropic, bf_client: BlockfrostClient):
        self._client = client
        self._bf = bf_client
        self._state: FOSState | None = None
        self._audit_path = Path(
            os.environ.get("FOS_AUDIT_LOG", ".fos_audit.jsonl")
        )

    def dispatch(self, tool_name: str, inputs: dict) -> str:
        try:
            if tool_name == "read_fos_state":
                return self._read_fos_state()
            elif tool_name == "cast_vote":
                return self._cast_vote(**inputs)
            elif tool_name == "execute_proposal":
                return self._execute_proposal(**inputs)
            elif tool_name == "expire_proposal":
                return self._expire_proposal(**inputs)
            elif tool_name == "execute_treasury_transfer":
                return self._execute_treasury_transfer(**inputs)
            elif tool_name == "write_audit_log":
                return self._write_audit_log(**inputs)
            else:
                return json.dumps({"success": False, "error": f"Unknown tool: {tool_name}"})
        except Exception as e:
            return json.dumps({"success": False, "error": str(e)})

    # ── Tool implementations ──────────────────────────────

    def _read_fos_state(self) -> str:
        self._state = read_fos_state(
            self._bf,
            config.REGISTRY_SCRIPT_HASH,
            config.GOVERNANCE_SCRIPT_HASH,
            config.TREASURY_SCRIPT_HASH,
        )
        s = self._state
        registry = s.registry

        member_rows = [
            {
                "key_hash": m.key_hash,
                "role": ["Admin", "Treasurer", "Member", "Observer"][m.role],
                "status": ["Active", "Suspended", "Removed"][m.status],
                "vote_weight": m.vote_weight,
            }
            for m in registry.members
        ]

        proposal_rows = []
        for utxo, gov in s.proposals:
            yes_score = gov.weighted_yes_score(registry)
            proposal_rows.append({
                "ref": utxo.ref,
                "status": gov.status_label(),
                "action": str(gov.action),
                "description": gov.description,
                "yes_score": yes_score,
                "quorum": gov.quorum,
                "quorum_met": yes_score >= gov.quorum,
                "vote_deadline_ms": gov.vote_deadline,
                "execute_after_ms": gov.execute_after,
                "votes_cast": len(gov.votes),
                "registry_version_pinned": gov.registry_version,
                "registry_version_current": registry.version,
                "version_match": gov.registry_version == registry.version,
                "agent_voted": gov.already_voted(config.FOS_AGENT_KEY_HASH),
            })

        return json.dumps({
            "success": True,
            "current_time_ms": s.current_time_ms,
            "registry": {
                "version": registry.version,
                "member_count": len(registry.members),
                "members": member_rows,
                "max_possible_yes_score": registry.max_possible_yes_score(),
            },
            "governance": {
                "total_proposals": len(s.proposals),
                "active": len(s.active_proposals),
                "executable_now": len(s.executable_proposals),
                "expirable_now": len(s.expirable_proposals),
                "unreachable_quorum": [u.ref for u, _ in s.unreachable_quorum_proposals],
                "executed_awaiting_transfer": len(s.executed_proposals),
                "proposals": proposal_rows,
            },
            "treasury": {
                "balance_lovelace": s.treasury_utxo.lovelace,
                "balance_ada": s.treasury_utxo.lovelace / 1_000_000,
                "max_transfer_lovelace": s.treasury_datum.max_transfer_lovelace,
                "governance_script_hash": s.treasury_datum.governance_script_hash,
            },
        }, indent=2)

    def _cast_vote(self, proposal_ref: str, approve: bool, reasoning: str) -> str:
        assert self._state, "Call read_fos_state first"
        utxo, gov = self._find_proposal(proposal_ref)
        tx = build_cast_vote_tx(
            governance_utxo=utxo,
            governance_datum=gov,
            registry_utxo=self._state.registry_utxo,
            voter_key_hash=config.FOS_AGENT_KEY_HASH,
            voter_address="",
            approve=approve,
            change_address="",
            current_time_ms=self._state.current_time_ms,
            governance_script_hash=config.GOVERNANCE_SCRIPT_HASH,
        )
        result = {"success": True, "transaction": tx.summary()}
        if config.AUTONOMOUS_MODE:
            result["submission"] = self._sign_and_submit(tx)
        return json.dumps(result)

    def _execute_proposal(self, proposal_ref: str, reasoning: str) -> str:
        assert self._state, "Call read_fos_state first"
        utxo, gov = self._find_proposal(proposal_ref)
        tx = build_execute_proposal_tx(
            governance_utxo=utxo,
            governance_datum=gov,
            registry_utxo=self._state.registry_utxo,
            change_address="",
            current_time_ms=self._state.current_time_ms,
            governance_script_hash=config.GOVERNANCE_SCRIPT_HASH,
        )
        result = {"success": True, "transaction": tx.summary()}
        if config.AUTONOMOUS_MODE:
            result["submission"] = self._sign_and_submit(tx)
        return json.dumps(result)

    def _expire_proposal(self, proposal_ref: str, reasoning: str) -> str:
        assert self._state, "Call read_fos_state first"
        utxo, gov = self._find_proposal(proposal_ref)
        tx = build_expire_proposal_tx(
            governance_utxo=utxo,
            governance_datum=gov,
            registry_utxo=self._state.registry_utxo,
            change_address="",
            current_time_ms=self._state.current_time_ms,
            governance_script_hash=config.GOVERNANCE_SCRIPT_HASH,
        )
        result = {"success": True, "transaction": tx.summary()}
        if config.AUTONOMOUS_MODE:
            result["submission"] = self._sign_and_submit(tx)
        return json.dumps(result)

    def _execute_treasury_transfer(self, governance_ref: str, reasoning: str) -> str:
        assert self._state, "Call read_fos_state first"
        utxo, gov = self._find_proposal(governance_ref)
        tx = build_execute_transfer_tx(
            treasury_utxo=self._state.treasury_utxo,
            treasury_datum=self._state.treasury_datum,
            governance_utxo=utxo,
            governance_datum=gov,
            registry_utxo=self._state.registry_utxo,
            change_address="",
            treasury_script_hash=config.TREASURY_SCRIPT_HASH,
        )
        result = {"success": True, "transaction": tx.summary()}
        if config.AUTONOMOUS_MODE:
            result["submission"] = self._sign_and_submit(tx)
        return json.dumps(result)

    def _write_audit_log(self, event: str, details: str, proposal_ref: str = "") -> str:
        entry = {
            "ts": datetime.utcnow().isoformat(),
            "event": event,
            "proposal_ref": proposal_ref,
            "agent": config.FOS_AGENT_KEY_HASH[:12] or "unset",
            "details": details,
        }
        with open(self._audit_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
        return json.dumps({"success": True, "logged": entry})

    # ── Helpers ───────────────────────────────────────────

    def _find_proposal(self, ref: str) -> tuple[UTxO, GovernanceDatum]:
        for utxo, gov in self._state.proposals:
            if utxo.ref == ref:
                return utxo, gov
        raise ValueError(f"Proposal not found: {ref}")

    def _sign_and_submit(self, unsigned_tx: UnsignedTransaction) -> dict:
        """
        Sign an UnsignedTransaction and submit it to the chain.

        Only called when AUTONOMOUS_MODE=True and all signing prerequisites
        are configured (signing key, collateral ref, agent address).

        Returns a dict with tx_hash on success, or error on failure.
        """
        if not config.FOS_AGENT_SIGNING_KEY:
            return {"submitted": False, "reason": "FOS_AGENT_SIGNING_KEY not set"}
        if not config.FOS_COLLATERAL_REF:
            return {"submitted": False, "reason": "FOS_COLLATERAL_REF not set"}

        try:
            from .signing import (
                build_signed_transaction,
                pkh_to_enterprise_address,
                script_hash_to_address,
            )
            import json as _json, os as _os
            from pathlib import Path as _Path

            # Load plutus scripts from plutus.json
            plutus_path = _Path(__file__).parent.parent / "identity_registry" / "plutus.json"
            plutus_scripts_hex = []
            if plutus_path.exists():
                data = _json.loads(plutus_path.read_text())
                plutus_scripts_hex = [
                    v["compiledCode"] for v in data.get("validators", [])
                    if "mock" not in v.get("compiledCode", "")
                ]

            agent_address = pkh_to_enterprise_address(
                config.FOS_AGENT_KEY_HASH, config.NETWORK
            )

            # Fetch agent wallet UTxOs to calculate change
            agent_utxos = self._bf._get(f"addresses/{agent_address}/utxos")
            agent_lovelace = sum(
                int(a["quantity"])
                for u in (agent_utxos if isinstance(agent_utxos, list) else [])
                for a in u.get("amount", [])
                if a["unit"] == "lovelace"
            )
            # Rough change = wallet balance - fee estimate
            change_lovelace = max(0, agent_lovelace - 600_000)

            signed_cbor = build_signed_transaction(
                unsigned_tx=unsigned_tx,
                signing_key_hex=config.FOS_AGENT_SIGNING_KEY,
                plutus_scripts_hex=plutus_scripts_hex,
                collateral_ref=config.FOS_COLLATERAL_REF,
                change_address=agent_address,
                change_lovelace=change_lovelace,
                blockfrost_url=config.BLOCKFROST_URL,
                project_id=config.BLOCKFROST_PROJECT_ID,
                network=config.NETWORK,
            )

            tx_hash = self._bf.submit_tx(signed_cbor)
            return {"submitted": True, "tx_hash": tx_hash}

        except Exception as e:
            return {"submitted": False, "error": str(e)}


# ─── Agent Loop ───────────────────────────────────────────

def run_fos_agent(
    instruction: str,
    max_iterations: int = 15,
    verbose: bool = True,
) -> list[dict]:
    """
    Run the FOS operator agent for a single task.

    The agent reads on-chain state, reasons about what to do, and builds
    unsigned transactions — but never submits without confirmation (unless
    config.AUTONOMOUS_MODE is True).

    Args:
        instruction: Natural language task, e.g.
            "Check FOS state and take any pending actions"
            "Vote yes on proposals under 5 ADA from registered members"
            "Execute all proposals that have passed quorum and timelock"
        max_iterations: Hard cap on agent turns.
        verbose: Print progress to stdout.
    """
    anthropic_client = anthropic.Anthropic()

    bf_client = BlockfrostClient(
        project_id=config.BLOCKFROST_PROJECT_ID,
        base_url=config.BLOCKFROST_URL,
    )
    agent = FOSAgent(anthropic_client, bf_client)

    if not config.is_configured() and verbose:
        print("⚠️  FOS not fully configured — running in mock mode.")
        print("   Set FOS_*_SCRIPT_HASH and BLOCKFROST_PROJECT_ID for live chain.")

    messages = [{"role": "user", "content": instruction}]
    iteration = 0

    if verbose:
        print(f"\n{'='*60}")
        print(f"🏛  FOS Operator Agent")
        print(f"📡 Network: {config.NETWORK}")
        print(f"🔒 Autonomous: {config.AUTONOMOUS_MODE}")
        print(f"{'='*60}\n")

    while iteration < max_iterations:
        iteration += 1
        if verbose:
            print(f"── Turn {iteration} " + "─" * 40)

        response = anthropic_client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=4096,
            system=FOS_SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text") and verbose:
                    print(f"\n✅ Agent: {block.text}")
            break

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    if verbose:
                        print(f"🔧 {block.name}({json.dumps(block.input)[:80]})")
                    result = agent.dispatch(block.name, block.input)
                    result_dict = json.loads(result)
                    if verbose:
                        if result_dict.get("success"):
                            print(f"   ✓ OK")
                        else:
                            print(f"   ✗ {result_dict.get('error', '')[:100]}")
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
                elif block.type == "text" and block.text.strip() and verbose:
                    print(f"\n💬 {block.text.strip()}\n")
            messages.append({"role": "user", "content": tool_results})

    if iteration >= max_iterations and verbose:
        print(f"\n⚠️  Max iterations ({max_iterations}) reached.")

    if verbose:
        print(f"\n{'='*60}\n")

    return messages


# ─── Monitor loop ─────────────────────────────────────────

def run_monitor(poll_interval_seconds: int = 300):
    """
    Long-running monitor: checks FOS state on a schedule and acts on
    pending proposals.  Runs until interrupted.

    Each cycle calls run_fos_agent("Check FOS state and take any pending actions").
    With AUTONOMOUS_MODE=true the agent will submit transactions automatically.
    With AUTONOMOUS_MODE=false it will describe what it would do and wait.
    """
    import time
    print(f"🏛  FOS Monitor — polling every {poll_interval_seconds}s")
    print(f"   Press Ctrl+C to stop.\n")
    while True:
        try:
            run_fos_agent("Check FOS state and take any pending actions.")
        except KeyboardInterrupt:
            print("\nMonitor stopped.")
            break
        except Exception as e:
            print(f"⚠️  Cycle error: {e}")
        time.sleep(poll_interval_seconds)
