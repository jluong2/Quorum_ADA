# Update project documentation

Update README.md, PROJECT_OVERVIEW.md, and CLAUDE.md to reflect any features or changes that have been implemented but are not yet documented.

## Step 1 — Identify what's new

Check `git diff main` (or the recent commits since last doc update) to understand what changed:
- New features added
- New files or modules
- New API routes
- New contract redeemers or types
- New safety rules or invariants
- Changed behavior

## Step 2 — Update CLAUDE.md

CLAUDE.md is the technical reference for Claude Code. Update:
- Architecture sections (new modules, new tools, new contract types)
- New invariants or safety properties
- New commands or env vars
- New transaction builders

## Step 3 — Update README.md

README.md is the public-facing overview. Update:
- Feature list / "What makes this different"
- Architecture diagram if structure changed
- Safety rules list if new rules added
- Quick-start instructions if new commands added

## Step 4 — Update PROJECT_OVERVIEW.md

PROJECT_OVERVIEW.md is the detailed technical narrative. Update:
- Smart contract section (new redeemers, new types, new invariants)
- Operator agent section (new tools, new safety rules)
- Transaction builder table (new builders)
- Dashboard routes table (new API endpoints)

## Step 5 — Verify

After updating, confirm:
- No stale references (old function names, removed features)
- New features are accurately described
- Code examples match current implementation

Report what was updated in each file.
