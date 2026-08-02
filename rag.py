"""
Core RAG logic: embed a question, search FAISS for the closest chunks,
send question + chunks to the LLM (Groq), return the answer and its sources.

Terminal test:
    python rag.py
"""

import os
import pickle

import faiss
import numpy as np
from dotenv import load_dotenv
from sentence_transformers import SentenceTransformer

load_dotenv()

INDEX_PATH = "data/index.faiss"
CHUNKS_PATH = "data/chunks.pkl"
EMBED_MODEL = "all-MiniLM-L6-v2"
GROQ_MODEL = "llama-3.1-8b-instant"

TOP_K = 4
# cosine similarity below this = "I don't know" instead of a guess
MIN_SIMILARITY = 0.20

_embedder = None
_index = None
_chunks = None
_groq_client = None


def _load():
    global _embedder, _index, _chunks
    if _embedder is None:
        _embedder = SentenceTransformer(EMBED_MODEL)
    if _index is None:
        if not os.path.exists(INDEX_PATH):
            raise SystemExit("No index found. Run `python ingest.py` first.")
        _index = faiss.read_index(INDEX_PATH)
        with open(CHUNKS_PATH, "rb") as f:
            _chunks = pickle.load(f)


def reload_index():
    """Force the next search() to re-read the index from disk (call after re-ingesting)."""
    global _index, _chunks
    _index = None
    _chunks = None


def _get_groq_client():
    global _groq_client
    if _groq_client is None:
        from groq import Groq
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise SystemExit("GROQ_API_KEY not set. Add it to your .env file.")
        _groq_client = Groq(api_key=api_key)
    return _groq_client


def search(question, top_k=TOP_K):
    _load()
    query_vec = _embedder.encode([question])
    query_vec = np.array(query_vec, dtype="float32")
    faiss.normalize_L2(query_vec)

    scores, indices = _index.search(query_vec, top_k)
    results = []
    for score, idx in zip(scores[0], indices[0]):
        if idx == -1:
            continue
        chunk = _chunks[idx]
        results.append({"text": chunk["text"], "source": chunk["source"], "score": float(score)})
    return results


def build_prompt(question, results):
    context = "\n\n".join(f"[{r['source']}]\n{r['text']}" for r in results)
    return (
        "You are a customer support assistant. Answer the question using ONLY the "
        "context below. If the context does not contain the answer, say you don't "
        "know instead of guessing. Keep the answer short and direct.\n\n"
        f"Context:\n{context}\n\n"
        f"Question: {question}\n"
        "Answer:"
    )


def ask(question):
    results = search(question)

    if not results or results[0]["score"] < MIN_SIMILARITY:
        return {
            "answer": "I'm not sure about that based on the information I have. "
                      "Please contact support directly for help with this.",
            "sources": [],
        }

    prompt = build_prompt(question, results)
    client = _get_groq_client()
    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=300,
    )
    answer = response.choices[0].message.content.strip()

    sources = sorted({r["source"] for r in results})
    return {"answer": answer, "sources": sources}


if __name__ == "__main__":
    print("RAG chatbot (terminal mode). Type 'quit' to exit.\n")
    while True:
        question = input("You: ").strip()
        if question.lower() in ("quit", "exit"):
            break
        if not question:
            continue
        result = ask(question)
        print(f"\nBot: {result['answer']}")
        if result["sources"]:
            print(f"(source: {', '.join(result['sources'])})")
        print()
