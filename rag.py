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

# Keep torch single-threaded. Each worker thread allocates its own buffers, and
# on a small instance that headroom is needed for request handling — the app was
# being OOM-killed. Indexing a handful of FAQ documents doesn't need the
# parallelism anyway.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import torch

torch.set_num_threads(1)

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


def _load_index(tenant):
    """Load one tenant's index, rebuilding it from the database if absent.

    The index is a disk cache, not the source of truth. Free hosting wipes the
    container's filesystem on every deploy, so the first search after a deploy
    finds nothing on disk and regenerates it from the stored documents.
    """
    slug = tenant["slug"]
    if slug in _indexes:
        return _indexes[slug]

    path = ingest.index_path(slug)
    if not os.path.exists(path):
        import db
        if not db.get_documents(tenant["id"]):
            return None  # workspace genuinely has no documents yet
        print(f"[rag] rebuilding index for {slug} from the database")
        if not ingest.build_index(tenant["id"], slug):
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
    """Raises RuntimeError, not SystemExit, if the key is missing.

    SystemExit is a BaseException, not an Exception — it skips ordinary
    try/except blocks entirely. Raised inside a request that matters: under
    gunicorn's sync worker, an uncaught SystemExit kills the whole worker
    process, not just that request. With a single worker (the memory budget
    here rules out more), every subsequent request fails until the master
    respawns it — and if the key is still missing, the next chat request
    kills the replacement too. One bad request becomes a permanent crash loop
    that takes down the entire app, not just answers. Confirmed by testing
    under gunicorn directly, not assumed.
    """
    global _groq_client
    if _groq_client is None:
        from groq import Groq
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY not set. Add it to your .env file.")
        _groq_client = Groq(api_key=api_key)
    return _groq_client


def search(tenant, question, top_k=TOP_K):
    loaded = _load_index(tenant)
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


SHARED_RULES = (
    "Rules:\n"
    "- Only say hello if the user actually greeted you. Never open with 'Hello' or "
    "'How are you today' otherwise.\n"
    "- Do not assume the time of day (no 'good morning'), the user's mood, or that "
    "they have already ordered or been helped.\n"
    "- Keep every reply under 30 words. Do not repeat the same closing line every time.\n"
)

# Asking the model to label its own outcome beats guessing from its wording,
# which varies every run. The options are branch-specific on purpose: listing
# ANSWERED when no context exists made the model pick it every time.
MARKER_WITH_CONTEXT = (
    "After your reply, on a new line, write exactly ONE of these words:\n"
    "  ANSWERED  - you answered using the context above\n"
    "  NOANSWER  - a question about this business the context did not answer\n"
    "  OFFTOPIC  - the message had nothing to do with this business\n"
    "  CHAT      - a greeting, pleasantry, complaint or insult\n"
)

MARKER_NO_CONTEXT = ""  # classified separately — see _classify_message()


# Asking this model to answer *and* categorise in one call proved unreliable:
# it anchored on whichever label was listed first rather than on meaning
# (measured 5/11 wrong). A separate call doing nothing but classification is
# accurate, and it only runs when retrieval found nothing — so the common,
# answerable path still costs a single request.
CLASSIFY_PROMPT = """Classify this customer message for {company}.

Reply with ONE word only:

CHAT - a greeting, thanks, goodbye, small talk, a complaint, or an insult.
  Examples: "hi", "thanks", "how are you", "bye", "you are useless", "idiot"

OFFTOPIC - asks about something unrelated to {company}: general knowledge,
  trivia, other companies, personal or financial advice.
  Examples: "what is the capital of France", "should I invest in bitcoin",
  "tell me a joke", "what's the weather"

QUESTION - asks about {company} itself: its products, prices, hours, location,
  policies, bookings or services.
  Examples: "do you cater weddings", "is there parking", "what time do you open",
  "do you have vegan options"

Message: {message}

One word:"""


def _classify_message(client, question, company_name):
    """CHAT / OFFTOPIC / QUESTION for messages retrieval couldn't answer."""
    try:
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{
                "role": "user",
                "content": CLASSIFY_PROMPT.format(company=company_name, message=question),
            }],
            temperature=0,
            max_tokens=5,
        )
        label = response.choices[0].message.content.strip().upper()
        for known in ("CHAT", "OFFTOPIC", "QUESTION"):
            if known in label:
                return known
    except Exception as exc:
        print(f"[rag] classify failed: {type(exc).__name__}: {exc}")
    # Unsure? Treat it as small talk. Logging a false gap is worse than missing
    # one — a dashboard full of "hi" is what makes the list useless.
    return "CHAT"


def build_prompt(question, results, company_name, contact, has_context):
    """Two prompts, chosen by whether retrieval actually found anything.

    They are kept separate on purpose. A single prompt that merely *mentions*
    no documents were found still invites the model to be helpful from its own
    knowledge — it confidently invented a gluten-free menu option that appears
    nowhere in the documents. When there is no context the model is given no
    room to answer at all.
    """
    fallback = (
        f"tell them to contact {contact}" if contact
        else "tell them to contact support directly"
    )

    if not has_context:
        return (
            f"You are the customer-support assistant for {company_name}.\n\n"
            f"You have NO information about {company_name} for this message. You know "
            f"nothing about their products, prices, hours, policies or services.\n\n"
            "You may reply in only these ways:\n"
            "1. GREETING ('hi'): greet back in one short sentence and ask how you can help.\n"
            "2. PLEASANTRY ('how are you', 'thanks', 'bye', 'who are you'): answer warmly "
            "in one sentence. Do not greet them again.\n"
            "3. FRUSTRATION OR INSULT: acknowledge it calmly in one sentence without "
            f"taking offence, then {fallback} if they need a person. Never argue back.\n"
            f"3b. ASKING FOR A PERSON ('talk to a human', 'speak to someone', 'contact "
            f"support'): always {fallback}. Never brush this off.\n"
            f"4. ANY QUESTION unrelated to {company_name} (trivia, opinions, advice): say "
            f"you can only help with {company_name} questions. Never answer it, even "
            "though you know the answer.\n"
            f"5. ANY OTHER QUESTION: say you don't have that information and {fallback}.\n\n"
            f"CRITICAL: you must NEVER state a fact about {company_name} — never confirm "
            "or deny that they offer something, never give a price, time, or policy. "
            "Saying 'yes we offer that' or 'no we don't do that' is forbidden. If you are "
            "not replying to a greeting, pleasantry or insult, use case 4 or 5.\n\n"
            f"{SHARED_RULES}\n"
            f"{MARKER_NO_CONTEXT}\n"
            f"User: {question}\n"
            "Assistant:"
        )

    context = "\n\n".join(f"[{r['source']}]\n{r['text']}" for r in results)
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
        f"don't have that information and {fallback}. Do not fill the gap from your own "
        "knowledge.\n"
        "5. FRUSTRATION, INSULT, OR COMPLAINT ('this is useless', 'idiot', 'stupid'): "
        "do not greet them and do not take offence. Acknowledge it calmly in one "
        f"sentence, then {fallback} if they need a person. Never argue back.\n"
        f"5b. ASKING FOR A PERSON ('talk to a human', 'speak to someone', 'contact "
        f"support'): always {fallback}. Never brush this off.\n"
        f"6. ANYTHING ELSE unrelated to {company_name} (trivia, opinions, advice, other "
        "companies): do not answer it and do not give an opinion, even if you know the "
        f"answer. Say you can only help with {company_name} questions.\n\n"
        f"CRITICAL: every fact you state about {company_name} must appear verbatim in the "
        "context below. Never confirm or deny that they offer something unless the "
        "context says so.\n\n"
        f"{SHARED_RULES}\n"
        f"Context from {company_name}'s documents:\n{context}\n\n"
        f"{MARKER_WITH_CONTEXT}\n"
        f"User: {question}\n"
        "Assistant:"
    )


def _complete_with_retry(client, prompt, attempts=3):
    """Call the LLM, retrying briefly on rate limits.

    Groq's free tier caps tokens per minute, and a burst of traffic across
    tenants hits it easily. Most of those are transient, so a couple of short
    backoffs recover silently instead of showing the customer an error.
    """
    import time

    # Keep the total added wait under ~5s — someone is staring at a chat box.
    delay = 1.5
    for attempt in range(attempts):
        try:
            return client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[{"role": "user", "content": prompt}],
                # 0 = same question gives the same answer. Support answers
                # should be consistent, and it makes eval.py reproducible.
                temperature=0,
                max_tokens=300,
            )
        except Exception as exc:
            retryable = "rate_limit" in str(exc).lower() or "429" in str(exc)
            if not retryable or attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay *= 2


def ask(question, tenant):
    """tenant is a row from db.tenants (dict) — everything is scoped to it."""
    import db

    slug = tenant["slug"]
    company_name = tenant.get("company_name") or "this business"
    contact = db.support_contact_line(tenant)

    results = search(tenant, question)
    has_context = bool(results) and results[0]["score"] >= MIN_SIMILARITY

    prompt = build_prompt(
        question,
        results if has_context else [],
        company_name,
        contact,
        has_context,
    )
    try:
        client = _get_groq_client()
        response = _complete_with_retry(client, prompt)
    except Exception as exc:
        # The LLM provider is rate-limited, down, or misconfigured (missing/bad
        # API key). A customer should get a human to talk to, not a 500 page —
        # and the app must survive this, not crash the worker process.
        print(f"[rag] LLM call failed for {slug}: {type(exc).__name__}: {exc}")
        answer = "Sorry — I can't answer right now, please try again in a moment."
        if contact:
            answer += f" If it's urgent, contact {contact}."
        # An outage isn't a documentation gap, so don't log it as one.
        return {"answer": answer, "sources": [], "outcome": "ERROR", "answered": True}

    raw = response.choices[0].message.content.strip()
    answer, outcome = _split_outcome(raw)

    if has_context:
        # Cite a document only when the model says it answered from the context —
        # refusals and small talk must never carry a citation.
        if outcome == "ANSWERED":
            return {"answer": answer, "sources": sorted({r["source"] for r in results}),
                    "outcome": "ANSWERED", "answered": True}
        # Context existed but didn't cover the question: that's a real gap.
        return {"answer": answer, "sources": [], "outcome": "NOANSWER", "answered": False}

    # Nothing was retrieved, so work out what kind of message this actually was.
    kind = _classify_message(client, question, company_name)
    is_gap = kind == "QUESTION"
    return {"answer": answer, "sources": [],
            "outcome": "NOANSWER" if is_gap else kind,
            "answered": not is_gap}


OUTCOMES = ("ANSWERED", "NOANSWER", "OFFTOPIC", "CHAT")


def _split_outcome(raw):
    """Strip the trailing outcome label off the model's reply.

    Returns (clean_answer, outcome). If the label is missing we fall back to
    CHAT, which neither cites a document nor logs a gap — the safe default.
    """
    lines = raw.rstrip().splitlines()
    if not lines:
        return raw, "CHAT"

    label = lines[-1].strip().strip("*_`[]() .:-").upper()
    if label in OUTCOMES:
        return "\n".join(lines[:-1]).strip(), label
    return raw, "CHAT"


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
