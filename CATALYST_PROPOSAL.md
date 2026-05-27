# Project Catalyst Fund 15 — Proposal

---

## Title
Quorum: Open-Source Governance and AI Operator Infrastructure for Cardano DAOs

---

## One-Line Summary
A fully working, open-source governance and treasury system for Cardano — with an AI agent that monitors proposals, casts votes, and executes treasury transfers autonomously.

---

## Problem

Most Cardano DAOs and community funds have no real governance infrastructure. They rely on informal group chats, shared wallets, or a single key holder controlling the treasury. This creates three problems that hold the ecosystem back:

1. **No accountability.** There is no on-chain record of who voted for what, or why a treasury payment was made.
2. **High technical barrier.** Teams that want proper governance have to write their own smart contracts — most don't have the expertise.
3. **No path to automation.** As AI agents become capable of interacting with blockchains, there is no open-source, auditable framework for deploying a governed AI operator on Cardano. Teams build ad-hoc solutions or avoid automation entirely.

The result: treasury funds sit under-governed, decisions are made off-chain with no audit trail, and Cardano DAOs operate with less structure than equivalent communities on other chains.

---

## Solution

Quorum is a complete governance and treasury management system for Cardano, built with Aiken smart contracts and a Claude AI-powered operator agent.

**This project is already fully built, tested, and open source.** This proposal funds deployment to mainnet, community onboarding, and packaging for other teams to adopt.

### What Quorum does

Any Cardano organisation — a DAO, a protocol team, a community fund — can deploy Quorum and immediately have:

- **A verified member registry** on-chain, with roles and weighted voting power
- **A structured proposal system** — any member can raise a proposal; the community votes; the outcome is enforced by the smart contract, not by trust
- **A governed treasury** — ADA is locked in a smart contract and can only be released when a proposal passes, quorum is met, and a timelock delay has cleared
- **A web dashboard** — a live view of the governance state, with wallet integration so members can vote and execute proposals directly from their browser
- **An AI operator agent** — a Claude-powered agent that watches the chain, evaluates proposals against safety rules, casts votes, and executes approved proposals — with a full audit log of every decision

### How the AI agent works

The operator agent runs continuously, monitoring the deployed contracts via Blockfrost. For each open proposal it:

1. Reads the current membership registry and treasury state
2. Checks whether the proposal meets all safety conditions (amount within cap, recipient is an active member, membership hasn't changed since the proposal was created)
3. Votes yes or no based on configurable rules
4. After quorum and a timelock delay, executes the proposal and triggers the treasury payment
5. Logs every decision — approved, rejected, skipped, or flagged as anomalous — to an append-only audit file

The agent never holds a private key. It builds unsigned transaction descriptors that are signed separately by the organisation's wallet. In the default configuration it describes its intent and waits for human confirmation before submitting anything.

### Why this matters for Cardano

Quorum is built specifically for Cardano's eUTxO model. The architecture — where each contract layer reads the layer below it as a read-only reference input — is a pattern unique to Cardano that provides strong security guarantees without complex cross-contract calls.

The AI agent integration is also Cardano-native: it reads on-chain data, constructs valid Cardano transactions, and respects all ledger rules. It is a reference implementation for how autonomous AI agents can operate responsibly on Cardano.

---

## Current State

The project is complete and working today:

- All three Aiken smart contracts written and tested
- AI operator agent built and tested against a mock chain
- Web dashboard running with CIP-30 wallet integration (Eternl, Nami, Lace, VESPR)
- Full signing and transaction submission layer implemented
- Deployed to Cardano preprod testnet
- Open source on GitHub: github.com/jluong2/Quorum_ADA
- MIT licensed

Anyone can run the dashboard locally in mock mode today — no wallet or blockchain connection required — and see the full governance workflow in action.

---

## Impact

**Who benefits directly:**
- Cardano DAOs that need governance infrastructure without writing their own contracts
- Protocol teams that want treasury controls with a verifiable audit trail
- Developer teams building AI agent applications on Cardano
- The Aiken ecosystem — Quorum is a working reference implementation of multi-validator architecture with reference inputs

**What success looks like:**
- At least 3 Cardano community organisations running Quorum governance on mainnet by end of project
- A publicly documented deployment that any team can replicate in under a day
- An open-source AI operator that other Cardano projects can adopt and configure for their own governance needs

---

## Milestones

### Milestone 1 — Mainnet deployment (Month 1–2)
Deploy Quorum to Cardano mainnet with a public, verifiable script address. Commission an independent review of the smart contract security. Publish all script hashes, compiled Plutus scripts, and the review report publicly.

**Deliverable:** Live mainnet deployment + published security review

### Milestone 2 — Documentation (Month 2–3)
Write a complete deployment guide so any team can go from source code to a live governance system. Produce a video walkthrough of the full governance workflow — from creating a proposal to executing a treasury payment. Publish a dedicated documentation site.

**Deliverable:** Published docs site + video walkthrough

### Milestone 3 — AI operator packaging (Month 3–4)
Package the AI operator agent as a standalone installable tool with a configuration guide. Add exportable audit reporting so organisations can publish transparent treasury records. Write a guide for teams deploying their own operator.

**Deliverable:** Installable package + audit reporting tool + guide

### Milestone 4 — Community onboarding (Month 4–6)
Work directly with 3 Cardano community organisations to deploy Quorum for their governance needs. Gather structured feedback and ship improvements based on real-world use. Present the project at a Catalyst town hall or Cardano community event.

**Deliverable:** 3 live deployments + public feedback report + community presentation

---

## Budget

| Item | ADA |
|---|---|
| Smart contract security review | 15,000 |
| Mainnet deployment and infrastructure costs | 5,000 |
| Documentation site and video production | 10,000 |
| AI operator packaging and audit tooling | 15,000 |
| Community onboarding (3 organisations, hands-on support) | 10,000 |
| Contingency (10%) | 5,500 |
| **Total requested** | **60,500 ADA** |

All funded work will be delivered as open-source contributions under the MIT license. No part of this project will be closed-source or paywalled.

---

## Team

**Johnny Luong** — sole developer and project lead.

Built the complete Quorum stack independently: three Aiken smart contracts, a Claude AI operator agent, a PyCardano-based transaction builder and signing layer, and a Flask web dashboard with CIP-30 wallet integration. The project represents significant original technical work across smart contract development, AI systems integration, and Cardano transaction construction.

GitHub: github.com/jluong2/Quorum_ADA

---

## Open Source Commitment

Quorum is fully open source under the MIT license and will remain so. All code, documentation, deployment scripts, configuration guides, and audit reports produced under this grant will be publicly available. The goal is for Quorum to become a community resource — not a product.

---

*Proposal prepared for Project Catalyst Fund 15*
*Category: Developer Ecosystem / Open Source Tooling*
