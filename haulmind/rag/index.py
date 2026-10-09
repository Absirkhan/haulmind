"""Build, cache and query the FAISS index over the corpus in ``haulmind/rag/corpus``."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

import faiss
import numpy as np
from pydantic import BaseModel

from haulmind.config import get_settings
from haulmind.logging_setup import get_logger
from haulmind.rag.embeddings import HashedEmbedder

log = get_logger(__name__)

CORPUS_DIR = Path(__file__).parent / "corpus"
HEADER_RE = re.compile(r"^(codes|system|tags):\s*(.*)$", re.IGNORECASE | re.MULTILINE)


class Chunk(BaseModel):
    id: str
    doc: str
    title: str
    section: str
    text: str
    codes: list[str]
    system: str
    source: str


class SearchHit(BaseModel):
    chunk: Chunk
    score: float

    @property
    def citation(self) -> str:
        return f"{self.chunk.source}#{_slug(self.chunk.section)}"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def load_corpus(corpus_dir: Path = CORPUS_DIR) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in sorted(corpus_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        title_match = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else path.stem
        codes: list[str] = []
        system = ""
        for key, val in HEADER_RE.findall(text):
            if key.lower() == "codes":
                codes = [c.strip() for c in val.split(",") if c.strip()]
            elif key.lower() == "system":
                system = val.strip()
        body = HEADER_RE.sub("", text)
        parts = re.split(r"^##\s+", body, flags=re.MULTILINE)
        intro = parts[0]
        intro_body = re.sub(r"^#\s+.+$", "", intro, flags=re.MULTILINE).strip()
        sections: list[tuple[str, str]] = []
        if intro_body:
            sections.append(("Overview", intro_body))
        for part in parts[1:]:
            heading, _, sec_body = part.partition("\n")
            sections.append((heading.strip(), sec_body.strip()))
        for i, (heading, sec_body) in enumerate(sections):
            if not sec_body:
                continue
            chunks.append(
                Chunk(
                    id=f"{path.stem}#{i}",
                    doc=path.stem,
                    title=title,
                    section=heading,
                    text=f"{title} — {heading}\n{sec_body}",
                    codes=codes,
                    system=system,
                    source=path.name,
                )
            )
    return chunks


@dataclass
class RagIndex:
    index: faiss.Index
    chunks: list[Chunk]
    embedder: HashedEmbedder

    def search(self, query: str, k: int = 3, codes: list[str] | None = None) -> list[SearchHit]:
        if not self.chunks:
            return []
        q = self.embedder.embed([query])
        n = min(len(self.chunks), max(k * 6, 12))
        scores, ids = self.index.search(q, n)
        hits: list[SearchHit] = []
        wanted = {c.upper() for c in (codes or [])}
        for score, idx in zip(scores[0].tolist(), ids[0].tolist(), strict=False):
            if idx < 0:
                continue
            ch = self.chunks[idx]
            boost = 0.35 if wanted and wanted & {c.upper() for c in ch.codes} else 0.0
            hits.append(SearchHit(chunk=ch, score=float(score) + boost))
        hits.sort(key=lambda h: h.score, reverse=True)
        # at most two chunks from the same doc so results span sources
        out: list[SearchHit] = []
        per_doc: dict[str, int] = {}
        for h in hits:
            if per_doc.get(h.chunk.doc, 0) >= 2:
                continue
            per_doc[h.chunk.doc] = per_doc.get(h.chunk.doc, 0) + 1
            out.append(h)
            if len(out) >= k:
                break
        return out


_INDEX: RagIndex | None = None


def _paths() -> tuple[Path, Path, Path]:
    d = get_settings().faiss_dir
    return d / "index.faiss", d / "chunks.json", d / "embedder.json"


def corpus_fingerprint(corpus_dir: Path = CORPUS_DIR) -> str:
    h = hashlib.sha256()
    for path in sorted(corpus_dir.glob("*.md")):
        h.update(path.name.encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def _stored_fingerprint() -> str | None:
    _, _, emb_path = _paths()
    if not emb_path.exists():
        return None
    try:
        data = json.loads(emb_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    fp = data.get("fingerprint")
    return str(fp) if fp else None


def build_index(force: bool = False, corpus_dir: Path = CORPUS_DIR) -> RagIndex:
    """Build the index from the corpus and cache it to disk (rebuilds if the corpus changed)."""
    global _INDEX
    idx_path, chunks_path, emb_path = _paths()
    fresh = _stored_fingerprint() == corpus_fingerprint(corpus_dir)
    if not force and fresh and idx_path.exists() and chunks_path.exists():
        return get_index()
    chunks = load_corpus(corpus_dir)
    embedder = HashedEmbedder.fit((c.text for c in chunks), dim=512)
    vectors = embedder.embed([c.text for c in chunks]) if chunks else np.zeros((0, 512), np.float32)
    index = faiss.IndexFlatIP(embedder.dim)
    if len(chunks):
        index.add(vectors)
    idx_path.parent.mkdir(parents=True, exist_ok=True)
    faiss.write_index(index, str(idx_path))
    chunks_path.write_text(
        json.dumps([c.model_dump() for c in chunks], ensure_ascii=False), encoding="utf-8"
    )
    emb_path.write_text(
        json.dumps({**embedder.to_dict(), "fingerprint": corpus_fingerprint(corpus_dir)}),
        encoding="utf-8",
    )
    _INDEX = RagIndex(index=index, chunks=chunks, embedder=embedder)
    log.info("faiss index built", chunks=len(chunks), dim=embedder.dim, path=str(idx_path))
    return _INDEX


def get_index() -> RagIndex:
    """Load the cached index (building it if missing). Cached in-process."""
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    idx_path, chunks_path, emb_path = _paths()
    if not (idx_path.exists() and chunks_path.exists() and emb_path.exists()):
        return build_index(force=True)
    if _stored_fingerprint() != corpus_fingerprint():
        return build_index(force=True)
    index = faiss.read_index(str(idx_path))
    chunks = [Chunk.model_validate(c) for c in json.loads(chunks_path.read_text(encoding="utf-8"))]
    embedder = HashedEmbedder.from_dict(json.loads(emb_path.read_text(encoding="utf-8")))
    _INDEX = RagIndex(index=index, chunks=chunks, embedder=embedder)
    return _INDEX


def ensure_index() -> RagIndex:
    return get_index()


def reset_index_cache() -> None:
    global _INDEX
    _INDEX = None


def search(query: str, k: int = 3, codes: list[str] | None = None) -> list[SearchHit]:
    return get_index().search(query, k=k, codes=codes)
