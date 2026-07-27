"""
Reads every .txt file in documents/, splits into chunks, embeds them,
and builds a FAISS index for fast similarity search.

Run this once whenever documents/ changes:
    python ingest.py
"""

import os
import pickle

import faiss
import numpy as np
from sentence_transformers import SentenceTransformer

DOCS_DIR = "documents"
INDEX_PATH = "data/index.faiss"
CHUNKS_PATH = "data/chunks.pkl"
EMBED_MODEL = "all-MiniLM-L6-v2"

CHUNK_SIZE = 500       # characters per chunk
CHUNK_OVERLAP = 50     # characters shared between consecutive chunks


def load_documents():
    docs = []
    for filename in sorted(os.listdir(DOCS_DIR)):
        if not filename.endswith(".txt"):
            continue
        path = os.path.join(DOCS_DIR, filename)
        with open(path, "r", encoding="utf-8") as f:
            docs.append((filename, f.read()))
    return docs


def chunk_text(text, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    chunks = []
    start = 0
    text = text.strip()
    while start < len(text):
        end = start + size
        chunks.append(text[start:end].strip())
        start += size - overlap
    return [c for c in chunks if c]


def build_index():
    docs = load_documents()
    if not docs:
        raise SystemExit(f"No .txt files found in {DOCS_DIR}/. Add at least one document first.")

    chunks = []  # list of {"text": str, "source": filename}
    for filename, text in docs:
        for chunk in chunk_text(text):
            chunks.append({"text": chunk, "source": filename})

    print(f"Loaded {len(docs)} document(s), split into {len(chunks)} chunk(s).")

    model = SentenceTransformer(EMBED_MODEL)
    embeddings = model.encode([c["text"] for c in chunks], show_progress_bar=True)
    embeddings = np.array(embeddings, dtype="float32")

    # normalize so inner-product search behaves like cosine similarity
    faiss.normalize_L2(embeddings)

    index = faiss.IndexFlatIP(embeddings.shape[1])
    index.add(embeddings)

    os.makedirs("data", exist_ok=True)
    faiss.write_index(index, INDEX_PATH)
    with open(CHUNKS_PATH, "wb") as f:
        pickle.dump(chunks, f)

    print(f"Index built: {INDEX_PATH} ({index.ntotal} vectors)")


if __name__ == "__main__":
    build_index()
