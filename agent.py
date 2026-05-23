"""
Cardano Coding Agent
A Claude-powered agent that builds Cardano smart contracts from natural language.
"""

import os
import json
import subprocess
import anthropic
from pathlib import Path
from datetime import datetime

# RAG layer — gracefully optional so the agent still runs without the DB
try:
    from rag import CardanoRAG
    _RAG_AVAILABLE = True
except ImportError:
    _RAG_AVAILABLE = False

# Default RAG database location (sibling to this file)
RAG_DB_PATH = os.environ.get(
    "CARDANO_RAG_DB",
    str(Path(__file__).parent / ".rag_db")
)

# Override embedding function (None = use default sentence-transformers).
# Set this in tests to avoid model downloads:
#   import agent; agent.RAG_EMBEDDING_FN = my_test_fn
RAG_EMBEDDING_FN = None


# ─────────────────────────────────────────────
# SYSTEM PROMPT
# ─────────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert Cardano smart contract engineer specializing in Aiken.
Your job is to take natural language descriptions and produce working, tested Aiken validators.

## Cardano / eUTXO Mental Model
- Transactions consume UTXOs and produce new UTXOs — there are no mutable accounts
- Every UTXO at a script address is locked by a validator
- To spend a UTXO, a transaction must satisfy the validator
- Validators receive three arguments: datum (locked state), redeemer (spend action), context (tx info)
- Validators return Bool — True allows the spend, False rejects it

## Aiken Patterns You Must Follow
1. Always define datum and redeemer as distinct types
2. Always handle all pattern match cases — Aiken enforces exhaustiveness
3. Use `expect` for assertions that should fail the validator if false
4. Prefer `when` expressions over `if/else` for pattern matching
5. Always write at least one test per validator using `test` blocks

## Your Workflow
1. Understand the intent — ask for clarification if the requirement is ambiguous
2. Plan the datum, redeemer, and validator logic before writing code
3. Write the full validator file with types and tests
4. Use tools to build and test — iterate on errors until passing
5. Explain what you built and why

## Error Handling Rules
- Always read the full error before forming a fix hypothesis
- Log your hypothesis before applying a fix
- If the same error repeats after 2 fixes, surface it to the user with full context
- Never silently discard errors

## Output Rules
- Always write complete files — never partial snippets
- File paths are relative to the project root
- Aiken source files go in /validators/ or /lib/
- Tests live in the same file as the validator using `test` blocks
"""


# ─────────────────────────────────────────────
# SKILLS (tool functions)
# ─────────────────────────────────────────────

def write_file(project_path: str, relative_path: str, content: str) -> dict:
    """Write a file inside the project directory."""
    full_path = Path(project_path) / relative_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(content)
    return {"success": True, "path": str(full_path)}


def read_file(project_path: str, relative_path: str) -> dict:
    """Read a file from the project directory."""
    full_path = Path(project_path) / relative_path
    if not full_path.exists():
        return {"success": False, "error": f"File not found: {relative_path}"}
    return {"success": True, "content": full_path.read_text()}


def list_files(project_path: str) -> dict:
    """List all files in the project directory."""
    files = []
    for p in Path(project_path).rglob("*"):
        if p.is_file() and ".git" not in str(p):
            files.append(str(p.relative_to(project_path)))
    return {"success": True, "files": files}


def aiken_check(project_path: str) -> dict:
    """Run aiken check (fast type-check, no full build)."""
    result = subprocess.run(
        ["aiken", "check"],
        cwd=project_path,
        capture_output=True,
        text=True
    )
    return _parse_aiken_output(result)


def aiken_build(project_path: str) -> dict:
    """Run aiken build (full compilation)."""
    result = subprocess.run(
        ["aiken", "build"],
        cwd=project_path,
        capture_output=True,
        text=True
    )
    return _parse_aiken_output(result)


def aiken_test(project_path: str) -> dict:
    """Run aiken test suite."""
    result = subprocess.run(
        ["aiken", "test"],
        cwd=project_path,
        capture_output=True,
        text=True
    )
    return _parse_aiken_output(result)


def scaffold_project(base_path: str, project_name: str) -> dict:
    """Initialize a new Aiken project."""
    project_path = Path(base_path) / project_name
    if project_path.exists():
        return {"success": False, "error": f"Project '{project_name}' already exists"}

    result = subprocess.run(
        ["aiken", "new", project_name],
        cwd=base_path,
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        return {"success": False, "error": result.stderr}

    return {
        "success": True,
        "project_path": str(project_path),
        "message": f"Scaffolded Aiken project at {project_path}"
    }


def search_docs(query: str, n_results: int = 5, source: str = "") -> dict:
    """Search the Aiken / Cardano RAG knowledge base."""
    if not _RAG_AVAILABLE:
        return {"success": False, "error": "RAG not available. Run: pip install chromadb sentence-transformers --break-system-packages"}

    rag = CardanoRAG(persist_dir=RAG_DB_PATH, embedding_fn=RAG_EMBEDDING_FN)
    if rag.count() == 0:
        return {
            "success": False,
            "error": "Knowledge base is empty. Run: python rag_ingest.py"
        }

    results = rag.query(query, n_results=n_results, source_filter=source or None)
    return {
        "success": True,
        "results": results,
        "count": len(results),
    }


def read_project_memory(project_path: str) -> dict:
    """Read the agent's working memory for this project."""
    memory_path = Path(project_path) / ".agent_memory.json"
    if not memory_path.exists():
        return {"success": True, "memory": _default_memory()}
    return {"success": True, "memory": json.loads(memory_path.read_text())}


def update_project_memory(project_path: str, updates: dict) -> dict:
    """Update the agent's working memory for this project."""
    memory_path = Path(project_path) / ".agent_memory.json"
    memory = _default_memory()
    if memory_path.exists():
        memory = json.loads(memory_path.read_text())
    memory.update(updates)
    memory["last_updated"] = datetime.now().isoformat()
    memory_path.write_text(json.dumps(memory, indent=2))
    return {"success": True}


# ─────────────────────────────────────────────
# TOOL DEFINITIONS (Claude API format)
# ─────────────────────────────────────────────

TOOLS = [
    {
        "name": "write_file",
        "description": "Write a file to the Aiken project. Use for validators, lib modules, and config files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "relative_path": {"type": "string", "description": "Path relative to project root, e.g. validators/my_validator.ak"},
                "content": {"type": "string", "description": "Full file content to write"}
            },
            "required": ["relative_path", "content"]
        }
    },
    {
        "name": "read_file",
        "description": "Read a file from the Aiken project.",
        "input_schema": {
            "type": "object",
            "properties": {
                "relative_path": {"type": "string", "description": "Path relative to project root"}
            },
            "required": ["relative_path"]
        }
    },
    {
        "name": "list_files",
        "description": "List all files in the project to understand its current structure.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "aiken_check",
        "description": "Fast type-check the project without full compilation. Use this first to catch type errors quickly.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "aiken_build",
        "description": "Full compilation of the Aiken project. Run after aiken_check passes.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "aiken_test",
        "description": "Run all tests in the project. Run after aiken_build succeeds.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "scaffold_project",
        "description": "Initialize a new Aiken project. Use this at the start of a new contract.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_name": {"type": "string", "description": "Name for the new Aiken project"}
            },
            "required": ["project_name"]
        }
    },
    {
        "name": "search_docs",
        "description": (
            "Search the Aiken / Cardano knowledge base for relevant documentation, "
            "stdlib references, and validator examples. "
            "Call this when you need to look up Aiken syntax, stdlib functions, "
            "or want a reference example before writing a new validator."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language query, e.g. 'how to check validity range' or 'minting policy example'"
                },
                "n_results": {
                    "type": "integer",
                    "description": "Number of results to return (default 5)",
                    "default": 5
                },
                "source": {
                    "type": "string",
                    "description": "Filter by source: 'aiken-tour', 'stdlib', or 'example'. Leave empty to search all.",
                    "default": ""
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "read_project_memory",
        "description": "Read the agent's working memory — design decisions, failed approaches, open questions.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "update_project_memory",
        "description": "Update the agent's working memory with new decisions, hypotheses, or open questions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "updates": {
                    "type": "object",
                    "description": "Fields to update in memory",
                    "properties": {
                        "contract_purpose": {"type": "string"},
                        "design_decisions": {"type": "array", "items": {"type": "string"}},
                        "failed_approaches": {"type": "array", "items": {"type": "string"}},
                        "open_questions": {"type": "array", "items": {"type": "string"}}
                    }
                }
            },
            "required": ["updates"]
        }
    }
]


# ─────────────────────────────────────────────
# TOOL DISPATCHER
# ─────────────────────────────────────────────

def dispatch_tool(name: str, inputs: dict, project_path: str) -> str:
    """Route a tool call to the right skill function."""
    try:
        if name == "write_file":
            result = write_file(project_path, inputs["relative_path"], inputs["content"])
        elif name == "read_file":
            result = read_file(project_path, inputs["relative_path"])
        elif name == "list_files":
            result = list_files(project_path)
        elif name == "aiken_check":
            result = aiken_check(project_path)
        elif name == "aiken_build":
            result = aiken_build(project_path)
        elif name == "aiken_test":
            result = aiken_test(project_path)
        elif name == "scaffold_project":
            result = scaffold_project(
                str(Path(project_path).parent),
                inputs["project_name"]
            )
        elif name == "search_docs":
            result = search_docs(
                inputs["query"],
                n_results=inputs.get("n_results", 5),
                source=inputs.get("source", ""),
            )
        elif name == "read_project_memory":
            result = read_project_memory(project_path)
        elif name == "update_project_memory":
            result = update_project_memory(project_path, inputs["updates"])
        else:
            result = {"success": False, "error": f"Unknown tool: {name}"}
    except Exception as e:
        result = {"success": False, "error": str(e)}

    return json.dumps(result)


# ─────────────────────────────────────────────
# AGENT LOOP
# ─────────────────────────────────────────────

def _build_rag_system_prompt(user_message: str) -> str:
    """
    Auto-retrieve relevant docs for the user's message and prepend them
    to the base system prompt.  Returns the plain SYSTEM_PROMPT if RAG
    is unavailable or the DB is empty.
    """
    if not _RAG_AVAILABLE:
        return SYSTEM_PROMPT

    try:
        rag = CardanoRAG(persist_dir=RAG_DB_PATH, embedding_fn=RAG_EMBEDDING_FN)
        if rag.count() == 0:
            return SYSTEM_PROMPT

        context = rag.build_context_string(user_message, n_results=6, min_score=0.3)
        if not context:
            return SYSTEM_PROMPT

        return SYSTEM_PROMPT + "\n\n" + context

    except Exception:
        # Never let RAG errors block the agent
        return SYSTEM_PROMPT


def run_agent(user_message: str, project_path: str, max_iterations: int = 20):
    """
    Main agent loop. Runs until the task is complete or max iterations hit.
    """
    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": user_message}]
    iteration = 0

    print(f"\n{'='*60}")
    print(f"🤖 Cardano Agent Starting")
    print(f"📁 Project: {project_path}")

    # RAG status line
    if _RAG_AVAILABLE:
        try:
            rag = CardanoRAG(persist_dir=RAG_DB_PATH, embedding_fn=RAG_EMBEDDING_FN)
            chunk_count = rag.count()
            if chunk_count > 0:
                print(f"📚 RAG: {chunk_count} chunks loaded from {RAG_DB_PATH}")
            else:
                print(f"📚 RAG: DB empty — run `python rag_ingest.py` to build knowledge base")
        except Exception:
            print(f"📚 RAG: unavailable")
    else:
        print(f"📚 RAG: not installed — run `pip install chromadb sentence-transformers`")

    print(f"{'='*60}\n")

    # Build system prompt with auto-retrieved context
    system_prompt = _build_rag_system_prompt(user_message)

    while iteration < max_iterations:
        iteration += 1
        print(f"── Iteration {iteration} ──────────────────────")

        response = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=8096,
            system=system_prompt,
            tools=TOOLS,
            messages=messages
        )

        # Add assistant response to history
        messages.append({"role": "assistant", "content": response.content})

        # Check stop reason
        if response.stop_reason == "end_turn":
            # Extract and print final text response
            for block in response.content:
                if hasattr(block, "text"):
                    print(f"\n✅ Agent: {block.text}")
            print(f"\n{'='*60}")
            print("Agent completed task.")
            print(f"{'='*60}\n")
            break

        # Process tool calls
        if response.stop_reason == "tool_use":
            tool_results = []

            for block in response.content:
                if block.type == "tool_use":
                    print(f"🔧 Tool: {block.name}")
                    if block.name not in ["read_project_memory", "list_files"]:
                        # Show inputs for non-trivial tools (truncated)
                        inputs_str = json.dumps(block.input)
                        print(f"   Input: {inputs_str[:120]}{'...' if len(inputs_str) > 120 else ''}")

                    result = dispatch_tool(block.name, block.input, project_path)
                    result_dict = json.loads(result)

                    # Show result summary
                    if result_dict.get("success"):
                        print(f"   ✓ OK")
                    else:
                        print(f"   ✗ Error: {result_dict.get('error', 'unknown')[:100]}")

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result
                    })

                elif block.type == "text" and block.text.strip():
                    print(f"\n💬 {block.text.strip()}\n")

            # Add tool results to history
            messages.append({"role": "user", "content": tool_results})

    if iteration >= max_iterations:
        print(f"\n⚠️  Max iterations ({max_iterations}) reached. Surfacing to user.")

    return messages


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def _parse_aiken_output(result: subprocess.CompletedProcess) -> dict:
    """Parse aiken CLI output into a structured result."""
    return {
        "success": result.returncode == 0,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "return_code": result.returncode,
        # Surface errors prominently so the agent sees them clearly
        "errors": _extract_errors(result.stderr) if result.returncode != 0 else []
    }


def _extract_errors(stderr: str) -> list:
    """Extract structured errors from aiken stderr output."""
    errors = []
    current_error = {}
    for line in stderr.splitlines():
        if line.startswith("error"):
            if current_error:
                errors.append(current_error)
            current_error = {"message": line, "details": []}
        elif current_error:
            current_error["details"].append(line)
    if current_error:
        errors.append(current_error)
    return errors


def _default_memory() -> dict:
    return {
        "contract_purpose": "",
        "design_decisions": [],
        "failed_approaches": [],
        "open_questions": [],
        "last_updated": None
    }


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    # Default project path — override with env var or arg
    project_path = os.environ.get("CARDANO_PROJECT_PATH", "./my_cardano_project")

    if len(sys.argv) > 1:
        user_message = " ".join(sys.argv[1:])
    else:
        print("Cardano Agent — describe what you want to build:")
        user_message = input("> ").strip()

    run_agent(user_message, project_path)
