"""Small RAG over synthetic DTC / service guidance docs: hashed embeddings + FAISS."""

import logging

for _name in ("faiss", "faiss.loader"):
    logging.getLogger(_name).setLevel(logging.WARNING)

from haulmind.rag.index import SearchHit, build_index, ensure_index, get_index, search  # noqa: E402

__all__ = ["SearchHit", "build_index", "ensure_index", "get_index", "search"]
