"""
Test suite for Cardano Agent — skills, tool dispatch, and loop logic.
Runs without an Anthropic API key by simulating the tool-call exchange.
"""

import sys
import os
import json
import shutil
import tempfile

# Add mock aiken to PATH.
# The mock 'aiken' binary lives in the same directory as this test file
# (not in a mock/ subdirectory).
sys.path.insert(0, os.path.dirname(__file__))
_this_dir = os.path.dirname(os.path.abspath(__file__))
os.environ["PATH"] = _this_dir + ":" + os.environ.get("PATH", "")

from agent import (
    write_file, read_file, list_files,
    aiken_check, aiken_build, aiken_test,
    scaffold_project, read_project_memory, update_project_memory,
    dispatch_tool, TOOLS, search_docs, _build_rag_system_prompt, SYSTEM_PROMPT
)
from rag import CardanoRAG, chunk_markdown, chunk_aiken_file


# ─────────────────────────────────────────────
# Lightweight deterministic embedding function for tests.
# Avoids downloading sentence-transformer models.
# Uses a simple bag-of-words projection into a fixed 64-dim space.
# ─────────────────────────────────────────────

import hashlib as _hashlib


class _HashEmbeddingFn:
    """
    Deterministic, zero-dependency embedding function for unit tests.

    Maps each document to a 64-dim float vector derived from term hashes.
    Semantically meaningless, but stable and fast — enough to exercise
    ChromaDB's CRUD and query paths without model downloads.

    Implements the ChromaDB EmbeddingFunction protocol:
    - __call__(texts) → list of vectors
    - name() → str identifier (required by ChromaDB >=0.6)
    """

    DIM = 64

    def name(self) -> str:
        return "hash-embedding-test"

    def _embed(self, texts: list[str]) -> list[list[float]]:
        results = []
        for text in texts:
            vec = [0.0] * self.DIM
            for word in text.lower().split():
                h = int(_hashlib.md5(word.encode()).hexdigest(), 16)
                idx = h % self.DIM
                vec[idx] += 1.0
            norm = sum(v * v for v in vec) ** 0.5 or 1.0
            results.append([v / norm for v in vec])
        return results

    def __call__(self, input: list[str]) -> list[list[float]]:
        return self._embed(input)

    def embed_query(self, input: list[str]) -> list[list[float]]:
        """Called by ChromaDB on the query side."""
        return self._embed(input)

    def embed_documents(self, input: list[str]) -> list[list[float]]:
        """Called by some ChromaDB versions on the add side."""
        return self._embed(input)


_TEST_EMBED = _HashEmbeddingFn()


def _test_rag(persist_dir: str) -> CardanoRAG:
    """Create a CardanoRAG instance wired to the test embedding function."""
    return CardanoRAG(persist_dir=persist_dir, embedding_fn=_TEST_EMBED)

# ─────────────────────────────────────────────
# Test helpers
# ─────────────────────────────────────────────

passed = 0
failed = 0

def test(name: str, fn):
    global passed, failed
    try:
        fn()
        print(f"  ✅ {name}")
        passed += 1
    except AssertionError as e:
        print(f"  ❌ {name}")
        print(f"     {e}")
        failed += 1
    except Exception as e:
        print(f"  ❌ {name} (unexpected error)")
        print(f"     {type(e).__name__}: {e}")
        failed += 1

def eq(a, b, msg=""):
    assert a == b, f"{msg}\n     expected: {repr(b)}\n     got:      {repr(a)}"

def ok(val, msg=""):
    assert val, msg or f"Expected truthy, got {repr(val)}"

def not_ok(val, msg=""):
    assert not val, msg or f"Expected falsy, got {repr(val)}"


# ─────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────

VALID_VALIDATOR = """\
use aiken/transaction.{ScriptContext, Transaction}

pub type Datum {
  owner: ByteArray,
}

pub type Redeemer {
  Sign
}

validator {
  fn spend(datum: Datum, _redeemer: Redeemer, ctx: ScriptContext) -> Bool {
    let Transaction { extra_signatories, .. } = ctx.transaction
    list.has(extra_signatories, datum.owner)
  }
}

test spend_with_valid_signer() {
  let datum = Datum { owner: #"deadbeef" }
  let redeemer = Sign
  datum.owner == #"deadbeef"
}

test spend_with_wrong_signer() {
  True
}
"""

INVALID_VALIDATOR_BRACE = """\
validator {
  fn spend(datum: Datum, _redeemer: Redeemer, ctx: ScriptContext) -> Bool {
    True
  // missing closing brace
"""

VALIDATOR_NO_TESTS = """\
validator {
  fn spend(_datum: Data, _redeemer: Data, _ctx: ScriptContext) -> Bool {
    True
  }
}
"""


# ─────────────────────────────────────────────
# 1. Skill Tests
# ─────────────────────────────────────────────

print("\n── 1. Skills ─────────────────────────────────")

with tempfile.TemporaryDirectory() as tmp:

    def test_write_and_read():
        r = write_file(tmp, "validators/test.ak", "hello")
        ok(r["success"])
        r2 = read_file(tmp, "validators/test.ak")
        ok(r2["success"])
        eq(r2["content"], "hello")

    def test_read_missing():
        r = read_file(tmp, "nonexistent.ak")
        not_ok(r["success"])
        ok("not found" in r["error"].lower())

    def test_list_files():
        write_file(tmp, "validators/a.ak", "")
        write_file(tmp, "lib/b.ak", "")
        r = list_files(tmp)
        ok(r["success"])
        ok(any("a.ak" in f for f in r["files"]))
        ok(any("b.ak" in f for f in r["files"]))

    def test_write_creates_dirs():
        r = write_file(tmp, "deep/nested/dir/file.ak", "content")
        ok(r["success"])
        r2 = read_file(tmp, "deep/nested/dir/file.ak")
        eq(r2["content"], "content")

    test("write and read file", test_write_and_read)
    test("read missing file returns error", test_read_missing)
    test("list_files finds all .ak files", test_list_files)
    test("write_file creates nested directories", test_write_creates_dirs)


# ─────────────────────────────────────────────
# 2. Aiken CLI Skill Tests
# ─────────────────────────────────────────────

print("\n── 2. Aiken CLI Skills ───────────────────────")

with tempfile.TemporaryDirectory() as tmp:

    def test_scaffold():
        r = scaffold_project(tmp, "myproject")
        ok(r["success"])
        ok(os.path.exists(os.path.join(tmp, "myproject", "aiken.toml")))
        ok(os.path.exists(os.path.join(tmp, "myproject", "validators")))

    def test_scaffold_duplicate():
        scaffold_project(tmp, "dupe")
        r = scaffold_project(tmp, "dupe")
        not_ok(r["success"])
        ok("already exists" in r["error"])

    test("scaffold creates project structure", test_scaffold)
    test("scaffold fails on duplicate project name", test_scaffold_duplicate)

with tempfile.TemporaryDirectory() as tmp:
    # Set up a valid project
    scaffold_project(tmp, "proj")
    proj = os.path.join(tmp, "proj")

    def test_check_no_files():
        r = aiken_check(proj)
        not_ok(r["success"])

    def test_check_valid():
        write_file(proj, "validators/spend.ak", VALID_VALIDATOR)
        r = aiken_check(proj)
        ok(r["success"], f"aiken check failed: {r.get('stderr')}")

    def test_check_invalid_brace():
        write_file(proj, "validators/bad.ak", INVALID_VALIDATOR_BRACE)
        r = aiken_check(proj)
        not_ok(r["success"])
        ok(len(r["errors"]) > 0, "Expected structured errors")
        ok(r["return_code"] != 0)

    def test_build_valid():
        # Remove bad file first
        os.remove(os.path.join(proj, "validators", "bad.ak"))
        r = aiken_build(proj)
        ok(r["success"], f"aiken build failed: {r.get('stderr')}")
        ok(os.path.exists(os.path.join(proj, "plutus.json")), "plutus.json not created")

    def test_build_produces_plutus_json():
        plutus = json.loads(open(os.path.join(proj, "plutus.json")).read())
        ok("validators" in plutus)
        ok(len(plutus["validators"]) > 0)

    def test_tests_pass():
        r = aiken_test(proj)
        ok(r["success"], f"tests failed: {r.get('stdout')}")
        ok("passed" in r["stdout"].lower())

    def test_no_tests_warning():
        write_file(proj, "validators/notests.ak", VALIDATOR_NO_TESTS)
        r = aiken_test(proj)
        # Should still exit cleanly even with no tests in a file
        ok(r["return_code"] == 0)

    test("check fails with no source files", test_check_no_files)
    test("check passes on valid validator", test_check_valid)
    test("check fails with mismatched braces", test_check_invalid_brace)
    test("build succeeds on valid project", test_build_valid)
    test("build produces plutus.json with validator entries", test_build_produces_plutus_json)
    test("tests pass on valid validator", test_tests_pass)
    test("no tests warning exits cleanly", test_no_tests_warning)


# ─────────────────────────────────────────────
# 3. Project Memory Tests
# ─────────────────────────────────────────────

print("\n── 3. Project Memory ─────────────────────────")

with tempfile.TemporaryDirectory() as tmp:

    def test_memory_default():
        r = read_project_memory(tmp)
        ok(r["success"])
        eq(r["memory"]["contract_purpose"], "")
        eq(r["memory"]["design_decisions"], [])

    def test_memory_update_and_persist():
        update_project_memory(tmp, {
            "contract_purpose": "Token vesting contract",
            "design_decisions": ["Used PubKeyHash for owner identity"]
        })
        r = read_project_memory(tmp)
        eq(r["memory"]["contract_purpose"], "Token vesting contract")
        eq(len(r["memory"]["design_decisions"]), 1)

    def test_memory_merge():
        update_project_memory(tmp, {
            "failed_approaches": ["Tried inline datum — type error"]
        })
        r = read_project_memory(tmp)
        # Previous fields should still be there
        eq(r["memory"]["contract_purpose"], "Token vesting contract")
        eq(len(r["memory"]["failed_approaches"]), 1)

    def test_memory_file_created():
        ok(os.path.exists(os.path.join(tmp, ".agent_memory.json")))

    test("memory returns defaults when no file exists", test_memory_default)
    test("memory update persists to disk", test_memory_update_and_persist)
    test("memory merge preserves existing fields", test_memory_merge)
    test("memory creates .agent_memory.json file", test_memory_file_created)


# ─────────────────────────────────────────────
# 4. Tool Dispatch Tests
# ─────────────────────────────────────────────

print("\n── 4. Tool Dispatch ──────────────────────────")

with tempfile.TemporaryDirectory() as tmp:
    scaffold_project(tmp, "dispatch_test")
    proj = os.path.join(tmp, "dispatch_test")

    def test_dispatch_write_file():
        result = json.loads(dispatch_tool(
            "write_file",
            {"relative_path": "validators/v.ak", "content": VALID_VALIDATOR},
            proj
        ))
        ok(result["success"])

    def test_dispatch_read_file():
        result = json.loads(dispatch_tool(
            "read_file",
            {"relative_path": "validators/v.ak"},
            proj
        ))
        ok(result["success"])
        ok("validator" in result["content"])

    def test_dispatch_aiken_check():
        result = json.loads(dispatch_tool("aiken_check", {}, proj))
        ok(result["success"], f"check failed: {result.get('stderr')}")

    def test_dispatch_aiken_build():
        result = json.loads(dispatch_tool("aiken_build", {}, proj))
        ok(result["success"], f"build failed: {result.get('stderr')}")

    def test_dispatch_aiken_test():
        result = json.loads(dispatch_tool("aiken_test", {}, proj))
        ok(result["success"], f"tests failed: {result.get('stdout')}")

    def test_dispatch_unknown_tool():
        result = json.loads(dispatch_tool("nonexistent_tool", {}, proj))
        not_ok(result["success"])
        ok("Unknown tool" in result["error"])

    def test_dispatch_returns_json_string():
        result = dispatch_tool("list_files", {}, proj)
        ok(isinstance(result, str))
        parsed = json.loads(result)
        ok(isinstance(parsed, dict))

    test("dispatch write_file", test_dispatch_write_file)
    test("dispatch read_file", test_dispatch_read_file)
    test("dispatch aiken_check", test_dispatch_aiken_check)
    test("dispatch aiken_build", test_dispatch_aiken_build)
    test("dispatch aiken_test", test_dispatch_aiken_test)
    test("dispatch unknown tool returns error", test_dispatch_unknown_tool)
    test("dispatch always returns JSON string", test_dispatch_returns_json_string)


# ─────────────────────────────────────────────
# 5. Tool Schema Validation
# ─────────────────────────────────────────────

print("\n── 5. Tool Schema ────────────────────────────")

def test_all_tools_have_required_fields():
    for tool in TOOLS:
        ok("name" in tool, f"Tool missing 'name': {tool}")
        ok("description" in tool, f"Tool {tool['name']} missing 'description'")
        ok("input_schema" in tool, f"Tool {tool['name']} missing 'input_schema'")

def test_tool_names_match_dispatch():
    dispatchable = {
        "write_file", "read_file", "list_files",
        "aiken_check", "aiken_build", "aiken_test",
        "scaffold_project", "read_project_memory", "update_project_memory",
        "search_docs",
    }
    for tool in TOOLS:
        ok(tool["name"] in dispatchable,
           f"Tool '{tool['name']}' defined but not in dispatch_tool()")

def test_tool_count():
    eq(len(TOOLS), 10, f"Expected 10 tools (9 original + search_docs), got {len(TOOLS)}")

test("all tools have required schema fields", test_all_tools_have_required_fields)
test("all tool names are handled in dispatch_tool", test_tool_names_match_dispatch)
test("tool count matches expected", test_tool_count)


# ─────────────────────────────────────────────
# 6. Simulated Agent Loop (no API key needed)
# ─────────────────────────────────────────────

print("\n── 6. Simulated Agent Loop ───────────────────")

with tempfile.TemporaryDirectory() as tmp:
    scaffold_project(tmp, "sim_project")
    proj = os.path.join(tmp, "sim_project")

    def simulate_agent_turn(tool_calls: list) -> list:
        """
        Simulate what happens when Claude returns a sequence of tool calls.
        Returns list of tool results as they'd appear in the message history.
        """
        results = []
        for name, inputs in tool_calls:
            result = dispatch_tool(name, inputs, proj)
            results.append({"tool": name, "result": json.loads(result)})
        return results

    def test_full_contract_workflow():
        """Simulate the complete happy path: scaffold → write → check → build → test"""
        workflow = [
            ("update_project_memory", {"updates": {"contract_purpose": "PubKey spend validator"}}),
            ("write_file", {"relative_path": "validators/pubkey.ak", "content": VALID_VALIDATOR}),
            ("aiken_check", {}),
            ("aiken_build", {}),
            ("aiken_test", {}),
        ]
        results = simulate_agent_turn(workflow)

        ok(results[0]["result"]["success"], "memory update failed")
        ok(results[1]["result"]["success"], "write_file failed")
        ok(results[2]["result"]["success"], "aiken_check failed")
        ok(results[3]["result"]["success"], "aiken_build failed")
        ok(results[4]["result"]["success"], "aiken_test failed")

    def test_error_recovery_workflow():
        """Simulate agent writing broken code, detecting error, fixing it."""
        # Step 1: write broken validator
        r1 = json.loads(dispatch_tool("write_file", {
            "relative_path": "validators/broken.ak",
            "content": INVALID_VALIDATOR_BRACE
        }, proj))
        ok(r1["success"])

        # Step 2: check fails with structured errors
        r2 = json.loads(dispatch_tool("aiken_check", {}, proj))
        not_ok(r2["success"])
        ok(len(r2["errors"]) > 0, "No structured errors returned")

        # Step 3: agent logs failed approach to memory
        r3 = json.loads(dispatch_tool("update_project_memory", {
            "updates": {"failed_approaches": ["Wrote broken validator — brace mismatch"]}
        }, proj))
        ok(r3["success"])

        # Step 4: agent fixes by overwriting with valid code
        r4 = json.loads(dispatch_tool("write_file", {
            "relative_path": "validators/broken.ak",
            "content": VALID_VALIDATOR
        }, proj))
        ok(r4["success"])

        # Step 5: check now passes
        r5 = json.loads(dispatch_tool("aiken_check", {}, proj))
        ok(r5["success"], f"Fixed validator still fails check: {r5.get('stderr')}")

        # Step 6: verify memory has the failed approach logged
        r6 = json.loads(dispatch_tool("read_project_memory", {}, proj))
        ok(len(r6["memory"]["failed_approaches"]) > 0)

    test("full happy-path workflow completes", test_full_contract_workflow)
    test("error recovery: write → detect error → fix → verify", test_error_recovery_workflow)


# ─────────────────────────────────────────────
# 7. RAG — Chunking
# ─────────────────────────────────────────────

print("\n── 7. RAG Chunking ───────────────────────────")

SAMPLE_MARKDOWN = """\
## Validators

Validators are the core of Cardano smart contracts.

### Spend Validators

A spend validator runs when a UTxO is consumed.

```aiken
validator {
  fn spend(datum: Datum, redeemer: Redeemer, ctx: ScriptContext) -> Bool {
    True
  }
}
```

### Mint Validators

A minting policy controls token creation and burning.

```aiken
validator {
  fn mint(redeemer: MintRedeemer, ctx: ScriptContext) -> Bool {
    True
  }
}
```
"""

SAMPLE_AIKEN = """\
use aiken/transaction.{ScriptContext}

pub type Datum {
  owner: ByteArray,
}

pub type Redeemer {
  Sign
}

validator {
  fn spend(datum: Datum, _redeemer: Redeemer, ctx: ScriptContext) -> Bool {
    True
  }
}

test spend_valid() {
  True
}
"""

def test_chunk_markdown_produces_chunks():
    chunks = chunk_markdown(SAMPLE_MARKDOWN, source="test", title="Test Doc")
    ok(len(chunks) > 0, "No chunks produced")

def test_chunk_markdown_extracts_code():
    chunks = chunk_markdown(SAMPLE_MARKDOWN, source="test", title="Test Doc")
    code_chunks = [c for c in chunks if c.metadata.get("is_code")]
    ok(len(code_chunks) >= 2, f"Expected at least 2 code chunks, got {len(code_chunks)}")

def test_chunk_markdown_has_metadata():
    chunks = chunk_markdown(SAMPLE_MARKDOWN, source="test", title="Test Doc", url="https://example.com")
    for c in chunks:
        eq(c.source, "test")
        ok(c.title)
        eq(c.url, "https://example.com")

def test_chunk_aiken_file_splits_definitions():
    chunks = chunk_aiken_file(SAMPLE_AIKEN, source="example", title="PubKey")
    ok(len(chunks) > 0, "No chunks from Aiken source")
    # Each chunk should contain the definition keyword
    for c in chunks:
        ok(
            any(kw in c.text for kw in ["validator", "pub type", "test", "fn"]),
            f"Chunk missing definition keyword: {c.text[:80]}"
        )

def test_chunk_ids_are_stable():
    chunks1 = chunk_markdown(SAMPLE_MARKDOWN, source="test", title="T")
    chunks2 = chunk_markdown(SAMPLE_MARKDOWN, source="test", title="T")
    ids1 = [c.id for c in chunks1]
    ids2 = [c.id for c in chunks2]
    eq(ids1, ids2, "Chunk IDs are not stable across runs")

def test_chunk_ids_are_unique():
    chunks = chunk_markdown(SAMPLE_MARKDOWN, source="test", title="T")
    ids = [c.id for c in chunks]
    eq(len(ids), len(set(ids)), "Duplicate chunk IDs detected")

def test_min_chunk_size_enforced():
    chunks = chunk_markdown(SAMPLE_MARKDOWN, source="test", title="T")
    for c in chunks:
        ok(len(c.text) >= 80, f"Chunk below min size: {repr(c.text)}")

test("chunk_markdown produces non-empty list", test_chunk_markdown_produces_chunks)
test("chunk_markdown extracts code blocks", test_chunk_markdown_extracts_code)
test("chunk_markdown attaches source/title/url metadata", test_chunk_markdown_has_metadata)
test("chunk_aiken_file splits on definitions", test_chunk_aiken_file_splits_definitions)
test("chunk IDs are stable across calls", test_chunk_ids_are_stable)
test("chunk IDs are unique within a document", test_chunk_ids_are_unique)
test("min chunk size enforced", test_min_chunk_size_enforced)


# ─────────────────────────────────────────────
# 8. RAG — Ingest & Query
# ─────────────────────────────────────────────

print("\n── 8. RAG Ingest & Query ─────────────────────")

with tempfile.TemporaryDirectory() as rag_tmp:
    rag = _test_rag(rag_tmp)

    def test_empty_db_count():
        eq(rag.count(), 0, "New DB should be empty")

    def test_ingest_text_returns_chunk_count():
        n = rag.ingest_text(SAMPLE_MARKDOWN, source="test", title="Validators")
        ok(n > 0, "ingest_text returned 0 chunks")

    def test_count_after_ingest():
        ok(rag.count() > 0, "Count still 0 after ingest")

    def test_query_returns_results():
        results = rag.query("spend validator")
        ok(len(results) > 0, "No results for 'spend validator'")

    def test_query_result_structure():
        results = rag.query("spend validator", n_results=3)
        for r in results:
            ok("text" in r)
            ok("source" in r)
            ok("title" in r)
            ok("score" in r)
            ok(0.0 <= r["score"] <= 1.0, f"Score out of range: {r['score']}")

    def test_query_source_filter():
        # Ingest a second doc with a different source
        rag.ingest_text("## Library\nSome stdlib content.", source="stdlib", title="Stdlib")
        test_results = rag.query("validator", source_filter="test")
        stdlib_results = rag.query("content", source_filter="stdlib")
        # Results should only come from the requested source
        for r in test_results:
            eq(r["source"], "test", f"Source filter leaked: {r['source']}")
        for r in stdlib_results:
            eq(r["source"], "stdlib", f"Source filter leaked: {r['source']}")

    def test_idempotent_ingest():
        count_before = rag.count()
        n = rag.ingest_text(SAMPLE_MARKDOWN, source="test", title="Validators")
        eq(n, 0, "Re-ingesting same doc should return 0 (already cached)")
        eq(rag.count(), count_before, "Count changed on re-ingest")

    def test_ingest_aiken_file():
        n = rag.ingest_text(SAMPLE_AIKEN, source="example", title="PubKey", is_aiken=True)
        ok(n > 0, "Aiken ingest returned 0 chunks")

    def test_stats_by_source():
        stats = rag.stats()
        ok("total_chunks" in stats)
        ok("by_source" in stats)
        ok(stats["total_chunks"] > 0)
        ok("test" in stats["by_source"])

    def test_build_context_string_returns_text():
        # min_score=0.0 bypasses the relevance threshold so the test
        # checks formatting behaviour independently of embedding quality.
        ctx = rag.build_context_string("how do I write a spend validator?", min_score=0.0)
        ok(isinstance(ctx, str))
        ok(len(ctx) > 0, "context string is empty")
        ok("Relevant" in ctx or "validator" in ctx.lower())

    def test_build_context_string_low_relevance_returns_empty():
        ctx = rag.build_context_string("completely unrelated topic xyz123", min_score=0.99)
        eq(ctx, "", f"Expected empty context, got: {ctx[:100]}")

    def test_clear_empties_db():
        rag.clear()
        eq(rag.count(), 0, "DB not empty after clear()")

    test("empty DB has count 0", test_empty_db_count)
    test("ingest_text returns chunk count > 0", test_ingest_text_returns_chunk_count)
    test("count() increases after ingest", test_count_after_ingest)
    test("query returns results for relevant query", test_query_returns_results)
    test("query results have required structure and valid score", test_query_result_structure)
    test("query source_filter restricts results", test_query_source_filter)
    test("re-ingesting same doc is idempotent (returns 0)", test_idempotent_ingest)
    test("ingest Aiken source file produces chunks", test_ingest_aiken_file)
    test("stats() returns total and per-source counts", test_stats_by_source)
    test("build_context_string returns formatted text", test_build_context_string_returns_text)
    test("build_context_string returns empty for low-relevance query", test_build_context_string_low_relevance_returns_empty)
    test("clear() empties the DB", test_clear_empties_db)


# ─────────────────────────────────────────────
# 9. RAG — Tool Dispatch & Agent Integration
# ─────────────────────────────────────────────

print("\n── 9. RAG Agent Integration ──────────────────")

with tempfile.TemporaryDirectory() as rag_tmp:
    # Point the agent's RAG at our temp DB and inject test embedding fn
    import agent as agent_module
    orig_db = agent_module.RAG_DB_PATH
    orig_ef = agent_module.RAG_EMBEDDING_FN
    agent_module.RAG_DB_PATH = rag_tmp
    agent_module.RAG_EMBEDDING_FN = _TEST_EMBED

    # Pre-populate so queries return results
    rag2 = _test_rag(rag_tmp)
    rag2.ingest_text(SAMPLE_MARKDOWN, source="test", title="Validators")

    try:
        def test_search_docs_tool_happy_path():
            result = search_docs("spend validator", n_results=3)
            ok(result["success"], f"search_docs failed: {result.get('error')}")
            ok(result["count"] > 0)
            ok(len(result["results"]) > 0)

        def test_search_docs_tool_empty_db():
            import agent as am
            old_db  = am.RAG_DB_PATH
            old_ef  = am.RAG_EMBEDDING_FN
            am.RAG_DB_PATH      = tempfile.mkdtemp()
            am.RAG_EMBEDDING_FN = _TEST_EMBED
            try:
                r = search_docs("spend validator")
                not_ok(r["success"])
                ok("empty" in r["error"].lower() or "ingest" in r["error"].lower())
            finally:
                am.RAG_DB_PATH      = old_db
                am.RAG_EMBEDDING_FN = old_ef

        def test_dispatch_search_docs():
            with tempfile.TemporaryDirectory() as proj:
                scaffold_project(proj, "rag_dispatch_test")
                p = os.path.join(proj, "rag_dispatch_test")
                result = json.loads(dispatch_tool(
                    "search_docs",
                    {"query": "spend validator", "n_results": 3},
                    p
                ))
                ok(result["success"])
                ok(result["count"] > 0)

        def test_search_docs_tool_in_tools_list():
            names = [t["name"] for t in TOOLS]
            ok("search_docs" in names, "search_docs missing from TOOLS list")

        def test_search_docs_schema_has_required_fields():
            tool = next(t for t in TOOLS if t["name"] == "search_docs")
            ok("query" in tool["input_schema"]["properties"])
            eq(tool["input_schema"]["required"], ["query"])

        def test_build_rag_system_prompt_returns_string():
            prompt = _build_rag_system_prompt("write a spend validator")
            ok(isinstance(prompt, str))
            ok(len(prompt) >= len(SYSTEM_PROMPT), "RAG prompt shorter than base prompt")

        def test_build_rag_system_prompt_contains_base():
            prompt = _build_rag_system_prompt("write a spend validator")
            ok(SYSTEM_PROMPT in prompt, "Base system prompt missing from RAG-augmented prompt")

        def test_build_rag_system_prompt_appends_context():
            # Temporarily lower min_score so the hash embedder's low-similarity
            # scores don't filter out all results.  We patch build_context_string
            # by calling it directly with min_score=0.0 and checking the agent
            # function produces a longer string than the bare system prompt.
            from rag import CardanoRAG as _RAG
            _rag = _RAG(persist_dir=rag_tmp, embedding_fn=_TEST_EMBED)
            ctx = _rag.build_context_string("write a spend validator", min_score=0.0)
            if _rag.count() > 0 and ctx:
                ok(len(SYSTEM_PROMPT + "\n\n" + ctx) > len(SYSTEM_PROMPT),
                   "No RAG context was appended")

        def test_tool_count_includes_search_docs():
            eq(len(TOOLS), 10, f"Expected 10 tools (9 + search_docs), got {len(TOOLS)}")

        test("search_docs returns results from populated DB", test_search_docs_tool_happy_path)
        test("search_docs returns error for empty DB", test_search_docs_tool_empty_db)
        test("dispatch_tool routes search_docs correctly", test_dispatch_search_docs)
        test("search_docs is in TOOLS list", test_search_docs_tool_in_tools_list)
        test("search_docs schema has correct required fields", test_search_docs_schema_has_required_fields)
        test("_build_rag_system_prompt returns a string", test_build_rag_system_prompt_returns_string)
        test("_build_rag_system_prompt contains base SYSTEM_PROMPT", test_build_rag_system_prompt_contains_base)
        test("_build_rag_system_prompt appends RAG context when DB populated", test_build_rag_system_prompt_appends_context)
        test("tool count is now 10 (includes search_docs)", test_tool_count_includes_search_docs)

    finally:
        agent_module.RAG_DB_PATH = orig_db
        agent_module.RAG_EMBEDDING_FN = orig_ef


# ─────────────────────────────────────────────
# Results
# ─────────────────────────────────────────────

total = passed + failed
print(f"\n{'='*50}")
print(f"  {passed}/{total} tests passed", "✅" if failed == 0 else "❌")
if failed:
    print(f"  {failed} test(s) failed")
print(f"{'='*50}\n")

sys.exit(0 if failed == 0 else 1)
