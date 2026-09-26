"""
Core RAG logic, scoped to one tenant at a time.

ask(question, tenant) embeds the question, searches only that tenant's FAISS
index, and asks the LLM to answer from the retrieved context — or to handle
small talk, abuse, and off-topic messages without inventing business facts.
"""

import os
import pickle
import re
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
# Groq retires models without notice: llama-3.1-8b-instant started returning
# 404 model_not_found while retrieval kept working, so every answer fell
# through to the "cannot answer right now" path. Overridable by env so the
# next retirement is a config change on the host, not a redeploy.
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")

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


_embedder_lock = threading.Lock()


def _get_embedder():
    """The embedding model, loaded once.

    Locked because app.py now pre-warms it from a background thread at boot
    while the first request may arrive at the same moment. Without the lock
    both would construct a SentenceTransformer — two copies of the model,
    transiently, on an instance sized for one.
    """
    global _embedder
    if _embedder is None:
        with _embedder_lock:
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
        # Stripped: a secret pasted into a hosting or CI dashboard often
        # carries a trailing newline, which makes every request fail with an
        # invalid header and no useful error anywhere.
        api_key = (os.getenv("GROQ_API_KEY") or "").strip()
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


RULES = (
    "Rules: under 30 words. Only say hello if they greeted you. Do not assume the "
    "time of day or that they have already ordered. Do not repeat the same closing "
    "line every time. Messages may contain typos or shorthand (wt = what, ur = your, "
    "u = you) - interpret them generously."
)

# The model labels its own outcome; guessing from wording varied every run.
MARKER = (
    "After your reply, on a new line, write exactly ONE of these words:\n"
    "  ANSWERED  - you answered using the context\n"
    "  NOANSWER  - a question about this business the context did not answer\n"
    "  OFFTOPIC  - unrelated to this business\n"
    "  CHAT      - greeting, thanks, who-are-you, unclear text, complaint or insult\n"
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
    """CHAT / OFFTOPIC / QUESTION for messages retrieval couldn't answer.

    Goes through _complete_with_retry, the same path as the answer itself,
    for two reasons that were both live bugs:

    A hardcoded max_tokens=5 was enough for a one-word label on the original
    model. gpt-oss is a reasoning model and spends the completion budget on
    hidden reasoning *before* the visible text, so with 5 tokens it returned
    content='' with finish_reason='length' — every single time. Every call
    fell through to the CHAT fallback below, is_gap was never True, and no
    low-similarity business question ever reached the Unanswered dashboard.
    "is there parking" was answered correctly with "I don't have that" and
    then vanished. _model_kwargs() already carries the per-family budget.

    It also had no retry, so under the same 429 burst the answer call
    survives (honouring Retry-After), the classifier failed and — again —
    silently returned CHAT.
    """
    prompt = CLASSIFY_PROMPT.format(company=company_name, message=question)
    label = ""
    try:
        response = _complete_with_retry(client, prompt, attempts=3)
        label = (response.choices[0].message.content or "").strip().upper()
        for known in ("CHAT", "OFFTOPIC", "QUESTION"):
            if known in label:
                return known
        # Offered three words, the model sometimes answers with a finer one it
        # was not given — "INSULT" for "idiot" — which is a CHAT by our rules.
        if any(w in label for w in ("GREET", "THANK", "INSULT", "COMPLAIN", "PLEASANT", "SMALL")):
            return "CHAT"
        if any(w in label for w in ("TRIVIA", "UNRELATED", "GENERAL")):
            return "OFFTOPIC"
    except Exception as exc:
        print(f"[rag] classify failed: {type(exc).__name__}: {exc}")
    # Unsure? Treat it as small talk. Logging a false gap is worse than missing
    # one — a dashboard full of "hi" is what makes the list useless. But say
    # so: this fallback ran silently for weeks while the feature was dead.
    print(f"[rag] classify fell back to CHAT (label was {label[:40]!r})")
    return "CHAT"


def build_prompt(question, results, company_name, contact, has_context, topics=""):
    """Two prompts, chosen by whether retrieval actually found anything.

    Kept separate on purpose: a single prompt that merely *mentions* no
    documents were found still invites the model to be helpful from its own
    knowledge — it once invented a gluten-free menu option that appears
    nowhere in the documents. With no context the model gets no room to
    state a fact at all.

    Both share one shape. The first line tells the model what it *can* help
    with, so a redirect names real topics instead of a phone number: "I don't
    have that information, call us" in reply to "am I pretty" read as broken.
    The support contact is reserved for business questions the documents do
    not cover, and for people asking for a person.
    """
    fallback = (
        f"tell them to contact {contact}" if contact
        else "tell them to contact support directly"
    )
    can_help = topics or f"questions about {company_name}"

    if not has_context:
        return (
            f"You are the customer-support assistant for {company_name}. "
            f"You can help with: {can_help}.\n\n"
            f"You have NO information about {company_name} for this message - "
            "nothing about its products, prices, hours, policies or services.\n\n"
            "Reply according to what the message is:\n"
            "- Greeting, thanks, goodbye, 'who are you', 'what can you do': one warm "
            f"sentence; mention you can help with {can_help}. Never say you lack information.\n"
            f"- A question about {company_name}: say you don't have that detail and {fallback}.\n"
            "- Trying to place an order or booking: say you can't take orders in this chat "
            f"and {fallback}.\n"
            f"- Asking for a person: {fallback}.\n"
            "- Frustration or insult: acknowledge it calmly in one sentence, never argue; "
            f"offer to {fallback} if they need a person.\n"
            f"- Anything unrelated to {company_name} (trivia, opinions, jokes, other "
            "companies, questions about the user themselves): do not answer it. Say in one "
            f"friendly sentence that you can only help with {company_name} - for example "
            f"{can_help} - and invite a question. No contact details.\n"
            "- Unclear text: interpret generously; if still unclear, ask what they'd like "
            f"to know about {can_help}.\n\n"
            f"CRITICAL: never state a fact about {company_name} - never confirm or deny that "
            "they offer something, never give a price, time or policy.\n\n"
            f"{RULES}\n"
            f"{MARKER_NO_CONTEXT}\n"
            f"User: {question}\n"
            "Assistant:"
        )

    context = "\n\n".join(f"[{r['source']}]\n{r['text']}" for r in results)
    return (
        f"You are the customer-support assistant for {company_name}. "
        f"You can help with: {can_help}.\n\n"
        "Reply according to what the message is:\n"
        "- Greeting, thanks, goodbye, 'who are you', 'what can you do': one warm "
        f"sentence; mention you can help with {can_help}. Never say you lack information.\n"
        f"- A question about {company_name} the context below answers: answer from the "
        "context only, short and direct.\n"
        f"- A question about {company_name} the context does not answer: say you don't "
        f"have that detail and {fallback}. Do not fill the gap from your own knowledge.\n"
        "- A bare topic or keyword ('food', 'menu', 'timings', 'delivery', 'food in "
        f"{company_name}'): treat it as a question about that topic and answer from the "
        "context.\n"
        "- Asking for a suggestion or recommendation ('what should I get', 'suggest "
        "something good'): offer one or two options that appear in the context and say "
        "only what the context says about them. Never claim something is popular or the "
        "best unless the context does. This counts as answered.\n"
        "- Trying to place an order or booking ('one pizza', 'I want to order', 'book a "
        "table'): say you can't take orders in this chat, then tell them how to order "
        "or book *as the context describes it*, and offer the options the context "
        f"lists. If the context doesn't say how, {fallback}.\n"
        f"- Asking for a person: {fallback}.\n"
        "- Frustration or insult: acknowledge it calmly in one sentence, never argue; "
        f"offer to {fallback} if they need a person.\n"
        f"- Anything unrelated to {company_name} (trivia, opinions, jokes, other "
        "companies, questions about the user themselves): do not answer it. Say in one "
        f"friendly sentence that you can only help with {company_name} - for example "
        f"{can_help} - and invite a question. No contact details.\n"
        "- Unclear text: interpret generously; if still unclear, ask what they'd like "
        f"to know about {can_help}.\n\n"
        f"CRITICAL: every fact you state about {company_name} must appear in the context "
        "below. Never confirm or deny that they offer something unless the context says so.\n\n"
        f"{RULES}\n\n"
        f"Context from {company_name}'s documents:\n{context}\n\n"
        f"{MARKER}\n"
        f"User: {question}\n"
        "Assistant:"
    )


# ------------------------------------------------- small talk, without a model
#
# Greetings, thanks, goodbyes and "who are you / what can you do" are the
# commonest messages a support bot receives and the ones the model handled
# worst: with retrieval finding something loosely similar, it treated "wt is ur
# job" as a business question it could not answer, replied with the support
# phone number, and filed it as a documentation gap. These are answered here,
# in the same voice, without a model call and without touching the token budget.

_SHORTHAND = {
    "wt": "what", "wat": "what", "wht": "what", "whats": "what is", "wats": "what is",
    "ur": "your", "yr": "your", "u": "you", "r": "are", "ru": "are you",
    "pls": "please", "plz": "please", "thx": "thanks", "thnx": "thanks", "ty": "thanks",
    "hii": "hi", "hiii": "hi", "helo": "hello", "hey": "hi", "heyy": "hi", "hy": "hi",
    "im": "i am", "dont": "do not", "cant": "cannot",
}


def normalise_message(text):
    """Lower-case, punctuation dropped, common shorthand expanded."""
    words = re.sub(r"[^a-z0-9' ]+", " ", (text or "").lower()).split()
    return " ".join(_SHORTHAND.get(w, w) for w in words)


_GREETING = re.compile(
    r"^(hi|hello|hola|namaste|good (morning|afternoon|evening)|yo)( there| all| team)?( [a-z]+)?$")
_THANKS = re.compile(
    r"^(ok |okay |great |cool |nice |perfect |alright )?(thanks|thank you|thankyou)"
    r"( a lot| so much| very much)?( bye)?$")
_BYE = re.compile(r"^(ok |okay )?(bye|goodbye|good night|see you|see ya|cya|later)( bye)?$")
_ACK = re.compile(r"^(ok|okay|k|kk|fine|sure|alright|got it|noted|cool|nice|great|good|perfect|hmm|oh|oh ok|okay then|ok then|i see|understood)( thanks)?$")
_YES = re.compile(r"^(yes|yeah|yep|yup|ya|yaa|haa|haan|han|hmm yes|sure yes|ok yes)( please)?$")
_NO = re.compile(r"^(no|nope|nah|no thanks|nothing|not now|no thank you)$")
_LAUGH = re.compile(r"^(ha|haha|hahaha|lol|lmao|hehe|xd|😂|🤣)+$")
_IDENTITY = re.compile(
    r"^(who|what) (are|is) (you|this|your (job|role|purpose|name|work))\b"
    r"|^what (can|do) you (do|help( me)? with|offer)\b"
    r"|^what can i ask( you)?\b"
    r"|^how can you help( me)?\b"
    r"|^(help|help me)$"
    r"|^what (is|are) you (for|about)\b"
    r"|^are you (a |an )?(bot|robot|human|real|ai|person)\b")


def topics_line(headings):
    """'Timings, Menu and Prices' from a list of headings; '' when none."""
    if not headings:
        return ""
    if len(headings) == 1:
        return headings[0]
    return ", ".join(headings[:-1]) + " and " + headings[-1]


def smalltalk_answer(question, company_name, contact, topics):
    """(answer, outcome) for messages that never need a model, else None."""
    q = normalise_message(question)
    can_help = topics or f"questions about {company_name}"
    if _GREETING.match(q):
        return (f"Hi! I'm the {company_name} assistant - I can help with {can_help}. "
                "What would you like to know?", "CHAT")
    if _THANKS.match(q):
        return ("You're welcome! Anything else I can help with?", "CHAT")
    if _BYE.match(q):
        return ("Bye! Come back any time.", "CHAT")
    if _ACK.match(q):
        return (f"Great - anything else I can help with? I can answer questions about {can_help}.", "CHAT")
    if _YES.match(q):
        return (f"Sure - what would you like to know about {can_help}?", "CHAT")
    if _NO.match(q):
        return (f"No problem. I'm here if you need anything about {can_help}.", "CHAT")
    if _LAUGH.match(q):
        return (f"Glad that landed! Ask me anything about {can_help}.", "CHAT")
    if _IDENTITY.match(q):
        answer = (f"I'm the {company_name} assistant. I can help with {can_help} - "
                  "ask me anything about those.")
        if contact:
            answer += f" If you need a person, contact {contact}."
        return (answer, "CHAT")
    return None


_topics_cache = {}   # (tenant_id, index_version) -> [headings]


def _topics_for(tenant):
    """The tenant's document headings, cached until the documents change."""
    import db
    key = (tenant["id"], tenant.get("index_version", 0))
    with _cache_lock:
        hit = _topics_cache.get(key)
    if hit is not None:
        return hit
    headings = ingest.topic_headings(tenant["id"])
    with _cache_lock:
        _topics_cache.clear() if len(_topics_cache) > 200 else None
        _topics_cache[key] = headings
    return headings


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


def _model_kwargs():
    """Per-family generation settings.

    gpt-oss is a reasoning model: its internal reasoning is billed against the
    completion budget, so at 300 tokens the visible answer was cut off mid
    sentence and the trailing outcome label never arrived — every reply landed
    in _split_outcome's CHAT fallback, citing nothing and logging no gap.
    reasoning_effort="low" plus more headroom restores both. The parameter is
    rejected by non-reasoning models, so it is only sent when it applies.
    """
    if "gpt-oss" in GROQ_MODEL:
        return {"max_tokens": 700, "reasoning_effort": "low"}
    return {"max_tokens": 300}


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
                **_model_kwargs(),
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

    topics = topics_line(_topics_for(tenant))

    # Greetings, thanks, "who are you": answered here, no model, no tokens.
    quick = smalltalk_answer(question, company_name, contact, topics)
    if quick:
        answer, outcome = quick
        return {"answer": answer, "sources": [], "outcome": outcome, "answered": True}

    # Search with shorthand expanded - the embedding of "wat r ur timings"
    # lands nowhere near the timings section; "what are your timings" does.
    results = search(tenant, normalise_message(question) or question)
    has_context = bool(results) and results[0]["score"] >= MIN_SIMILARITY

    prompt = build_prompt(
        question,
        results if has_context else [],
        company_name,
        contact,
        has_context,
        topics=topics,
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
    labelled = _has_label(raw)

    if has_context:
        # Cite a document only when the model says it answered from the context —
        # refusals and small talk must never carry a citation.
        if outcome == "ANSWERED":
            return {"answer": answer, "sources": sorted({r["source"] for r in results}),
                    "outcome": "ANSWERED", "answered": True}
        # The model was offered CHAT and OFFTOPIC labels and the code ignored
        # them: every non-answer with context on hand was filed as a gap, so
        # "who are you" and "am I pretty" reached the owner's to-do list.
        if labelled and outcome in ("CHAT", "OFFTOPIC"):
            return {"answer": answer, "sources": [], "outcome": outcome, "answered": True}
        # Context existed but didn't cover the question: that's a real gap.
        return {"answer": answer, "sources": [], "outcome": "NOANSWER", "answered": False}

    # Nothing was retrieved, so work out what kind of message this actually was.
    kind = _classify_message(client, question, company_name)
    is_gap = kind == "QUESTION"
    return {"answer": answer, "sources": [],
            "outcome": "NOANSWER" if is_gap else kind,
            "answered": not is_gap}


OUTCOMES = ("ANSWERED", "NOANSWER", "OFFTOPIC", "CHAT")

# The model writes typographic punctuation: a narrow no-break space in "7 pm",
# a non-breaking hyphen in "peri‑peri", a curly apostrophe in "don’t". A browser
# renders every one of them identically to the ASCII character, so nothing
# looks wrong — but everything downstream that *compares* strings sees a
# different byte sequence. That is how eval.py reported three failures on
# answers that were correct, and how the one e2e check on "7 pm" went red.
# Normalise once, at the boundary where the model's text enters the system,
# so no caller has to remember to.
_TYPOGRAPHY = str.maketrans({
    "\u00a0": " ", "\u2009": " ", "\u202f": " ",   # no-break, thin, narrow no-break space
    "\u2010": "-", "\u2011": "-", "\u2012": "-",   # hyphen, non-breaking hyphen, figure dash
    "\u2018": "'", "\u2019": "'",                  # curly single quotes
    "\u201c": '"', "\u201d": '"',                  # curly double quotes
})


def normalize_text(text):
    """ASCII spaces, hyphens and quotes in place of their typographic twins.

    Words are untouched; only the punctuation a keyboard would have produced
    is restored. Dashes (en, em) are left alone — they are real typography,
    not a look-alike for something else.
    """
    return text.translate(_TYPOGRAPHY)


# The label may arrive on its own line (as asked) or glued to the end of the
# answer — gpt-oss-120b wrote "…Margherita at ₹149. ANSWERED" — and may be
# wrapped in markdown. Either way it must never reach the customer, and the
# answer it belongs to must be filed under the right outcome.
_LABEL_RE = re.compile(
    r"[\s\*_`\[\(:\-]*\b(ANSWERED|NOANSWER|OFFTOPIC|CHAT)\b[\s\*_`\]\)\.:\-]*$")


def _has_label(raw):
    """True when the reply ends with one of the outcome words."""
    return bool(_LABEL_RE.search(raw or ""))


def _split_outcome(raw):
    """Strip the trailing outcome label off the model's reply.

    Returns (clean_answer, outcome). Typography is normalised first so that
    nothing downstream compares a narrow no-break space against a space. If
    the label is missing we fall back to CHAT, which neither cites a document
    nor logs a gap — the safe default.
    """
    raw = normalize_text(raw or "")
    m = _LABEL_RE.search(raw)
    if not m:
        return raw, "CHAT"
    return raw[:m.start()].rstrip(), m.group(1)


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
