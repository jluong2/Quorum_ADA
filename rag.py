"""
CardanoRAG — Retrieval-Augmented Generation layer for the Cardano Coding Agent.

Architecture:
  - ChromaDB for persistent vector storage (local, no server required)
  - sentence-transformers for local embeddings (no API key needed)
  - Documents chunked by semantic section (headings + code blocks)
  - Query returns ranked chunks with source metadata

Usage:
    from rag import CardanoRAG

    rag = CardanoRAG(persist_dir=".rag_db")
    rag.ingest_text("## Validators\\n...", source="aiken-tour", title="Validators")
    results = rag.query("how do I write a spend validator?")
    context  = rag.build_context_string("how do I write a spend validator?")
"""

from __future__ import annotations

import hashlib
import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ─────────────────────────────────────────────
# Lazy imports — heavy deps loaded on first use
# ─────────────────────────────────────────────

def _get_chroma_client(persist_dir: str):
    """Return a fresh persistent ChromaDB client for the given directory."""
    try:
        import chromadb
    except ImportError:
        raise ImportError(
            "chromadb is required for RAG. Install with:\n"
            "  pip install chromadb --break-system-packages"
        )
    return chromadb.PersistentClient(path=persist_dir)


def _default_embedding_fn():
    """
    Return the default embedding function (sentence-transformers, all-MiniLM-L6-v2).

    This is the production default.  Pass a different callable to CardanoRAG()
    to override — useful in tests or when a different model is preferred.
    """
    try:
        from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
    except ImportError:
        raise ImportError(
            "sentence-transformers is required for the default embedding function.\n"
            "Install with: pip install sentence-transformers --break-system-packages\n"
            "Or pass a custom embedding_fn to CardanoRAG()."
        )
    return SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")


# ─────────────────────────────────────────────
# Document / Chunk types
# ─────────────────────────────────────────────

@dataclass
class Chunk:
    """A piece of a document with its embedding metadata."""
    id: str                          # stable hash of content
    text: str                        # chunk text sent to embedder
    source: str                      # e.g. "aiken-tour", "stdlib", "example"
    title: str                       # section / page title
    url: str = ""                    # original URL if from web
    chunk_index: int = 0             # position within source doc
    metadata: dict = field(default_factory=dict)


# ─────────────────────────────────────────────
# Chunking
# ─────────────────────────────────────────────

# Targets for chunking
_MAX_CHUNK_CHARS = 1200   # ~300 tokens — keeps chunks focused
_MIN_CHUNK_CHARS = 80     # discard very short noise chunks


def chunk_markdown(text: str, source: str, title: str, url: str = "") -> list[Chunk]:
    """
    Split a markdown document into semantically meaningful chunks.

    Strategy:
    1. Split on heading boundaries (## / ###) to create sections
    2. Within each section, split code fences as separate sub-chunks
       (code and prose have different retrieval patterns)
    3. Merge tiny sections with the previous chunk
    4. Hard-split any remaining oversized chunks at paragraph boundaries
    """
    # Normalise line endings
    text = text.replace("\r\n", "\n").strip()

    # Split into raw sections at heading boundaries
    raw_sections = re.split(r"(?m)^(#{1,3} .+)$", text)

    # re.split with a capturing group: [pre, heading1, body1, heading2, body2, …]
    sections: list[tuple[str, str]] = []
    i = 0
    current_heading = title
    while i < len(raw_sections):
        part = raw_sections[i].strip()
        if re.match(r"^#{1,3} ", part):
            current_heading = part.lstrip("#").strip()
            if i + 1 < len(raw_sections):
                body = raw_sections[i + 1].strip()
                sections.append((current_heading, body))
                i += 2
            else:
                i += 1
        else:
            if part:
                sections.append((current_heading, part))
            i += 1

    if not sections:
        sections = [(title, text)]

    chunks: list[Chunk] = []
    chunk_index = 0

    for section_title, body in sections:
        # Split code fences out as their own sub-chunks
        parts = re.split(r"(```[\s\S]*?```)", body)
        for part in parts:
            part = part.strip()
            if not part:
                continue

            is_code = part.startswith("```")

            # Hard-split oversized prose at paragraph boundaries
            if len(part) > _MAX_CHUNK_CHARS and not is_code:
                sub_parts = part.split("\n\n")
                buffer = ""
                for sub in sub_parts:
                    if len(buffer) + len(sub) < _MAX_CHUNK_CHARS:
                        buffer = (buffer + "\n\n" + sub).strip()
                    else:
                        if len(buffer) >= _MIN_CHUNK_CHARS:
                            chunks.append(_make_chunk(
                                buffer, source, section_title, url, chunk_index
                            ))
                            chunk_index += 1
                        buffer = sub
                if len(buffer) >= _MIN_CHUNK_CHARS:
                    chunks.append(_make_chunk(
                        buffer, source, section_title, url, chunk_index
                    ))
                    chunk_index += 1
            else:
                if len(part) >= _MIN_CHUNK_CHARS:
                    # Prefix code chunks with section for context
                    text_to_embed = (
                        f"[{section_title}]\n{part}" if is_code
                        else part
                    )
                    chunks.append(_make_chunk(
                        text_to_embed, source, section_title, url, chunk_index,
                        metadata={"is_code": is_code}
                    ))
                    chunk_index += 1

    return chunks


def chunk_aiken_file(source_code: str, source: str, title: str, url: str = "") -> list[Chunk]:
    """
    Split an Aiken source file into validator / function / test chunks.

    Each top-level definition becomes its own chunk so the retriever
    can return precise, compilable snippets.
    """
    # Top-level definitions: validator, fn, type, pub type, test
    pattern = re.compile(
        r"(?m)^(?:pub\s+)?(?:type|fn|validator|test)\s+\w+",
    )

    boundaries = [m.start() for m in pattern.finditer(source_code)]
    if not boundaries:
        # Fall back to treating the whole file as one chunk
        return chunk_markdown(f"```aiken\n{source_code}\n```", source, title, url)

    chunks: list[Chunk] = []
    for i, start in enumerate(boundaries):
        end = boundaries[i + 1] if i + 1 < len(boundaries) else len(source_code)
        block = source_code[start:end].strip()
        if len(block) >= _MIN_CHUNK_CHARS:
            text = f"[{title}]\n```aiken\n{block}\n```"
            chunks.append(_make_chunk(text, source, title, url, i, metadata={"is_code": True}))

    return chunks


def _make_chunk(
    text: str,
    source: str,
    title: str,
    url: str,
    index: int,
    metadata: dict | None = None,
) -> Chunk:
    chunk_id = hashlib.sha256(f"{source}:{index}:{text[:200]}".encode()).hexdigest()[:16]
    return Chunk(
        id=chunk_id,
        text=text,
        source=source,
        title=title,
        url=url,
        chunk_index=index,
        metadata=metadata or {},
    )


# ─────────────────────────────────────────────
# CardanoRAG
# ─────────────────────────────────────────────

COLLECTION_NAME = "cardano_docs"


class CardanoRAG:
    """
    RAG layer for the Cardano coding agent.

    Wraps ChromaDB + sentence-transformers with domain-specific
    chunking for Aiken source files and markdown documentation.

    Args:
        persist_dir:   Directory for ChromaDB persistence.
        embedding_fn:  ChromaDB-compatible embedding function.
                       Defaults to SentenceTransformerEmbeddingFunction
                       (all-MiniLM-L6-v2).  Pass a custom callable to
                       override — useful in tests or offline environments.
    """

    def __init__(self, persist_dir: str = ".rag_db", embedding_fn=None):
        self._persist_dir = str(Path(persist_dir).resolve())
        self._embedding_fn = embedding_fn   # None → resolved lazily on first use
        self._client = None
        self._collection = None

    # ── Private ──────────────────────────────

    def _col(self):
        """Return ChromaDB collection, creating it if needed."""
        if self._collection is None:
            if self._client is None:
                self._client = _get_chroma_client(self._persist_dir)
            ef = self._embedding_fn if self._embedding_fn is not None else _default_embedding_fn()
            self._collection = self._client.get_or_create_collection(
                name=COLLECTION_NAME,
                embedding_function=ef,
                metadata={"hnsw:space": "cosine"},
            )
        return self._collection

    # ── Ingestion ────────────────────────────

    def ingest_text(
        self,
        text: str,
        source: str,
        title: str,
        url: str = "",
        is_aiken: bool = False,
    ) -> int:
        """
        Chunk and embed a text document into the vector store.

        Args:
            text:     Raw document text (markdown or Aiken source).
            source:   Category tag, e.g. "aiken-tour", "stdlib", "example".
            title:    Human-readable document title.
            url:      Original URL for citation (optional).
            is_aiken: If True, use Aiken-aware chunking instead of markdown.

        Returns:
            Number of chunks ingested.
        """
        if is_aiken:
            chunks = chunk_aiken_file(text, source, title, url)
        else:
            chunks = chunk_markdown(text, source, title, url)

        if not chunks:
            return 0

        col = self._col()

        # Filter out already-stored IDs (idempotent ingest)
        existing_ids = set(col.get(ids=[c.id for c in chunks])["ids"])
        new_chunks = [c for c in chunks if c.id not in existing_ids]

        if not new_chunks:
            return 0

        col.add(
            ids=[c.id for c in new_chunks],
            documents=[c.text for c in new_chunks],
            metadatas=[{
                "source": c.source,
                "title": c.title,
                "url": c.url,
                "chunk_index": c.chunk_index,
                **c.metadata,
            } for c in new_chunks],
        )

        return len(new_chunks)

    def ingest_file(self, path: str, source: str, title: str = "", url: str = "") -> int:
        """
        Ingest a local file (markdown or .ak Aiken source).

        Args:
            path:   Absolute or relative path to the file.
            source: Category tag for retrieval filtering.
            title:  Override title (defaults to filename stem).
            url:    Optional source URL for citation.

        Returns:
            Number of chunks ingested.
        """
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"File not found: {path}")

        text = p.read_text(encoding="utf-8")
        resolved_title = title or p.stem
        is_aiken = p.suffix == ".ak"
        return self.ingest_text(text, source, resolved_title, url, is_aiken=is_aiken)

    # ── Query ────────────────────────────────

    def query(
        self,
        query_text: str,
        n_results: int = 5,
        source_filter: Optional[str] = None,
    ) -> list[dict]:
        """
        Retrieve the top-k most relevant chunks for a query.

        Args:
            query_text:    Natural language or code query.
            n_results:     Number of results to return.
            source_filter: Restrict to one source category (optional).

        Returns:
            List of dicts with keys: text, source, title, url, score.
            Sorted by relevance (lowest cosine distance = most relevant).
        """
        col = self._col()
        if col.count() == 0:
            return []

        where = {"source": source_filter} if source_filter else None

        results = col.query(
            query_texts=[query_text],
            n_results=min(n_results, col.count()),
            where=where,
            include=["documents", "metadatas", "distances"],
        )

        output = []
        for doc, meta, dist in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        ):
            output.append({
                "text": doc,
                "source": meta.get("source", ""),
                "title": meta.get("title", ""),
                "url": meta.get("url", ""),
                "score": round(1 - dist, 4),   # cosine similarity (higher = better)
            })

        return output

    def build_context_string(
        self,
        query_text: str,
        n_results: int = 5,
        min_score: float = 0.25,
    ) -> str:
        """
        Build a formatted context block for injection into a system prompt.

        Returns an empty string if no relevant docs are found.
        """
        results = self.query(query_text, n_results=n_results)
        relevant = [r for r in results if r["score"] >= min_score]

        if not relevant:
            return ""

        lines = ["## Relevant Aiken / Cardano Reference\n"]
        for i, r in enumerate(relevant, 1):
            header = f"### [{i}] {r['title']}"
            if r["url"]:
                header += f"  \n_Source: {r['url']}_"
            lines.append(header)
            lines.append(r["text"])
            lines.append("")   # blank line between chunks

        return "\n".join(lines)

    # ── Utility ──────────────────────────────

    def count(self) -> int:
        """Return the total number of chunks stored."""
        return self._col().count()

    def clear(self) -> None:
        """Delete all stored chunks (useful for re-ingestion)."""
        if self._client is None:
            self._client = _get_chroma_client(self._persist_dir)
        self._client.delete_collection(COLLECTION_NAME)
        self._collection = None

    def stats(self) -> dict:
        """Return ingestion statistics grouped by source."""
        col = self._col()
        total = col.count()
        if total == 0:
            return {"total_chunks": 0, "by_source": {}}

        all_meta = col.get(include=["metadatas"])["metadatas"]
        by_source: dict[str, int] = {}
        for m in all_meta:
            src = m.get("source", "unknown")
            by_source[src] = by_source.get(src, 0) + 1

        return {"total_chunks": total, "by_source": by_source}
