"""
Builds a FAISS index per tenant.

Each business gets its own directory of documents and its own index, so one
tenant's search can never surface another tenant's content:

    documents/<slug>/*.txt   ->   data/<slug>/index.faiss + chunks.pkl

CLI (rebuild one tenant):
    python ingest.py <slug>
"""

import os
import pickle
import sys

import faiss
import numpy as np

# see the note in rag.py — keep torch single-threaded to stay inside the
# memory budget of a small instance
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import torch

torch.set_num_threads(1)

from sentence_transformers import SentenceTransformer

import db

DOCS_ROOT = "documents"
DATA_ROOT = "data"
EMBED_MODEL = "all-MiniLM-L6-v2"

CHUNK_SIZE = 600       # max characters per chunk

_model = None


def _get_model():
    global _model
    if _model is None:
        _model = SentenceTransformer(EMBED_MODEL)
    return _model


def _safe_slug(slug):
    """Never let a slug reach the filesystem without validation."""
    if not db.SLUG_RE.match(slug or ""):
        raise ValueError(f"Invalid workspace address: {slug!r}")
    return slug


def docs_dir(slug):
    path = os.path.join(DOCS_ROOT, _safe_slug(slug))
    os.makedirs(path, exist_ok=True)
    return path


def data_dir(slug):
    path = os.path.join(DATA_ROOT, _safe_slug(slug))
    os.makedirs(path, exist_ok=True)
    return path


def index_path(slug):
    return os.path.join(data_dir(slug), "index.faiss")


def chunks_path(slug):
    return os.path.join(data_dir(slug), "chunks.pkl")


def list_documents(slug):
    return sorted(f for f in os.listdir(docs_dir(slug)) if f.endswith(".txt"))


def load_documents(slug):
    docs = []
    directory = docs_dir(slug)
    for filename in list_documents(slug):
        with open(os.path.join(directory, filename), "r", encoding="utf-8") as f:
            docs.append((filename, f.read()))
    return docs


def chunk_text(text, max_size=CHUNK_SIZE):
    """Merge blank-line-separated paragraphs into chunks, keeping each FAQ
    section intact instead of cutting it apart at a fixed character count."""
    paragraphs = [p.strip() for p in text.strip().split("\n\n") if p.strip()]
    chunks = []
    current = ""
    for p in paragraphs:
        candidate = f"{current}\n\n{p}".strip() if current else p
        if len(candidate) > max_size and current:
            chunks.append(current)
            current = p
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def build_index(slug, verbose=False):
    docs = load_documents(slug)
    if not docs:
        # No documents yet (fresh workspace) — clear any stale index.
        for path in (index_path(slug), chunks_path(slug)):
            if os.path.exists(path):
                os.remove(path)
        return 0

    chunks = []  # list of {"text": str, "source": filename}
    for filename, text in docs:
        for chunk in chunk_text(text):
            chunks.append({"text": chunk, "source": filename})

    model = _get_model()
    embeddings = model.encode([c["text"] for c in chunks], show_progress_bar=verbose)
    embeddings = np.array(embeddings, dtype="float32")

    # normalize so inner-product search behaves like cosine similarity
    faiss.normalize_L2(embeddings)

    index = faiss.IndexFlatIP(embeddings.shape[1])
    index.add(embeddings)

    faiss.write_index(index, index_path(slug))
    with open(chunks_path(slug), "wb") as f:
        pickle.dump(chunks, f)

    if verbose:
        print(f"[{slug}] {len(docs)} document(s), {len(chunks)} chunk(s) indexed.")
    return len(chunks)


if __name__ == "__main__":
    db.init_db()
    if len(sys.argv) > 1:
        build_index(sys.argv[1], verbose=True)
    else:
        raise SystemExit("Usage: python ingest.py <workspace-slug>")
