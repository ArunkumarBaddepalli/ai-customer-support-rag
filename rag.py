"""
Core RAG logic, scoped to one tenant at a time.

ask(question, tenant) embeds the question, searches only that tenant's FAISS
index, and asks the LLM to answer from the retrieved context — or to handle
small talk, abuse, and off-topic messages without inventing business facts.
"""

import os
import pickle
import threading
from collections import OrderedDict

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
# Generous enough for a slow-but-working completion, short enough that a hung
# one frees its thread well inside gunicorn's 120s request timeout.
LLM_TIMEOUT_SECONDS = 20.0
# Ceiling on a single retry wait. Someone is staring at a chat box, so a server
# asking us to wait a minute is a request to fail fast, not to hold the thread.
MAX_RETRY_WAIT_SECONDS = 8.0
# cosine similarity below this = no usable context, so the LLM is told to answer
# without inventing business facts (small talk is fine, made-up prices are not)
MIN_SIMILARITY = 0.20

_embedder = None
_groq_client = None

# slug -> (faiss index, chunks, index_version). An OrderedDict used as an LRU:
# each entry holds a FAISS index plus every chunk's text, and nothing used to
# evict, so memory grew with the tenant count on an instance already sized
# tightly around the embedding model.
MAX_CACHED_INDEXES = 20
_indexes = OrderedDict()

# Guards the dict itself. Held only for dict operations, never across a rebuild.
_cache_lock = threading.Lock()

# One lock per tenant, so a cold rebuild for one workspace does not block chat
# requests for every other workspace. Without any lock, two threads asking the
# same fresh tenant a question both missed the cache, both rebuilt, and one
# wrote index.faiss while the other was reading it — a corrupt read, in exactly
# the first-request-after-deploy moment when it is most likely.
_build_locks = {}


def _get_embedder():
    global _embedder
    if _embedder is None:
        _embedder = SentenceTransformer(EMBED_MODEL)
    return _embedder


def _lock_for(slug):
    with _cache_lock:
        return _build_locks.setdefault(slug, threading.Lock())


def _cached(slug, version):
    """The cached entry if it is present and current, else None."""
    with _cache_lock:
        entry = _indexes.get(slug)
        if entry is None or entry[2] != version:
            return None
        _indexes.move_to_end(slug)      # most recently used
        return entry[0], entry[1]


def _remember(slug, index, chunks, version):
    with _cache_lock:
        _indexes[slug] = (index, chunks, version)
        _indexes.move_to_end(slug)
        while len(_indexes) > MAX_CACHED_INDEXES:
            _indexes.popitem(last=False)    # evict the least recently used


def _read_from_disk(tenant):
    """Read this tenant's index off disk, building it first if it isn't there."""
    slug = tenant["slug"]
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
    return index, chunks


def _load_index(tenant):
    """Load one tenant's index, rebuilding it from the database if absent.

    The index is a disk cache, not the source of truth. Free hosting wipes the
    container's filesystem on every deploy, so the first search after a deploy
    finds nothing on disk and regenerates it from the stored documents.

    Freshness is decided by the tenant's index_version rather than by an
    in-process invalidation call. reload_index() only ever cleared the cache in
    the process that handled the upload — correct with one worker, and silently
    wrong the moment there are two, where half the requests would keep
    answering from a stale index with no error anywhere.
    """
    slug = tenant["slug"]
    version = tenant.get("index_version", 0)

    hit = _cached(slug, version)
    if hit is not None:
        return hit

    with _lock_for(slug):
        # Re-check under the lock: another thread may have finished the rebuild
        # while this one was waiting for it.
        hit = _cached(slug, version)
        if hit is not None:
            return hit

        loaded = _read_from_disk(tenant)
        if loaded is None:
            return None
        _remember(slug, loaded[0], loaded[1], version)
        return loaded


def reload_index(slug):
    """Drop the cached index so the next search re-reads it from disk.

    Still called after an upload so this process sees the change immediately,
    without waiting to notice the version bump. Other processes pick it up from
    index_version on their next request.
    """
    with _cache_lock:
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
        # An explicit timeout, because the default is none: a hung connection
        # would otherwise hold a worker thread until gunicorn's 120s limit, and
        # there are only four threads. Three hung calls and the app is gone.
        #
        # max_retries=0 turns the SDK's own retry layer off. Retries are handled
        # deliberately in _complete_with_retry() with a budget chosen to keep
        # someone staring at a chat box waiting under ~5s; leaving both on would
        # multiply into a worst case several times that.
        _groq_client = Groq(api_key=api_key, timeout=LLM_TIMEOUT_SECONDS,
                            max_retries=0)
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


def _retry_after_seconds(exc):
    """How long the server asked us to wait, if it said.

    Groq answers a 429 with a Retry-After header, and it is usually 1-2
    seconds — far better information than any backoff curve we could guess.
    Ignoring it was measurably worse: a burst of questions failed on retries
    that were both too early and too few.
    """
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    raw = headers.get("retry-after") or headers.get("Retry-After")
    try:
        return min(float(raw), MAX_RETRY_WAIT_SECONDS)
    except (TypeError, ValueError):
        return None


def _complete_with_retry(client, prompt, attempts=5):
    """Call the LLM, retrying on rate limits.

    Groq's free tier caps tokens per minute and a burst across tenants hits it
    easily. This is the *only* retry layer — the SDK's own is switched off in
    _get_groq_client() so the two cannot multiply into a worst case nobody
    budgeted for. That makes honouring Retry-After this layer's job rather
    than a nicety: without it, eval.py dropped from 41/41 to 30/41, every
    failure a 429 the server had already told us how to survive.
    """
    import time

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
            wait = _retry_after_seconds(exc)
            if wait is None:
                wait = min(delay, MAX_RETRY_WAIT_SECONDS)
                delay *= 2
            time.sleep(wait)


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
