"""
Quorum alerting layer — sends notifications when governance events occur.

Supported channels (configure via env vars):
  DISCORD_WEBHOOK_URL        — Discord incoming webhook
  SLACK_WEBHOOK_URL          — Slack incoming webhook
  FOS_TREASURY_ALERT_ADA     — treasury low-balance alert threshold (default: 10 ADA)

Usage:
    from fos_agent.alerts import AlertManager
    alerts = AlertManager()
    alerts.check(fos_state)   # fires any new alerts; deduplicates on repeat polls
"""

from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timezone

from .chain import FOSState
from .types import TreasuryTransferAction


DISCORD_WEBHOOK_URL: str = os.environ.get("DISCORD_WEBHOOK_URL", "")
SLACK_WEBHOOK_URL: str = os.environ.get("SLACK_WEBHOOK_URL", "")
TREASURY_ALERT_ADA: float = float(os.environ.get("FOS_TREASURY_ALERT_ADA", "10"))

# Any TreasuryTransfer above this threshold is flagged as high-value.
HIGH_VALUE_ADA: float = 10.0


def _post_webhook(url: str, payload: dict) -> bool:
    """POST a JSON payload to a webhook URL. Returns True on HTTP 2xx."""
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "QuorumAlerts/1.0")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status < 300
    except Exception:
        return False


def send_alert(message: str, title: str = "Quorum") -> bool:
    """
    Send an alert to all configured webhook channels.

    Returns True if at least one channel accepted the message.
    Does nothing (returns False) if no channels are configured.
    """
    sent = False
    if DISCORD_WEBHOOK_URL:
        sent |= _post_webhook(DISCORD_WEBHOOK_URL, {
            "username": "Quorum",
            "content": f"**{title}**\n{message}",
        })
    if SLACK_WEBHOOK_URL:
        sent |= _post_webhook(SLACK_WEBHOOK_URL, {
            "text": f"*{title}*\n{message}",
        })
    return sent


class AlertManager:
    """
    Stateful alert deduplicator for the monitor loop.

    Tracks which (proposal_ref, alert_type) pairs have already fired so
    alerts are not re-sent on every poll cycle.  Reset by creating a new
    instance (e.g. process restart).
    """

    def __init__(self):
        self._fired: set[str] = set()

    def _once(self, key: str) -> bool:
        """Return True the first time this key is seen; False on repeats."""
        if key in self._fired:
            return False
        self._fired.add(key)
        return True

    def check(self, state: FOSState) -> list[str]:
        """
        Inspect state and fire any alerts not yet sent this session.

        Alert types fired:
          - treasury_low          : balance below FOS_TREASURY_ALERT_ADA
          - proposal_created      : first time a new proposal UTxO is seen
          - quorum_reached        : proposal just crossed its quorum threshold
          - deadline_24h          : vote deadline ≤ 24 h away
          - high_value_transfer   : active TreasuryTransfer proposal > HIGH_VALUE_ADA
          - at_risk               : < 48 h left, quorum not met, yes < 50% of max possible

        Returns list of alert messages that were dispatched.
        """
        sent: list[str] = []
        now = state.current_time_ms
        treasury_ada = state.treasury_utxo.lovelace / 1_000_000

        # ── Treasury low balance ─────────────────────────────────
        if treasury_ada < TREASURY_ALERT_ADA:
            key = f"treasury:low:{int(treasury_ada)}"
            if self._once(key):
                msg = (
                    f"Treasury balance is **{treasury_ada:.2f} ADA** "
                    f"— below the alert threshold of {TREASURY_ALERT_ADA:.0f} ADA."
                )
                send_alert(msg, "Treasury Low Balance")
                sent.append(msg)

        for utxo, gov in state.proposals:
            ref = utxo.ref

            # ── New proposal detected ────────────────────────────
            key_new = f"{ref}:seen"
            if self._once(key_new) and gov.is_voting:
                action_str = str(gov.action)
                msg = (
                    f"New proposal submitted.\n"
                    f"Ref: `{ref[:20]}…`\n"
                    f"Action: {action_str[:80]}\n"
                    f"Description: {gov.description[:100]}\n"
                    f"Quorum required: {gov.quorum} pts"
                )
                send_alert(msg, "New Proposal")
                sent.append(msg)

            if not gov.is_voting:
                continue

            hours_remaining = (gov.vote_deadline - now) / 3_600_000
            yes = gov.weighted_yes_score(state.registry)

            # ── Quorum reached ───────────────────────────────────
            if gov.quorum_met(state.registry):
                key_q = f"{ref}:quorum_reached"
                if self._once(key_q):
                    msg = (
                        f"Proposal has reached quorum!\n"
                        f"Ref: `{ref[:20]}…`\n"
                        f"Description: {gov.description[:80]}\n"
                        f"Score: {yes}/{gov.quorum} pts"
                    )
                    send_alert(msg, "Quorum Reached")
                    sent.append(msg)

            # ── Deadline within 24 h ─────────────────────────────
            if 0 < hours_remaining <= 24:
                key_d = f"{ref}:deadline_24h"
                if self._once(key_d):
                    quorum_status = "QUORUM MET" if gov.quorum_met(state.registry) else "quorum NOT met"
                    msg = (
                        f"Proposal deadline in **{hours_remaining:.1f}h** — {quorum_status}.\n"
                        f"Ref: `{ref[:20]}…`\n"
                        f"Description: {gov.description[:80]}\n"
                        f"Score: {yes}/{gov.quorum} pts"
                    )
                    send_alert(msg, "Proposal Deadline Approaching")
                    sent.append(msg)

            # ── High-value transfer ───────────────────────────────
            if isinstance(gov.action, TreasuryTransferAction):
                amount_ada = gov.action.lovelace / 1_000_000
                if amount_ada > HIGH_VALUE_ADA:
                    key_hv = f"{ref}:high_value"
                    if self._once(key_hv):
                        msg = (
                            f"High-value treasury transfer proposal active.\n"
                            f"Ref: `{ref[:20]}…`\n"
                            f"Amount: **{amount_ada:.2f} ADA** (requires 2/3 supermajority)\n"
                            f"Recipient: `{gov.action.recipient[:16]}…`\n"
                            f"Memo: {gov.action.memo or '(none)'}"
                        )
                        send_alert(msg, "High-Value Transfer Proposal")
                        sent.append(msg)

            # ── At-risk: low participation with deadline approaching ──
            if not gov.quorum_met(state.registry) and 0 < hours_remaining <= 48:
                max_score = state.registry.max_possible_yes_score()
                if max_score > 0 and yes / max_score < 0.5:
                    key_ar = f"{ref}:at_risk"
                    if self._once(key_ar):
                        pct = int(yes / max_score * 100)
                        msg = (
                            f"Proposal is at risk of failing — low participation.\n"
                            f"Ref: `{ref[:20]}…`\n"
                            f"Description: {gov.description[:80]}\n"
                            f"Deadline in: **{hours_remaining:.1f}h**\n"
                            f"Participation: {yes}/{max_score} pts ({pct}% of max) — "
                            f"quorum requires {gov.quorum} pts"
                        )
                        send_alert(msg, "Proposal At Risk")
                        sent.append(msg)

        return sent
