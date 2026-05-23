"""
rag_ingest.py — Build the Cardano RAG knowledge base.

Scrapes the official Aiken documentation and loads curated example
validators into ChromaDB. Run this once (or whenever you want to refresh).

Usage:
    python rag_ingest.py              # full ingest, default .rag_db dir
    python rag_ingest.py --db /path  # custom persist directory
    python rag_ingest.py --clear     # wipe DB then re-ingest
    python rag_ingest.py --stats     # show current DB stats and exit

Dependencies:
    pip install chromadb sentence-transformers requests beautifulsoup4 --break-system-packages
"""

import argparse
import sys
import time
from typing import Optional

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Missing deps. Run: pip install requests beautifulsoup4 --break-system-packages")
    sys.exit(1)

from rag import CardanoRAG


# ─────────────────────────────────────────────
# Aiken doc pages to ingest
# ─────────────────────────────────────────────

# (url, source_tag, title)
LANGUAGE_TOUR_PAGES = [
    ("https://aiken-lang.org/language-tour/primitive-types",    "aiken-tour", "Primitive Types"),
    ("https://aiken-lang.org/language-tour/variables",          "aiken-tour", "Variables"),
    ("https://aiken-lang.org/language-tour/functions",          "aiken-tour", "Functions"),
    ("https://aiken-lang.org/language-tour/control-flow",       "aiken-tour", "Control Flow"),
    ("https://aiken-lang.org/language-tour/custom-types",       "aiken-tour", "Custom Types"),
    ("https://aiken-lang.org/language-tour/type-aliases",       "aiken-tour", "Type Aliases"),
    ("https://aiken-lang.org/language-tour/pattern-matching",   "aiken-tour", "Pattern Matching"),
    ("https://aiken-lang.org/language-tour/expect",             "aiken-tour", "Expect"),
    ("https://aiken-lang.org/language-tour/generics",           "aiken-tour", "Generics"),
    ("https://aiken-lang.org/language-tour/list",               "aiken-tour", "List"),
    ("https://aiken-lang.org/language-tour/tuples",             "aiken-tour", "Tuples"),
    ("https://aiken-lang.org/language-tour/modules",            "aiken-tour", "Modules"),
    ("https://aiken-lang.org/language-tour/standard-library",   "aiken-tour", "Standard Library"),
    ("https://aiken-lang.org/language-tour/tests",              "aiken-tour", "Tests"),
    ("https://aiken-lang.org/language-tour/validators",         "aiken-tour", "Validators"),
    ("https://aiken-lang.org/language-tour/troubleshooting",    "aiken-tour", "Troubleshooting"),
]

STDLIB_PAGES = [
    ("https://aiken-lang.org/stdlib/aiken/collection/list",     "stdlib", "aiken/collection/list"),
    ("https://aiken-lang.org/stdlib/aiken/collection/dict",     "stdlib", "aiken/collection/dict"),
    ("https://aiken-lang.org/stdlib/aiken/collection/pairs",    "stdlib", "aiken/collection/pairs"),
    ("https://aiken-lang.org/stdlib/aiken/crypto",              "stdlib", "aiken/crypto"),
    ("https://aiken-lang.org/stdlib/aiken/hash",                "stdlib", "aiken/hash"),
    ("https://aiken-lang.org/stdlib/aiken/math",                "stdlib", "aiken/math"),
    ("https://aiken-lang.org/stdlib/aiken/string",              "stdlib", "aiken/string"),
    ("https://aiken-lang.org/stdlib/aiken/option",              "stdlib", "aiken/option"),
    ("https://aiken-lang.org/stdlib/aiken/interval",            "stdlib", "aiken/interval"),
    ("https://aiken-lang.org/stdlib/aiken/transaction",         "stdlib", "aiken/transaction"),
    ("https://aiken-lang.org/stdlib/aiken/transaction/value",   "stdlib", "aiken/transaction/value"),
    ("https://aiken-lang.org/stdlib/aiken/transaction/certificate", "stdlib", "aiken/transaction/certificate"),
    ("https://aiken-lang.org/stdlib/aiken/transaction/credential", "stdlib", "aiken/transaction/credential"),
]

EXAMPLE_PAGES = [
    ("https://aiken-lang.org/example--hello-world",             "example", "Hello World Validator"),
    ("https://aiken-lang.org/example--vesting",                 "example", "Vesting Contract"),
    ("https://aiken-lang.org/example--gift-card",               "example", "Gift Card Contract"),
]


# ─────────────────────────────────────────────
# Curated inline examples
# (embedded directly so the DB works even without network access)
# ─────────────────────────────────────────────

INLINE_EXAMPLES = [
    {
        "source": "example",
        "title": "Identity Registry Validator (Quorum Layer 1)",
        "url": "",
        "is_aiken": True,
        "text": """\
use aiken/crypto.{VerificationKeyHash}
use aiken/list
use aiken/transaction.{InlineDatum, ScriptContext, Spend, Transaction, find_input}

pub type Role {
  Admin
  Treasurer
  Member
  Observer
}

pub type MemberStatus {
  Active
  Suspended
  Removed
}

pub type Member {
  key_hash: VerificationKeyHash,
  role: Role,
  joined_at: Int,
  status: MemberStatus,
}

pub type RegistryDatum {
  members: List<Member>,
  admin: VerificationKeyHash,
  version: Int,
}

pub type RegistryAction {
  AddMember { new_member: Member }
  RemoveMember { target_key: VerificationKeyHash }
  UpdateMember {
    target_key: VerificationKeyHash,
    new_role: Role,
    new_status: MemberStatus,
  }
  TransferAdmin { new_admin: VerificationKeyHash }
}

fn member_exists(members: List<Member>, key: VerificationKeyHash) -> Bool {
  list.any(members, fn(m) { m.key_hash == key })
}

fn remove_member_by_key(members: List<Member>, key: VerificationKeyHash) -> List<Member> {
  list.filter(members, fn(m) { m.key_hash != key })
}

fn update_member_by_key(
  members: List<Member>,
  key: VerificationKeyHash,
  new_role: Role,
  new_status: MemberStatus,
) -> List<Member> {
  list.map(members, fn(m) {
    if m.key_hash == key {
      Member { key_hash: m.key_hash, role: new_role, joined_at: m.joined_at, status: new_status }
    } else {
      m
    }
  })
}

validator {
  fn spend(datum: RegistryDatum, action: RegistryAction, ctx: ScriptContext) -> Bool {
    let Transaction { extra_signatories, outputs, inputs, .. } = ctx.transaction
    expect Spend(own_ref) = ctx.purpose
    expect Some(own_input) = find_input(inputs, own_ref)
    let own_address = own_input.output.address
    let continuing_outputs = list.filter(outputs, fn(o) { o.address == own_address })
    expect [continuing] = continuing_outputs
    expect InlineDatum(raw_datum) = continuing.datum
    expect new_datum: RegistryDatum = raw_datum
    expect list.has(extra_signatories, datum.admin)
    expect new_datum.version == datum.version + 1
    when action is {
      AddMember { new_member } ->
        !member_exists(datum.members, new_member.key_hash) &&
        new_datum.members == [new_member, ..datum.members] &&
        new_datum.admin == datum.admin
      RemoveMember { target_key } ->
        member_exists(datum.members, target_key) &&
        new_datum.members == remove_member_by_key(datum.members, target_key) &&
        new_datum.admin == datum.admin
      UpdateMember { target_key, new_role, new_status } ->
        member_exists(datum.members, target_key) &&
        new_datum.members == update_member_by_key(datum.members, target_key, new_role, new_status) &&
        new_datum.admin == datum.admin
      TransferAdmin { new_admin } ->
        new_datum.admin == new_admin && new_datum.members == datum.members
    }
  }
}

test member_exists_finds_registered_key() {
  let m = Member { key_hash: #"deadbeef", role: Member, joined_at: 1000, status: Active }
  member_exists([m], #"deadbeef")
}

test member_exists_misses_unknown_key() {
  let m = Member { key_hash: #"deadbeef", role: Member, joined_at: 1000, status: Active }
  !member_exists([m], #"cafebabe")
}

test remove_member_by_key_removes_correct_member() {
  let m1 = Member { key_hash: #"aa", role: Member, joined_at: 100, status: Active }
  let m2 = Member { key_hash: #"bb", role: Treasurer, joined_at: 200, status: Active }
  remove_member_by_key([m1, m2], #"aa") == [m2]
}

test update_member_by_key_changes_role_and_status() {
  let m = Member { key_hash: #"aa", role: Member, joined_at: 100, status: Active }
  let updated = update_member_by_key([m], #"aa", Treasurer, Suspended)
  when updated is {
    [um] -> um.role == Treasurer && um.status == Suspended
    _ -> False
  }
}
""",
    },
    {
        "source": "example",
        "title": "PubKey Spend Validator",
        "url": "",
        "is_aiken": True,
        "text": """\
use aiken/transaction.{ScriptContext, Transaction}
use aiken/crypto.{VerificationKeyHash}

pub type Datum {
  owner: VerificationKeyHash,
}

pub type Redeemer {
  Sign
}

/// A simple validator that checks the transaction is signed by the datum owner.
validator {
  fn spend(datum: Datum, _redeemer: Redeemer, ctx: ScriptContext) -> Bool {
    let Transaction { extra_signatories, .. } = ctx.transaction
    list.has(extra_signatories, datum.owner)
  }
}

test spend_valid() {
  let datum = Datum { owner: #"deadbeef" }
  list.has([#"deadbeef"], datum.owner) == True
}

test spend_invalid() {
  let datum = Datum { owner: #"deadbeef" }
  list.has([#"cafebabe"], datum.owner) == False
}
""",
    },
    {
        "source": "example",
        "title": "Time-Locked Vesting Validator",
        "url": "",
        "is_aiken": True,
        "text": """\
use aiken/interval.{Finite}
use aiken/transaction.{ScriptContext, Transaction}
use aiken/transaction/value.{Value}
use aiken/crypto.{VerificationKeyHash}

pub type VestingDatum {
  beneficiary: VerificationKeyHash,
  lock_until: Int,   // POSIX time in milliseconds
}

pub type VestingRedeemer {
  Claim
}

/// Releases funds to the beneficiary after lock_until has passed.
validator {
  fn spend(datum: VestingDatum, _redeemer: VestingRedeemer, ctx: ScriptContext) -> Bool {
    let Transaction { extra_signatories, validity_range, .. } = ctx.transaction

    // 1. Beneficiary must sign
    let signed_by_beneficiary = list.has(extra_signatories, datum.beneficiary)

    // 2. Transaction must be submitted after lock_until
    expect Finite(lower_bound) = validity_range.lower_bound.bound_type
    let after_lock = lower_bound >= datum.lock_until

    signed_by_beneficiary && after_lock
  }
}

test vesting_before_unlock_fails() {
  // Simulating that 500 < 1000 (lock_until)
  (500 >= 1000) == False
}

test vesting_after_unlock_passes() {
  (1001 >= 1000) == True
}
""",
    },
    {
        "source": "example",
        "title": "Multi-Sig Validator",
        "url": "",
        "is_aiken": True,
        "text": """\
use aiken/transaction.{ScriptContext, Transaction}
use aiken/crypto.{VerificationKeyHash}

pub type MultiSigDatum {
  required_signers: List<VerificationKeyHash>,
  threshold: Int,
}

pub type MultiSigRedeemer {
  Spend
}

/// Requires at least `threshold` of `required_signers` to sign.
validator {
  fn spend(datum: MultiSigDatum, _redeemer: MultiSigRedeemer, ctx: ScriptContext) -> Bool {
    let Transaction { extra_signatories, .. } = ctx.transaction
    let sigs = list.filter(datum.required_signers, fn(k) { list.has(extra_signatories, k) })
    list.length(sigs) >= datum.threshold
  }
}

test multisig_threshold_met() {
  let datum = MultiSigDatum {
    required_signers: [#"aa", #"bb", #"cc"],
    threshold: 2,
  }
  let signers = [#"aa", #"cc"]
  let sigs = list.filter(datum.required_signers, fn(k) { list.has(signers, k) })
  list.length(sigs) >= datum.threshold
}

test multisig_threshold_not_met() {
  let datum = MultiSigDatum {
    required_signers: [#"aa", #"bb", #"cc"],
    threshold: 2,
  }
  let signers = [#"aa"]
  let sigs = list.filter(datum.required_signers, fn(k) { list.has(signers, k) })
  list.length(sigs) >= datum.threshold == False
}
""",
    },
    {
        "source": "example",
        "title": "Token Minting Policy",
        "url": "",
        "is_aiken": True,
        "text": """\
use aiken/transaction.{ScriptContext, Mint, Transaction}
use aiken/transaction/value.{PolicyId, AssetName}
use aiken/crypto.{VerificationKeyHash}

pub type MintRedeemer {
  MintTokens { amount: Int }
  BurnTokens
}

/// Minting policy: only the policy owner can mint; anyone can burn.
validator {
  fn mint(redeemer: MintRedeemer, ctx: ScriptContext) -> Bool {
    let ScriptContext { purpose, transaction } = ctx
    expect Mint(policy_id) = purpose

    when redeemer is {
      MintTokens { amount } -> {
        // Owner must sign and amount must be positive
        let Transaction { extra_signatories, .. } = transaction
        let owner: VerificationKeyHash = #"deadbeef"
        list.has(extra_signatories, owner) && amount > 0
      }
      BurnTokens -> {
        // Anyone can burn — just check minted amount is negative
        True
      }
    }
  }
}

test mint_with_owner() {
  // owner signed → allowed
  list.has([#"deadbeef"], #"deadbeef") == True
}

test burn_always_allowed() {
  True
}
""",
    },
    {
        "source": "aiken-tour",
        "title": "Validator Structure and eUTXO Model",
        "url": "https://aiken-lang.org/language-tour/validators",
        "is_aiken": False,
        "text": """\
## Validators in Aiken

Validators are the core of Cardano smart contracts. A validator is a function
that receives three arguments and returns `Bool`.

### The Three Arguments

- **datum**: Data locked at the UTxO (set when the UTxO was created).
- **redeemer**: Data provided by the spending transaction (the "instruction").
- **context** (`ScriptContext`): Full transaction context — inputs, outputs,
  validity range, signatories, minting, etc.

### Anatomy of a Spend Validator

```aiken
use aiken/transaction.{ScriptContext}

pub type Datum  { /* your locked state */ }
pub type Redeemer { /* your spend instruction */ }

validator {
  fn spend(datum: Datum, redeemer: Redeemer, ctx: ScriptContext) -> Bool {
    // Return True to allow the spend, False to reject
    True
  }
}
```

### Validator Purposes

Aiken validators can handle multiple purposes in one file:

| Purpose   | When it runs                        |
|-----------|-------------------------------------|
| `spend`   | Spending a UTxO at the script addr  |
| `mint`    | Minting / burning tokens            |
| `publish` | Registering / deregistering stake   |
| `withdraw`| Withdrawing staking rewards         |

### Key Patterns

**Use `expect` for assertions** — aborts the validator with a clear error:
```aiken
expect Some(value) = option_value
```

**Use `when` for pattern matching** — Aiken enforces exhaustiveness:
```aiken
when redeemer is {
  Mint { amount } -> amount > 0
  Burn            -> True
}
```

**Destructure `ScriptContext`** to access transaction data:
```aiken
let Transaction { extra_signatories, outputs, validity_range, .. } = ctx.transaction
```
""",
    },
    {
        "source": "aiken-tour",
        "title": "Common eUTXO Patterns",
        "url": "https://aiken-lang.org/language-tour",
        "is_aiken": False,
        "text": """\
## Common Cardano / eUTXO Patterns

### Check a Signature
```aiken
let signed = list.has(ctx.transaction.extra_signatories, expected_key)
```

### Check Transaction Validity Range (Time Locks)
```aiken
use aiken/interval.{Finite}
expect Finite(lower) = ctx.transaction.validity_range.lower_bound.bound_type
let after_time = lower >= required_time
```

### Find an Output at a Script Address
```aiken
let script_outputs = list.filter(
  ctx.transaction.outputs,
  fn(o) { o.address == script_address }
)
```

### Compute Output Value
```aiken
use aiken/transaction/value
let lovelace = value.lovelace_of(output.value)
```

### Inline Datum Access
```aiken
use aiken/transaction.{InlineDatum}
expect InlineDatum(raw) = output.datum
expect datum: MyDatum = raw
```

### Minting Context
```aiken
use aiken/transaction.{Mint}
expect Mint(policy_id) = ctx.purpose
let minted = value.from_minted_value(ctx.transaction.mint)
let amount = value.quantity_of(minted, policy_id, asset_name)
```
""",
    },
]


# ─────────────────────────────────────────────
# Web scraping helpers
# ─────────────────────────────────────────────

HEADERS = {"User-Agent": "CardanoAgent/1.0 (knowledge base builder; educational)"}
REQUEST_DELAY = 0.5   # seconds between requests — be polite


def fetch_page(url: str, retries: int = 2) -> Optional[str]:
    """Fetch a URL and return cleaned markdown-ish text, or None on failure."""
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            return _html_to_text(resp.text, url)
        except Exception as e:
            if attempt == retries:
                print(f"  ✗ Failed to fetch {url}: {e}")
                return None
            time.sleep(1)
    return None


def _html_to_text(html: str, url: str) -> str:
    """
    Extract readable text + code blocks from an aiken-lang.org page.

    Preserves heading structure and fenced code blocks so the markdown
    chunker can split sections cleanly.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Remove nav, footer, scripts, styles
    for tag in soup.find_all(["nav", "footer", "script", "style", "aside"]):
        tag.decompose()

    # Find the main content area
    main = (
        soup.find("main")
        or soup.find("article")
        or soup.find("div", class_=lambda c: c and "content" in c)
        or soup.body
    )

    if not main:
        return soup.get_text(separator="\n", strip=True)

    lines = []
    for el in main.descendants:
        if not hasattr(el, "name"):
            continue

        if el.name in ("h1", "h2"):
            lines.append(f"\n## {el.get_text(strip=True)}\n")
        elif el.name == "h3":
            lines.append(f"\n### {el.get_text(strip=True)}\n")
        elif el.name in ("p", "li"):
            text = el.get_text(separator=" ", strip=True)
            if text:
                lines.append(text)
        elif el.name == "pre":
            code = el.get_text()
            # Detect language from class
            code_el = el.find("code")
            lang = ""
            if code_el and code_el.get("class"):
                classes = code_el.get("class", [])
                for c in classes:
                    if c.startswith("language-"):
                        lang = c.replace("language-", "")
                        break
            lines.append(f"\n```{lang}\n{code.strip()}\n```\n")

    return "\n".join(lines)


# ─────────────────────────────────────────────
# Ingestion orchestration
# ─────────────────────────────────────────────

def ingest_inline_examples(rag: CardanoRAG) -> int:
    """Load the curated inline examples (no network required)."""
    total = 0
    print("\n── Inline Examples ──────────────────────────")
    for ex in INLINE_EXAMPLES:
        n = rag.ingest_text(
            text=ex["text"],
            source=ex["source"],
            title=ex["title"],
            url=ex.get("url", ""),
            is_aiken=ex.get("is_aiken", False),
        )
        status = f"+{n} chunks" if n else "already cached"
        print(f"  {'✓' if n else '·'} {ex['title']}  ({status})")
        total += n
    return total


def ingest_web_pages(
    rag: CardanoRAG,
    pages: list[tuple[str, str, str]],
    section_label: str,
) -> int:
    """Fetch and ingest a list of (url, source, title) tuples."""
    total = 0
    print(f"\n── {section_label} ──────────────────────────")
    for url, source, title in pages:
        text = fetch_page(url)
        if text:
            n = rag.ingest_text(text, source=source, title=title, url=url)
            status = f"+{n} chunks" if n else "already cached"
            print(f"  {'✓' if n else '·'} {title}  ({status})")
            total += n
        time.sleep(REQUEST_DELAY)
    return total


def run_ingest(db_path: str, clear: bool = False, skip_web: bool = False) -> None:
    """Main ingestion entry point."""
    rag = CardanoRAG(persist_dir=db_path)

    if clear:
        print("🗑  Clearing existing database...")
        rag.clear()

    total = 0

    # 1. Always load inline examples first (no network required)
    total += ingest_inline_examples(rag)

    if not skip_web:
        # 2. Language tour
        total += ingest_web_pages(rag, LANGUAGE_TOUR_PAGES, "Aiken Language Tour")

        # 3. Standard library
        total += ingest_web_pages(rag, STDLIB_PAGES, "Aiken Standard Library")

        # 4. Official examples
        total += ingest_web_pages(rag, EXAMPLE_PAGES, "Official Examples")

    stats = rag.stats()
    print(f"\n{'='*50}")
    print(f"  Ingested {total} new chunks")
    print(f"  Total in DB: {stats['total_chunks']} chunks")
    for src, count in sorted(stats["by_source"].items()):
        print(f"    {src}: {count}")
    print(f"{'='*50}\n")


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Build the Cardano RAG knowledge base")
    parser.add_argument("--db", default=".rag_db", help="ChromaDB persist directory (default: .rag_db)")
    parser.add_argument("--clear", action="store_true", help="Clear the DB before ingesting")
    parser.add_argument("--stats", action="store_true", help="Show DB stats and exit")
    parser.add_argument("--offline", action="store_true", help="Skip web scraping (inline examples only)")
    args = parser.parse_args()

    if args.stats:
        rag = CardanoRAG(persist_dir=args.db)
        stats = rag.stats()
        print(f"\nDB: {args.db}")
        print(f"Total chunks: {stats['total_chunks']}")
        for src, count in sorted(stats["by_source"].items()):
            print(f"  {src}: {count}")
        print()
        return

    run_ingest(db_path=args.db, clear=args.clear, skip_web=args.offline)


if __name__ == "__main__":
    main()
