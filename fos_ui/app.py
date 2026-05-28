"""
Quorum Dashboard — Flask web UI for Quorum on-chain governance on Cardano.

Displays live on-chain state (registry, proposals, treasury) and lets a human
operator build unsigned transactions that can be signed with their wallet.

Usage:
    pip install flask
    python3 fos_ui/app.py          # http://localhost:5000

Env vars: same as the FOS agent (BLOCKFROST_PROJECT_ID, FOS_*_SCRIPT_HASH, etc.)
If not set, the UI runs against mock data.
"""

from __future__ import annotations
import json
import os
import sys
import time
from pathlib import Path

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

from flask import Flask, jsonify, render_template, request

from fos_agent import config
from fos_agent.chain import BlockfrostClient, FOSState, mock_fos_state, read_fos_state
from fos_agent.executor import run_executor
from fos_agent.transactions import (
    build_cast_vote_tx,
    build_claim_vesting_tx,
    build_create_proposal_tx,
    build_create_vesting_tx,
    build_execute_proposal_tx,
    build_expire_proposal_tx,
    build_execute_transfer_tx,
    build_set_delegate_tx,
)
from fos_agent.types import (
    CreateVestingAction,
    GovernanceDatum,
    NativeToken,
    RegistryDatum,
    TreasuryDatum,
    TreasuryTransferAction,
    RotateAdminAction,
    UpdateRegistryMemberAction,
    OffChainDecisionAction,
    UTxO,
    VestingDatum,
    VestingTranche,
    ROLE_NAMES,
    STATUS_NAMES,
    PROPOSAL_STATUS_NAMES,
    ROLE_ADMIN, ROLE_TREASURER, ROLE_MEMBER, ROLE_OBSERVER,
    STATUS_ACTIVE, STATUS_SUSPENDED, STATUS_REMOVED,
)

app = Flask(__name__)

_bf_client = BlockfrostClient(
    project_id=config.BLOCKFROST_PROJECT_ID,
    base_url=config.BLOCKFROST_URL,
)


# ─── Formatting helpers ───────────────────────────────────

def _fmt_ms(ms: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime('%d %b %Y %H:%M UTC')


def _time_remaining(deadline_ms: int, current_ms: int) -> str:
    diff = deadline_ms - current_ms
    if diff <= 0:
        return "Deadline passed"
    secs = diff // 1000
    days = secs // 86400
    hours = (secs % 86400) // 3600
    mins = (secs % 3600) // 60
    if days > 0:
        return f"{days}d {hours}h remaining"
    if hours > 0:
        return f"{hours}h {mins}m remaining"
    return f"{mins}m remaining"


def _action_details(action) -> dict:
    if isinstance(action, TreasuryTransferAction):
        return {
            "type": "Treasury Transfer",
            "recipient": action.recipient,
            "recipient_short": action.recipient[:12] + "…" + action.recipient[-8:],
            "amount_ada": f"{action.lovelace / 1_000_000:.2f}",
            "lovelace": action.lovelace,
            "memo": action.memo or "—",
            "tokens": [
                {
                    "policy_id": t.policy_id,
                    "asset_name": t.asset_name_str(),
                    "quantity": t.quantity,
                }
                for t in action.tokens
            ],
        }
    if isinstance(action, RotateAdminAction):
        return {
            "type": "Rotate Admin",
            "new_admin": action.new_admin,
            "new_admin_short": action.new_admin[:12] + "…" + action.new_admin[-8:],
        }
    if isinstance(action, UpdateRegistryMemberAction):
        return {
            "type": "Update Member",
            "target_key": action.target_key,
            "target_key_short": action.target_key[:12] + "…" + action.target_key[-8:],
            "new_role": ROLE_NAMES.get(action.new_role, "?"),
            "new_status": STATUS_NAMES.get(action.new_status, "?"),
        }
    if isinstance(action, OffChainDecisionAction):
        return {
            "type": "Off-Chain Decision",
            "memo": action.memo or "—",
        }
    if isinstance(action, CreateVestingAction):
        return {
            "type": "Create Vesting",
            "recipient": action.recipient,
            "recipient_short": action.recipient[:12] + "…" + action.recipient[-8:],
            "total_ada": f"{action.total_lovelace() / 1_000_000:.2f}",
            "tranche_count": len(action.tranches),
            "tranches": [
                {"release_time_ms": t.release_time, "lovelace": t.lovelace,
                 "ada": f"{t.lovelace / 1_000_000:.2f}", "release_fmt": _fmt_ms(t.release_time)}
                for t in action.tranches
            ],
            "memo": action.memo or "—",
        }
    return {"type": type(action).__name__}


# ─── State helpers ────────────────────────────────────────

def _get_state() -> FOSState:
    if not config.is_configured():
        return mock_fos_state()
    return read_fos_state(
        _bf_client,
        config.REGISTRY_SCRIPT_HASH,
        config.GOVERNANCE_SCRIPT_HASH,
        config.TREASURY_SCRIPT_HASH,
    )


def _state_to_dict(s: FOSState) -> dict:
    reg = s.registry

    members = [
        {
            "key_hash": m.key_hash,
            "key_hash_short": m.key_hash[:8] + "…" + m.key_hash[-8:],
            "role": ROLE_NAMES.get(m.role, "?"),
            "status": STATUS_NAMES.get(m.status, "?"),
            "vote_weight": m.vote_weight,
            "can_vote": m.can_vote,
            "delegate": m.delegate,
            "delegate_short": (m.delegate[:8] + "…" + m.delegate[-8:]) if m.delegate else None,
        }
        for m in reg.members
    ]

    member_map = {m.key_hash: m for m in reg.members}
    proposals = []
    for utxo, gov in s.proposals:
        yes_score = gov.weighted_yes_score(reg)
        pct = int(100 * yes_score / gov.quorum) if gov.quorum else 0
        is_transfer = isinstance(gov.action, TreasuryTransferAction)
        proposals.append({
            "ref": utxo.ref,
            "ref_short": utxo.ref[:20] + "…",
            "description": gov.description,
            "action": str(gov.action),
            "action_type": type(gov.action).__name__,
            "action_details": _action_details(gov.action),
            "status": gov.status_label(),
            "status_class": {
                "Voting": "voting",
                "Executed": "executed",
                "Expired": "expired",
            }.get(gov.status_label(), ""),
            "yes_score": yes_score,
            "quorum": gov.quorum,
            "quorum_pct": min(pct, 100),
            "quorum_met": gov.quorum_met(reg),
            "vote_deadline_ms": gov.vote_deadline,
            "execute_after_ms": gov.execute_after,
            "vote_deadline_fmt": _fmt_ms(gov.vote_deadline),
            "execute_after_fmt": _fmt_ms(gov.execute_after),
            "time_remaining": _time_remaining(gov.vote_deadline, s.current_time_ms),
            "timelock_cleared": s.current_time_ms >= gov.execute_after,
            "deadline_passed": s.current_time_ms > gov.vote_deadline,
            "votes": [
                {
                    "voter": v.voter[:12] + "…",
                    "voter_full": v.voter,
                    "approve": v.approve,
                    "label": "Yes" if v.approve else "No",
                    "role": ROLE_NAMES.get(member_map[v.voter].role, "?") if v.voter in member_map else "?",
                    "weight": member_map[v.voter].vote_weight if v.voter in member_map else 0,
                }
                for v in gov.votes
            ],
            "registry_version_pinned": gov.registry_version,
            "registry_version_current": reg.version,
            "version_ok": gov.registry_version == reg.version,
            "is_transfer": is_transfer,
            "transfer_lovelace": gov.action.lovelace if is_transfer else None,
            "transfer_ada": (
                f"{gov.action.lovelace / 1_000_000:.2f}"
                if is_transfer else None
            ),
            "transfer_tokens": (
                [{"asset_name": t.asset_name_str(), "quantity": t.quantity, "policy_id": t.policy_id}
                 for t in gov.action.tokens]
                if is_transfer else []
            ),
            "deposit": gov.deposit,
            "deposit_ada": f"{gov.deposit / 1_000_000:.2f}" if gov.deposit else None,
            "rationale_url": gov.rationale_url or None,
            "lovelace": utxo.lovelace,
        })

    vesting_schedules = [
        {
            "ref": utxo.ref,
            "ref_short": utxo.ref[:20] + "…",
            "recipient": d.recipient,
            "recipient_short": d.recipient[:12] + "…" + d.recipient[-8:],
            "total_lovelace": utxo.lovelace,
            "total_ada": f"{utxo.lovelace / 1_000_000:.2f}",
            "proposal_ref": str(d.proposal_ref),
            "tranches": [
                {
                    "release_time_ms": t.release_time,
                    "release_fmt": _fmt_ms(t.release_time),
                    "lovelace": t.lovelace,
                    "ada": f"{t.lovelace / 1_000_000:.2f}",
                    "matured": t.release_time <= s.current_time_ms,
                }
                for t in d.tranches
            ],
            "claimable_lovelace": d.claimable_lovelace(s.current_time_ms),
            "claimable_ada": f"{d.claimable_lovelace(s.current_time_ms) / 1_000_000:.2f}",
            "has_claimable": bool(d.matured_tranches(s.current_time_ms)),
        }
        for utxo, d in s.vesting_utxos
    ]

    return {
        "current_time_ms": s.current_time_ms,
        "network": config.NETWORK,
        "configured": config.is_configured(),
        "registry": {
            "version": reg.version,
            "admin": reg.admin[:12] + "…",
            "member_count": len(reg.members),
            "members": members,
            "max_yes_score": reg.max_possible_yes_score(),
        },
        "governance": {
            "total": len(s.proposals),
            "active": len(s.active_proposals),
            "executable_now": len(s.executable_proposals),
            "expirable_now": len(s.expirable_proposals),
            "awaiting_transfer": len(s.executed_proposals),
            "proposals": proposals,
        },
        "treasury": {
            "balance_lovelace": s.treasury_utxo.lovelace,
            "balance_ada": f"{s.treasury_utxo.lovelace / 1_000_000:.2f}",
            "max_transfer_lovelace": s.treasury_datum.max_transfer_lovelace,
            "max_transfer_ada": f"{s.treasury_datum.max_transfer_lovelace / 1_000_000:.2f}",
            "governance_script_hash": s.treasury_datum.governance_script_hash[:16] + "…",
        },
        "vesting": {
            "total": len(s.vesting_utxos),
            "claimable_count": len(s.claimable_vesting),
            "schedules": vesting_schedules,
        },
    }


# ─── CSRF protection ─────────────────────────────────────

@app.before_request
def _csrf_check():
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return
    origin = request.headers.get("Origin", "")
    if not origin:
        return  # same-origin form posts don't send Origin
    host_header = request.headers.get("Host", "")
    # Strip scheme from origin for comparison
    origin_host = origin.split("://", 1)[-1].rstrip("/")
    if origin_host != host_header:
        return jsonify({"ok": False, "error": "CSRF check failed"}), 403


# ─── Routes ───────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/state")
def api_state():
    try:
        s = _get_state()
        return jsonify({"ok": True, "data": _state_to_dict(s)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/propose", methods=["POST"])
def api_propose():
    """Build a create-proposal UnsignedTransaction."""
    body = request.json or {}

    action_type   = body.get("action_type", "")
    description   = body.get("description", "").strip()
    deadline_ms   = int(body.get("deadline_ms", 0))
    exec_after_ms = int(body.get("execute_after_ms", 0))
    quorum        = int(body.get("quorum", 0))
    deposit_ada   = float(body.get("deposit_ada", 2.0))
    deposit_lovelace = max(round(deposit_ada * 1_000_000), 2_000_000)
    rationale_url = body.get("rationale_url", "").strip()
    proposer      = body.get("proposer_key_hash", "") or config.FOS_AGENT_KEY_HASH or "00" * 28

    if not description:
        return jsonify({"ok": False, "error": "description is required"}), 400

    # Build the typed action
    try:
        if action_type == "TreasuryTransfer":
            raw_tokens = body.get("tokens", [])
            tokens = [
                NativeToken(
                    policy_id=t["policy_id"].strip(),
                    asset_name=t["asset_name"].strip(),
                    quantity=int(t["quantity"]),
                )
                for t in raw_tokens
                if t.get("policy_id") and t.get("quantity")
            ]
            action = TreasuryTransferAction(
                recipient=body.get("recipient", "").strip(),
                lovelace=round(float(body.get("amount_ada", 0)) * 1_000_000),
                memo=body.get("memo", "").strip(),
                tokens=tokens,
            )
        elif action_type == "OffChainDecision":
            action = OffChainDecisionAction(memo=body.get("memo", "").strip())
        elif action_type == "UpdateRegistryMember":
            role_map   = {"Admin": ROLE_ADMIN, "Treasurer": ROLE_TREASURER,
                          "Member": ROLE_MEMBER, "Observer": ROLE_OBSERVER}
            status_map = {"Active": STATUS_ACTIVE, "Suspended": STATUS_SUSPENDED,
                          "Removed": STATUS_REMOVED}
            action = UpdateRegistryMemberAction(
                target_key=body.get("target_key", "").strip(),
                new_role=role_map.get(body.get("new_role", "Member"), ROLE_MEMBER),
                new_status=status_map.get(body.get("new_status", "Active"), STATUS_ACTIVE),
            )
        elif action_type == "RotateAdmin":
            action = RotateAdminAction(new_admin=body.get("new_admin", "").strip())
        elif action_type == "CreateVesting":
            raw_tranches = body.get("tranches", [])
            tranches = [
                VestingTranche(
                    release_time=int(t["release_time_ms"]),
                    lovelace=round(float(t.get("ada", 0)) * 1_000_000),
                )
                for t in raw_tranches
                if t.get("release_time_ms") and t.get("ada")
            ]
            if not tranches:
                return jsonify({"ok": False, "error": "CreateVesting requires at least one tranche"}), 400
            action = CreateVestingAction(
                recipient=body.get("recipient", "").strip(),
                tranches=tranches,
                memo=body.get("memo", "").strip(),
            )
        else:
            return jsonify({"ok": False, "error": f"Unknown action_type: {action_type}"}), 400
    except (ValueError, TypeError) as e:
        return jsonify({"ok": False, "error": f"Invalid action fields: {e}"}), 400

    try:
        s = _get_state()

        if isinstance(action, TreasuryTransferAction):
            cap = s.treasury_datum.max_transfer_lovelace
            if action.lovelace > cap:
                return jsonify({"ok": False, "error": (
                    f"Transfer {action.lovelace / 1_000_000:.2f} ADA exceeds "
                    f"the per-proposal cap of {cap / 1_000_000:.2f} ADA"
                )}), 400

        tx = build_create_proposal_tx(
            registry_utxo=s.registry_utxo,
            registry=s.registry,
            proposer_key_hash=proposer,
            description=description,
            action=action,
            vote_deadline_ms=deadline_ms,
            execute_after_ms=exec_after_ms,
            quorum=quorum,
            governance_script_hash=config.GOVERNANCE_SCRIPT_HASH,
            current_time_ms=s.current_time_ms,
            deposit=deposit_lovelace,
            rationale_url=rationale_url,
        )
        return jsonify({
            "ok": True,
            "summary": tx.summary(),
            "inputs": tx.inputs,
            "datum_hex": tx.outputs[0].datum_hex if tx.outputs else None,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/vote", methods=["POST"])
def api_vote():
    """Build a cast-vote UnsignedTransaction. Returns the transaction summary."""
    body = request.json or {}
    proposal_ref = body.get("proposal_ref", "")
    approve = bool(body.get("approve", True))

    try:
        s = _get_state()
        utxo, gov = _find_proposal(s, proposal_ref)
        tx = build_cast_vote_tx(
            governance_utxo=utxo,
            governance_datum=gov,
            registry_utxo=s.registry_utxo,
            voter_key_hash=config.FOS_AGENT_KEY_HASH or "00" * 28,
            voter_address="",
            approve=approve,
            change_address="",
            current_time_ms=s.current_time_ms,
            governance_script_hash=config.GOVERNANCE_SCRIPT_HASH,
        )
        return jsonify({"ok": True, "summary": tx.summary(), "inputs": tx.inputs,
                        "redeemers": [r.data for r in tx.redeemers]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/execute", methods=["POST"])
def api_execute():
    """Build an execute-proposal UnsignedTransaction."""
    body = request.json or {}
    proposal_ref = body.get("proposal_ref", "")

    try:
        s = _get_state()
        utxo, gov = _find_proposal(s, proposal_ref)
        tx = build_execute_proposal_tx(
            governance_utxo=utxo,
            governance_datum=gov,
            registry_utxo=s.registry_utxo,
            change_address="",
            current_time_ms=s.current_time_ms,
            governance_script_hash=config.GOVERNANCE_SCRIPT_HASH,
        )
        return jsonify({"ok": True, "summary": tx.summary(), "inputs": tx.inputs})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/expire", methods=["POST"])
def api_expire():
    """Build an expire-proposal UnsignedTransaction."""
    body = request.json or {}
    proposal_ref = body.get("proposal_ref", "")

    try:
        s = _get_state()
        utxo, gov = _find_proposal(s, proposal_ref)
        tx = build_expire_proposal_tx(
            governance_utxo=utxo,
            governance_datum=gov,
            registry_utxo=s.registry_utxo,
            change_address="",
            current_time_ms=s.current_time_ms,
            governance_script_hash=config.GOVERNANCE_SCRIPT_HASH,
        )
        return jsonify({"ok": True, "summary": tx.summary(), "inputs": tx.inputs})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/delegate", methods=["POST"])
def api_delegate():
    """Build a set-delegate UnsignedTransaction for liquid democracy."""
    body = request.json or {}
    member_key_hash = body.get("member_key_hash", "").strip()
    new_delegate = body.get("new_delegate") or None  # None clears delegation
    if new_delegate:
        new_delegate = new_delegate.strip() or None

    if not member_key_hash:
        return jsonify({"ok": False, "error": "member_key_hash required"}), 400

    try:
        s = _get_state()
        tx = build_set_delegate_tx(
            registry_utxo=s.registry_utxo,
            registry_datum=s.registry,
            member_key_hash=member_key_hash,
            new_delegate=new_delegate,
            registry_script_hash=config.REGISTRY_SCRIPT_HASH,
        )
        return jsonify({"ok": True, "summary": tx.summary(), "inputs": tx.inputs})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/transfer", methods=["POST"])
def api_transfer():
    """Build an execute-treasury-transfer UnsignedTransaction."""
    body = request.json or {}
    governance_ref = body.get("governance_ref", "")

    try:
        s = _get_state()
        utxo, gov = _find_proposal(s, governance_ref)
        tx = build_execute_transfer_tx(
            treasury_utxo=s.treasury_utxo,
            treasury_datum=s.treasury_datum,
            governance_utxo=utxo,
            governance_datum=gov,
            registry_utxo=s.registry_utxo,
            change_address="",
            treasury_script_hash=config.TREASURY_SCRIPT_HASH,
        )
        return jsonify({"ok": True, "summary": tx.summary(), "inputs": tx.inputs})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/vesting/fund", methods=["POST"])
def api_vesting_fund():
    """Build a create-vesting-schedule UnsignedTransaction (funds from treasury)."""
    body = request.json or {}
    governance_ref = body.get("governance_ref", "")
    vesting_script_hash = body.get("vesting_script_hash", "") or config.VESTING_SCRIPT_HASH

    if not vesting_script_hash:
        return jsonify({"ok": False, "error": "vesting_script_hash required"}), 400

    try:
        s = _get_state()
        utxo, gov = _find_proposal(s, governance_ref)
        tx = build_create_vesting_tx(
            treasury_utxo=s.treasury_utxo,
            treasury_datum=s.treasury_datum,
            governance_utxo=utxo,
            governance_datum=gov,
            vesting_script_hash=vesting_script_hash,
            treasury_script_hash=config.TREASURY_SCRIPT_HASH,
        )
        return jsonify({"ok": True, "summary": tx.summary(), "inputs": tx.inputs})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/vesting/claim", methods=["POST"])
def api_vesting_claim():
    """Build a claim-vested UnsignedTransaction for a vesting UTxO."""
    body = request.json or {}
    vesting_ref = body.get("vesting_ref", "")
    recipient_address = body.get("recipient_address", "")

    if not vesting_ref:
        return jsonify({"ok": False, "error": "vesting_ref required"}), 400

    try:
        s = _get_state()
        utxo, vest = _find_vesting(s, vesting_ref)
        tx = build_claim_vesting_tx(
            vesting_utxo=utxo,
            vesting_datum=vest,
            recipient_address=recipient_address or f"addr_test1v{vest.recipient[:20]}",
            current_time_ms=s.current_time_ms,
            vesting_script_hash=config.VESTING_SCRIPT_HASH,
        )
        return jsonify({"ok": True, "summary": tx.summary(), "inputs": tx.inputs})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/executor/run", methods=["POST"])
def api_executor_run():
    """
    Run the executor agent for an executed governance proposal.
    The agent interprets the proposal mandate and carries it out autonomously.
    """
    body = request.json or {}
    proposal_ref = body.get("proposal_ref", "")
    if not proposal_ref:
        return jsonify({"ok": False, "error": "proposal_ref required"}), 400

    try:
        s = _get_state()
        utxo, gov = _find_proposal(s, proposal_ref)

        if gov.status_label() != "Executed":
            return jsonify({
                "ok": False,
                "error": f"Proposal status is '{gov.status_label()}' — must be Executed to run executor",
            }), 400

        result = run_executor(
            proposal_ref=proposal_ref,
            governance_datum=gov,
            fos_state=s,
            verbose=False,
        )
        return jsonify({"ok": True, "result": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/drep")
def api_drep():
    """Return the agent's current CIP-95 DRep status."""
    key_hash = config.FOS_AGENT_KEY_HASH
    if not key_hash:
        return jsonify({
            "ok": True,
            "drep": {
                "registered": False,
                "drep_id": "—",
                "voting_power_ada": "—",
                "delegator_count": 0,
                "is_active": False,
                "anchor_url": "",
                "mock": True,
            },
        })
    try:
        from fos_agent.drep import query_drep_status, drep_id_from_key_hash
        info = query_drep_status(key_hash)
        return jsonify({
            "ok": True,
            "drep": {
                "registered": info.registered,
                "drep_id": info.drep_id,
                "voting_power_lovelace": info.voting_power,
                "voting_power_ada": f"{info.voting_power / 1_000_000:.2f}",
                "delegator_count": info.delegator_count,
                "is_active": info.is_active,
                "anchor_url": info.anchor_url,
                "last_active_epoch": info.last_active_epoch,
            },
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/audit")
def api_audit():
    """Return recent audit log entries."""
    audit_path = Path(os.environ.get("FOS_AUDIT_LOG", ".fos_audit.jsonl"))
    entries = []
    if audit_path.exists():
        for line in audit_path.read_text().splitlines()[-50:]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return jsonify({"ok": True, "entries": list(reversed(entries))})


@app.route("/api/tx/submit", methods=["POST"])
def api_tx_submit():
    """Submit a signed transaction CBOR hex to the chain via Blockfrost."""
    body = request.json or {}
    cbor_hex = body.get("cbor_hex", "")
    if not cbor_hex:
        return jsonify({"ok": False, "error": "cbor_hex required"}), 400
    try:
        tx_hash = _bf_client.submit_tx(cbor_hex)
        return jsonify({"ok": True, "tx_hash": tx_hash})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/wallet/balance")
def api_wallet_balance():
    """Return ADA balance for an address (via Blockfrost or mock)."""
    address = request.args.get("address", "")
    if not address:
        return jsonify({"ok": False, "error": "address required"}), 400
    try:
        if not config.is_configured():
            return jsonify({"ok": True, "lovelace": 15_320_000, "ada": "15.32", "mock": True})
        data = _bf_client._get(f"addresses/{address}")
        lovelace = int(next(
            (a["quantity"] for a in data.get("amount", []) if a["unit"] == "lovelace"), 0
        ))
        return jsonify({"ok": True, "lovelace": lovelace, "ada": f"{lovelace/1_000_000:.2f}"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


# ─── Helpers ─────────────────────────────────────────────

def _find_proposal(s: FOSState, ref: str) -> tuple[UTxO, GovernanceDatum]:
    for utxo, gov in s.proposals:
        if utxo.ref == ref:
            return utxo, gov
    raise ValueError(f"Proposal not found: {ref}")


def _find_vesting(s: FOSState, ref: str) -> tuple[UTxO, VestingDatum]:
    for utxo, vest in s.vesting_utxos:
        if utxo.ref == ref:
            return utxo, vest
    raise ValueError(f"Vesting UTxO not found: {ref}")


# ─── Entry point ──────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    host = os.environ.get("HOST", "127.0.0.1")
    if not config.is_configured():
        print("⚠  Running in mock mode (set BLOCKFROST_PROJECT_ID + script hashes for live chain)")
    print(f"🌐  Quorum Dashboard → http://{host}:{port}")
    app.run(host=host, port=port, debug=debug)
