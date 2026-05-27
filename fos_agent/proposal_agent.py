"""
Quorum Proposal Agent — drafts and submits governance proposals from natural language.

The agent reads current on-chain state, validates feasibility, suggests appropriate
quorum and deadlines, shows the user a preview, and submits on confirmation.

Usage:
    from fos_agent.proposal_agent import run_proposal_agent
    run_proposal_agent("Propose a 5 ADA grant to member abc123 for Q2 work")
    run_proposal_agent("Propose rotating the admin key to deadbeef...")
    run_proposal_agent("Propose adopting MIT licence for all repos")
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import anthropic

from . import config
from .chain import BlockfrostClient, read_fos_state, mock_fos_state, FOSState
from .transactions import (
    UnsignedTransaction,
    build_create_proposal_tx,
)
from .types import (
    GovernanceDatum,
    RegistryDatum,
    TreasuryTransferAction,
    RotateAdminAction,
    UpdateRegistryMemberAction,
    OffChainDecisionAction,
    ROLE_ADMIN, ROLE_TREASURER, ROLE_MEMBER, ROLE_OBSERVER,
    ROLE_NAMES,
    STATUS_ACTIVE, STATUS_SUSPENDED, STATUS_REMOVED,
    STATUS_NAMES,
    action_requires_supermajority,
    SUPERMAJORITY_HIGH_VALUE_LOVELACE,
)


# ─── System Prompt ────────────────────────────────────────

PROPOSAL_SYSTEM_PROMPT = """You are the Quorum Proposal Agent — an AI assistant that helps DAO members \
draft and submit governance proposals on Cardano.

## Your job

1. Understand what action the user wants to propose.
2. Call `read_fos_state` to learn current on-chain state (members, treasury cap, open proposals).
3. Validate that the proposed action is feasible.
4. Call `draft_proposal` with all required fields, choosing sensible quorum and deadlines.
5. Show the user the proposal preview. Wait for their confirmation.
6. Only after explicit confirmation, call `submit_proposal`.

## Action types

**TreasuryTransfer** — pay ADA from the treasury to a registered member.
  - Needs: recipient key_hash (must be an Active member), amount_ada, memo.
  - Reject if amount_ada > max_transfer_lovelace / 1_000_000 (treasury cap).
  - Reject if recipient is not an Active member in the registry.
  - Flag: transfers > 10 ADA require a 2/3 supermajority on-chain — recommend higher quorum.

**OffChainDecision** — record a governance decision with no mandatory on-chain execution.
  - Needs: a clear memo describing the decision.
  - Examples: "Adopt MIT licence", "Elect Alice as Q3 treasurer", "Ratify the roadmap".

**UpdateRegistryMember** — change a member's role or status.
  - Needs: target_key_hash, new_role (Admin/Treasurer/Member/Observer), new_status (Active/Suspended/Removed).
  - Check that target_key_hash is a current member.

**RotateAdmin** — transfer admin key to a different member.
  - Needs: new_admin_key_hash (must be an Active member).
  - ALWAYS requires supermajority — flag this prominently to the user.
  - This is irreversible without another RotateAdmin proposal.

## Quorum guidance

Compute from the registry's max_possible_yes_score:
  - OffChainDecision / UpdateRegistryMember: simple majority = floor(max_score / 2) + 1
  - TreasuryTransfer ≤ 10 ADA: simple majority
  - TreasuryTransfer > 10 ADA: supermajority = ceil(max_score * 2 / 3)
  - RotateAdmin: supermajority = ceil(max_score * 2 / 3)

Always explain the quorum to the user in the preview.

## Timeline guidance

- vote_deadline_days: at least 3, default 7 days from now.
- execute_after_days: vote_deadline_days + 1 (minimum 24 h after voting closes).

## Rules

1. Always call read_fos_state first.
2. Validate feasibility before calling draft_proposal (amount ≤ cap, recipient exists, etc.).
3. Show a clear proposal summary after draft_proposal; ask the user to confirm.
4. Never call submit_proposal without explicit user confirmation.
5. If the mandate is unclear, ask the user one targeted question rather than guessing.
"""


# ─── Tools ───────────────────────────────────────────────

PROPOSAL_TOOLS = [
    {
        "name": "read_fos_state",
        "description": (
            "Read the current on-chain state: registry members, roles, treasury balance "
            "and cap, open proposals. Always call this first."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "draft_proposal",
        "description": (
            "Build a proposal draft and return a human-readable preview. "
            "Show this to the user and wait for confirmation before submitting."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action_type": {
                    "type": "string",
                    "enum": ["TreasuryTransfer", "OffChainDecision",
                             "UpdateRegistryMember", "RotateAdmin"],
                    "description": "Type of governance action",
                },
                "description": {
                    "type": "string",
                    "description": "Human-readable proposal description stored on-chain (max ~200 chars)",
                },
                "vote_deadline_days": {
                    "type": "number",
                    "description": "Days from now until voting closes (default 7, min 1)",
                },
                "execute_after_days": {
                    "type": "number",
                    "description": "Days from now until execution is allowed (must be > vote_deadline_days)",
                },
                "quorum": {
                    "type": "integer",
                    "description": "Minimum weighted yes score to pass",
                },
                # TreasuryTransfer
                "recipient_key_hash": {
                    "type": "string",
                    "description": "Recipient member key hash — 56 hex chars (TreasuryTransfer)",
                },
                "amount_ada": {
                    "type": "number",
                    "description": "ADA amount to transfer (TreasuryTransfer)",
                },
                "memo": {
                    "type": "string",
                    "description": "Free-text memo / rationale (TreasuryTransfer or OffChainDecision)",
                },
                # UpdateRegistryMember
                "target_key_hash": {
                    "type": "string",
                    "description": "Target member key hash (UpdateRegistryMember)",
                },
                "new_role": {
                    "type": "string",
                    "enum": ["Admin", "Treasurer", "Member", "Observer"],
                    "description": "New role (UpdateRegistryMember)",
                },
                "new_status": {
                    "type": "string",
                    "enum": ["Active", "Suspended", "Removed"],
                    "description": "New status (UpdateRegistryMember)",
                },
                # RotateAdmin
                "new_admin_key_hash": {
                    "type": "string",
                    "description": "New admin member key hash (RotateAdmin)",
                },
            },
            "required": ["action_type", "description", "quorum",
                         "vote_deadline_days", "execute_after_days"],
        },
    },
    {
        "name": "submit_proposal",
        "description": (
            "Submit the drafted proposal on-chain. "
            "Only call this after the user has explicitly confirmed the draft."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
]


# ─── Agent class ──────────────────────────────────────────

class _ProposalAgent:
    """State machine for a single proposal drafting session."""

    def __init__(self, bf_client: BlockfrostClient):
        self._bf = bf_client
        self._state: Optional[FOSState] = None
        self._draft: Optional[UnsignedTransaction] = None
        self._draft_summary: str = ""

    def dispatch(self, tool_name: str, inputs: dict) -> str:
        try:
            if tool_name == "read_fos_state":
                return self._read_fos_state()
            elif tool_name == "draft_proposal":
                return self._draft_proposal(**inputs)
            elif tool_name == "submit_proposal":
                return self._submit_proposal()
            else:
                return json.dumps({"success": False, "error": f"Unknown tool: {tool_name}"})
        except Exception as e:
            return json.dumps({"success": False, "error": str(e)})

    def _read_fos_state(self) -> str:
        if config.is_configured():
            self._state = read_fos_state(
                self._bf,
                config.REGISTRY_SCRIPT_HASH,
                config.GOVERNANCE_SCRIPT_HASH,
                config.TREASURY_SCRIPT_HASH,
            )
        else:
            self._state = mock_fos_state()

        s = self._state
        reg = s.registry
        max_score = reg.max_possible_yes_score()

        members = [
            {
                "key_hash": m.key_hash,
                "role": ROLE_NAMES.get(m.role, "?"),
                "status": STATUS_NAMES.get(m.status, "?"),
                "vote_weight": m.vote_weight,
                "can_vote": m.can_vote,
            }
            for m in reg.members
        ]

        return json.dumps({
            "success": True,
            "mock_mode": not config.is_configured(),
            "registry": {
                "version": reg.version,
                "admin": reg.admin,
                "member_count": len(reg.members),
                "max_possible_yes_score": max_score,
                "suggested_simple_majority_quorum": max_score // 2 + 1,
                "suggested_supermajority_quorum": math.ceil(max_score * 2 / 3),
                "members": members,
            },
            "treasury": {
                "balance_ada": s.treasury_utxo.lovelace / 1_000_000,
                "max_transfer_ada": s.treasury_datum.max_transfer_lovelace / 1_000_000,
                "max_transfer_lovelace": s.treasury_datum.max_transfer_lovelace,
            },
            "open_proposals": len(s.active_proposals),
        }, indent=2)

    def _draft_proposal(
        self,
        action_type: str,
        description: str,
        vote_deadline_days: float,
        execute_after_days: float,
        quorum: int,
        # TreasuryTransfer
        recipient_key_hash: str = "",
        amount_ada: float = 0.0,
        memo: str = "",
        # UpdateRegistryMember
        target_key_hash: str = "",
        new_role: str = "Member",
        new_status: str = "Active",
        # RotateAdmin
        new_admin_key_hash: str = "",
    ) -> str:
        assert self._state, "Call read_fos_state first"
        s = self._state
        now_ms = s.current_time_ms
        MS_PER_DAY = 86_400_000

        vote_deadline_ms = now_ms + int(vote_deadline_days * MS_PER_DAY)
        execute_after_ms = now_ms + int(execute_after_days * MS_PER_DAY)

        _ROLE_MAP = {"Admin": ROLE_ADMIN, "Treasurer": ROLE_TREASURER,
                     "Member": ROLE_MEMBER, "Observer": ROLE_OBSERVER}
        _STATUS_MAP = {"Active": STATUS_ACTIVE, "Suspended": STATUS_SUSPENDED,
                       "Removed": STATUS_REMOVED}

        # Build typed action
        if action_type == "TreasuryTransfer":
            lovelace = round(amount_ada * 1_000_000)
            cap = s.treasury_datum.max_transfer_lovelace
            if lovelace > cap:
                return json.dumps({
                    "success": False,
                    "error": (
                        f"Transfer {amount_ada:.2f} ADA exceeds the treasury cap "
                        f"of {cap / 1_000_000:.2f} ADA. Reduce the amount."
                    ),
                })
            if not recipient_key_hash or len(recipient_key_hash) < 56:
                return json.dumps({"success": False,
                                   "error": "recipient_key_hash must be 56 hex chars"})
            recipient_member = s.registry.find_member(recipient_key_hash)
            if recipient_member and recipient_member.status != STATUS_ACTIVE:
                return json.dumps({"success": False,
                                   "error": "Recipient is not an Active registry member"})
            action = TreasuryTransferAction(
                recipient=recipient_key_hash,
                lovelace=lovelace,
                memo=memo,
            )

        elif action_type == "OffChainDecision":
            action = OffChainDecisionAction(memo=memo or description)

        elif action_type == "UpdateRegistryMember":
            if not target_key_hash:
                return json.dumps({"success": False,
                                   "error": "target_key_hash is required for UpdateRegistryMember"})
            action = UpdateRegistryMemberAction(
                target_key=target_key_hash,
                new_role=_ROLE_MAP.get(new_role, ROLE_MEMBER),
                new_status=_STATUS_MAP.get(new_status, STATUS_ACTIVE),
            )

        elif action_type == "RotateAdmin":
            if not new_admin_key_hash or len(new_admin_key_hash) < 56:
                return json.dumps({"success": False,
                                   "error": "new_admin_key_hash must be 56 hex chars"})
            action = RotateAdminAction(new_admin=new_admin_key_hash)

        else:
            return json.dumps({"success": False,
                               "error": f"Unknown action_type: {action_type}"})

        # Validation: supermajority warning
        needs_supermajority = action_requires_supermajority(action)
        max_score = s.registry.max_possible_yes_score()
        min_supermajority = math.ceil(max_score * 2 / 3)
        if needs_supermajority and quorum < min_supermajority:
            return json.dumps({
                "success": False,
                "error": (
                    f"This action requires a 2/3 supermajority on-chain. "
                    f"Minimum quorum must be at least {min_supermajority} pts "
                    f"(max_score={max_score}). Current quorum={quorum}."
                ),
            })

        proposer = config.FOS_AGENT_KEY_HASH or ("00" * 28)

        try:
            tx = build_create_proposal_tx(
                registry_utxo=s.registry_utxo,
                registry=s.registry,
                proposer_key_hash=proposer,
                description=description,
                action=action,
                vote_deadline_ms=vote_deadline_ms,
                execute_after_ms=execute_after_ms,
                quorum=quorum,
                governance_script_hash=config.GOVERNANCE_SCRIPT_HASH or ("00" * 28),
                current_time_ms=now_ms,
            )
        except (ValueError, AssertionError) as e:
            return json.dumps({"success": False, "error": str(e)})

        self._draft = tx
        self._draft_summary = tx.summary()

        vote_dl_dt = datetime.fromtimestamp(vote_deadline_ms / 1000, tz=timezone.utc)
        exec_dt = datetime.fromtimestamp(execute_after_ms / 1000, tz=timezone.utc)

        return json.dumps({
            "success": True,
            "preview": {
                "action_type": action_type,
                "description": description,
                "action": str(action),
                "quorum": quorum,
                "max_possible_yes_score": max_score,
                "requires_supermajority": needs_supermajority,
                "vote_deadline": vote_dl_dt.strftime("%d %b %Y %H:%M UTC"),
                "execute_after": exec_dt.strftime("%d %b %Y %H:%M UTC"),
                "proposer": proposer[:12] + "…",
                "transaction_summary": self._draft_summary,
            },
            "instructions": (
                "Review the proposal above. "
                "Reply 'confirmed' or 'submit' to create the on-chain UTxO, "
                "or request changes."
            ),
        }, indent=2)

    def _submit_proposal(self) -> str:
        if not self._draft:
            return json.dumps({"success": False,
                               "error": "No drafted proposal. Call draft_proposal first."})
        if config.AUTONOMOUS_MODE and config.FOS_AGENT_SIGNING_KEY:
            try:
                from .signing import build_signed_transaction, pkh_to_enterprise_address
                import json as _json
                from pathlib import Path as _Path

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
                agent_utxos = self._bf._get(f"addresses/{agent_address}/utxos")
                agent_lovelace = sum(
                    int(a["quantity"])
                    for u in (agent_utxos if isinstance(agent_utxos, list) else [])
                    for a in u.get("amount", [])
                    if a["unit"] == "lovelace"
                )
                change_lovelace = max(0, agent_lovelace - 2_000_000)

                signed_cbor = build_signed_transaction(
                    unsigned_tx=self._draft,
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
                self._draft = None
                return json.dumps({"success": True, "tx_hash": tx_hash,
                                   "message": "Proposal submitted on-chain."})
            except Exception as e:
                return json.dumps({"success": False, "error": str(e)})
        else:
            summary = self._draft_summary
            self._draft = None
            return json.dumps({
                "success": True,
                "message": (
                    "Proposal transaction built. Set FOS_AUTONOMOUS_MODE=true "
                    "and FOS_AGENT_SIGNING_KEY to submit automatically, or copy "
                    "the transaction descriptor below to your wallet."
                ),
                "transaction": summary,
            })


# ─── Entry point ─────────────────────────────────────────

def run_proposal_agent(
    instruction: str,
    max_iterations: int = 12,
    verbose: bool = True,
    model: str = "claude-sonnet-4-6",
) -> dict:
    """
    Run the proposal drafting agent for a natural-language request.

    Args:
        instruction: What the user wants to propose, e.g.
            "Propose a 5 ADA grant to member abc123 for Q2 dev work"
            "Propose rotating the admin key to deadbeef..."
            "Propose adopting MIT licence for all public repos"
        max_iterations: Hard cap on agent turns.
        verbose: Stream progress to stdout.
        model: Claude model to use.

    Returns a dict:
        {
          "success": bool,
          "tx_hash": str | None,    # set if submitted autonomously
          "summary": str,           # agent's final text
          "iterations": int,
          "error": str | None,
        }
    """
    client = anthropic.Anthropic()
    bf_client = BlockfrostClient(
        project_id=config.BLOCKFROST_PROJECT_ID,
        base_url=config.BLOCKFROST_URL,
    )
    agent = _ProposalAgent(bf_client)

    if verbose:
        print(f"\n{'='*60}")
        print(f"📜  Quorum Proposal Agent")
        print(f"💬  {instruction[:80]}")
        print(f"{'='*60}\n")

    messages = [{"role": "user", "content": instruction}]
    iteration = 0
    final_summary = ""
    error_msg: Optional[str] = None

    try:
        while iteration < max_iterations:
            iteration += 1
            if verbose:
                print(f"── Turn {iteration} " + "─" * 40)

            response = client.messages.create(
                model=model,
                max_tokens=4096,
                system=PROPOSAL_SYSTEM_PROMPT,
                tools=PROPOSAL_TOOLS,
                messages=messages,
            )
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                for block in response.content:
                    if hasattr(block, "text"):
                        final_summary = block.text
                        if verbose:
                            print(f"\n✅ Agent: {block.text}")
                break

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        if verbose:
                            print(f"🔧 {block.name}({json.dumps(block.input)[:100]})")
                        result = agent.dispatch(block.name, block.input)
                        result_data = json.loads(result)
                        if verbose:
                            if result_data.get("success"):
                                print(f"   ✓ OK")
                            else:
                                print(f"   ✗ {result_data.get('error', '')[:120]}")
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        })
                    elif block.type == "text" and block.text.strip() and verbose:
                        print(f"\n💬 {block.text.strip()}\n")
                messages.append({"role": "user", "content": tool_results})

    except Exception as e:
        error_msg = str(e)
        if verbose:
            print(f"\n❌ Proposal agent error: {e}")

    if verbose:
        print(f"\n{'='*60}\n")

    return {
        "success": error_msg is None,
        "summary": final_summary,
        "iterations": iteration,
        "error": error_msg,
    }
