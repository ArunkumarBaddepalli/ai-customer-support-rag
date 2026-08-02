"""
Core RAG logic, scoped to one tenant at a time.

ask(question, tenant) embeds the question, searches only that tenant's FAISS
index, and asks the LLM to answer from the retrieved context — or to handle
small talk, abuse, and off-topic messages without inventing business facts.
"""

import os
import pickle

import faiss
import numpy as np
from dotenv import load_dotenv

load_dotenv()

from sentence_transformers import SentenceTransformer

import ingest

EMBED_MODEL = "all-MiniLM-L6-v2"
GROQ_MODEL = "llama-3.1-8b-instant"

TOP_K = 4
# cosine similarity below this = no usable context, so the LLM is told to answer
# without inventing business facts (small talk is fine, made-up prices are not)
MIN_SIMILARITY = 0.20

_embedder = None
_groq_client = None
_indexes = {}  # slug -> (faiss index, chunks)


def _get_embedder():
    global _embedder
    if _embedder is None:
        _embedder = SentenceTransformer(EMBED_MODEL)
    return _embedder


def _load_index(slug):
    """Load (and cache) one tenant's index. Returns None if they have no docs."""
    if slug in _indexes:
        return _indexes[slug]

    path = ingest.index_path(slug)
    if not os.path.exists(path):
        return None

    index = faiss.read_index(path)
    with open(ingest.chunks_path(slug), "rb") as f:
        chunks = pickle.load(f)
    _indexes[slug] = (index, chunks)
    return _indexes[slug]


def reload_index(slug):
    """Drop the cached index so the next search re-reads it from disk."""
    _indexes.pop(slug, None)


def _get_groq_client():
    global _groq_client
    if _groq_client is None:
        from groq import Groq
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise SystemExit("GROQ_API_KEY not set. Add it to your .env file.")
        _groq_client = Groq(api_key=api_key)
    return _groq_client


def search(slug, question, top_k=TOP_K):
    loaded = _load_index(slug)
    if loaded is None:
        return []
    index, chunks = loaded

    query_vec = _get_embedder().encode([question])
    query_vec = np.array(query_vec, dtype="float32")
    faiss.normalize_L2(query_vec)

    scores, indices = index.search(query_vec, min(top_k, index.ntotal))
    results = []
    for score, idx in zip(scores[0], indices[0]):
        if idx == -1:
            continue
        chunk = chunks[idx]
        results.append({"text": chunk["text"], "source": chunk["source"], "score": float(score)})
    return results


def build_prompt(question, results, company_name, contact, has_context):
    """One prompt handles every kind of message. The LLM decides which case
    applies — no hardcoded keyword lists, so it copes with phrasings we never
    thought of ('how are you', 'this is useless', 'thanks!')."""
    if has_context:
        context = "\n\n".join(f"[{r['source']}]\n{r['text']}" for r in results)
        context_block = f"Context from {company_name}'s documents:\n{context}"
    else:
        context_block = "No relevant company documents matched this message."

    fallback = f"tell them to contact {contact}" if contact else "tell them to contact support directly"

    return (
        f"You are the customer-support assistant for {company_name}.\n\n"
        "Match the user's message to ONE case and reply accordingly:\n"
        "1. GREETING ('hi', 'good morning'): greet back in one short sentence and ask "
        "how you can help.\n"
        "2. PLEASANTRY ('how are you', 'thanks', 'bye', 'who are you', 'what can you "
        "do'): answer it warmly in one sentence. Do not greet them again.\n"
        f"3. QUESTION ABOUT {company_name.upper()} answered by the context below: answer "
        "using ONLY that context. Short and direct.\n"
        f"4. QUESTION ABOUT {company_name.upper()} the context does not answer: say you "
        f"don't have that information and {fallback}.\n"
        "5. FRUSTRATION, INSULT, OR COMPLAINT ('this is useless', 'idiot', 'stupid'): "
        "do not greet them and do not take offence. Acknowledge it calmly in one "
        f"sentence, then {fallback} if they need a person. Never argue back.\n"
        f"6. ANYTHING ELSE unrelated to {company_name} (trivia, opinions, advice, other "
        "companies): do not answer it and do not give an opinion, even if you know the "
        f"answer. Say you can only help with {company_name} questions.\n\n"
        "Rules:\n"
        "- Only say hello if the user actually greeted you. Never open with 'Hello' or "
        "'How are you today' otherwise.\n"
        "- Never invent prices, timings, policies, or contact details, and never claim "
        f"what {company_name} does or doesn't offer unless the context says so.\n"
        "- Do not assume the time of day (no 'good morning'), the user's mood, or that "
        "they have already ordered or been helped.\n"
        "- Keep every reply under 30 words. Do not repeat the same closing line every "
        "time.\n\n"
        f"{context_block}\n\n"
        "After your reply, on a new line, write SOURCED if you used the context above "
        "to answer, or NOSOURCE if you did not.\n\n"
        f"User: {question}\n"
        "Assistant:"
    )


def ask(question, tenant):
    """tenant is a row from db.tenants (dict) — everything is scoped to it."""
    slug = tenant["slug"]
    company_name = tenant.get("company_name") or "this business"
    contact = (tenant.get("support_contact") or "").strip()

    results = search(slug, question)
    has_context = bool(results) and results[0]["score"] >= MIN_SIMILARITY

    prompt = build_prompt(
        question,
        results if has_context else [],
        company_name,
        contact,
        has_context,
    )
    client = _get_groq_client()
    response = client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role": "user", "content": prompt}],
        # 0 = same question gives the same answer. Support answers should be
        # consistent, and it makes eval.py reproducible instead of flaky.
        temperature=0,
        max_tokens=300,
    )

    raw = response.choices[0].message.content.strip()
    answer, used_context = _split_source_marker(raw)

    # Only credit a document when retrieval found one AND the model says it
    # actually used it — otherwise small talk and refusals get a bogus citation.
    if has_context and used_context:
        return {"answer": answer, "sources": sorted({r["source"] for r in results})}
    return {"answer": answer, "sources": []}


def _split_source_marker(raw):
    """Strip the trailing SOURCED / NOSOURCE marker off the model's reply.

    Returns (clean_answer, used_context). If the model forgot the marker we fall
    back to False so we under-cite rather than cite the wrong document."""
    lines = raw.rstrip().splitlines()
    if not lines:
        return raw, False

    marker = lines[-1].strip().strip("*_`[]() .").upper()
    if marker in ("SOURCED", "NOSOURCE"):
        return "\n".join(lines[:-1]).strip(), marker == "SOURCED"
    return raw, False


if __name__ == "__main__":
    import sys

    import db

    db.init_db()
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python rag.py <workspace-slug>")

    tenant = db.get_tenant_by_slug(sys.argv[1])
    if not tenant:
        raise SystemExit(f"No workspace found with slug {sys.argv[1]!r}")

    print(f"Chatting with {tenant['company_name']}. Type 'quit' to exit.\n")
    while True:
        question = input("You: ").strip()
        if question.lower() in ("quit", "exit"):
            break
        if not question:
            continue
        result = ask(question, tenant)
        print(f"\nBot: {result['answer']}")
        if result["sources"]:
            print(f"(source: {', '.join(result['sources'])})")
        print()
