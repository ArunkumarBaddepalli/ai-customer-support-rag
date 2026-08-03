# SupportBot — multi-tenant AI customer-support chatbot (RAG)

A small SaaS: a business signs up, uploads their FAQ, and gets a public chat page that
answers customer questions **from their own documents** — citing which document it used,
and saying "I don't know" instead of inventing an answer.

Built with Retrieval-Augmented Generation. Each business is an isolated workspace with
its own documents, its own search index, and its own branding.

**Live demo:** https://ai-customer-support-rag-mpki.onrender.com
*(free-tier hosting sleeps after ~15 min idle — the first request can take 30–60s to wake up)*

Demo workspace: `/c/pizza-palace` · sign in as `demo@pizzapalace.example` / `demo12345`

## Why RAG instead of just calling ChatGPT?

A general LLM has never seen your prices, timings, or refund policy, so it guesses.
RAG retrieves the relevant passage from your documents first and hands it to the model
as context, so answers are grounded in facts you control — and you can show the source.

## How it works

1. **Ingest** (`ingest.py`) — reads a tenant's documents from the database, splits them
   into chunks, embeds each with `sentence-transformers` (`all-MiniLM-L6-v2`), and writes
   a FAISS index to `data/<slug>/`.
2. **Retrieve** (`rag.py`) — embeds the question, searches **only that tenant's** index,
   and checks a similarity threshold.
3. **Generate** (`rag.py`) — sends question + retrieved chunks to Groq
   (`llama-3.1-8b-instant`), which answers from that context or handles small talk.
4. **Serve** (`app.py`) — Flask: accounts, dashboard, and the public bot at `/c/<slug>`.

## Multi-tenancy

One deployment serves many businesses. Isolation is enforced in three places:

| Layer | How |
|---|---|
| **Data** | `tenant_id` foreign key on documents and unanswered questions; every query is scoped to it |
| **Files** | Each tenant's FAISS index is built separately, under `data/<slug>/` |
| **Access** | Dashboard routes resolve the tenant from the **session user**, never from a URL parameter — so there is no tenant id to tamper with |

Slugs are validated against `^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$` before they ever touch
the filesystem, uploads go through `secure_filename`, and a reserved-word list stops a
business claiming `/c/admin` or `/c/login`.

Verified by test: one workspace's bot is asked another's question and correctly refuses,
and neither dashboard nor unanswered list shows the other's data.

## Routes

| Route | Who | What |
|---|---|---|
| `/` | public | Landing page |
| `/signup`, `/login`, `/logout` | public | Accounts |
| `/onboarding` | owner | First-run wizard: branding → first document |
| `/dashboard` | owner | Add/delete documents (re-indexes immediately) |
| `/dashboard/settings` | owner | Name, tagline, logo, colour, support contact |
| `/dashboard/gaps` | owner | Questions the bot couldn't answer |
| `/dashboard/profile` | owner | Change email / password |
| `/c/<slug>` | public | The business's live chatbot |
| `/c/<slug>/logo` | public | That business's logo, served from the database |
| `/api/c/<slug>/chat` | public | That bot's chat endpoint |

## Tech stack

| Piece | Tool |
|---|---|
| Backend | Python + Flask |
| Auth | Flask sessions + PBKDF2-SHA256 password hashing (600k iterations) |
| Embeddings | `sentence-transformers` (`all-MiniLM-L6-v2`) |
| Vector search | FAISS (one index per tenant) |
| LLM | Groq (`llama-3.1-8b-instant`, free API) |
| Database | Postgres in production, SQLite locally (same code) |
| Frontend | HTML/CSS/vanilla JS, no framework |

## Project structure

```
├── app.py             # routes: auth, onboarding, dashboard, public bot
├── db.py              # storage: Postgres or SQLite behind one interface
├── security.py        # password hashing
├── rag.py             # embed → search tenant's index → ask LLM → answer + sources
├── ingest.py          # per-tenant chunking and FAISS index building
├── eval.py            # 41-case answer-quality suite
├── tests/e2e.py       # 73-check end-to-end suite
├── seed_demo.py       # creates the Pizza Palace demo workspace
├── sample_docs/       # demo FAQ + example FAQs you can upload
├── data/<slug>/       # each tenant's FAISS index (rebuildable, gitignored)
├── templates/         # landing, auth, dashboard, public chat
└── static/            # app.css (dashboard), style.css + script.js (chat widget)
```

## Running it locally

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env            # add your free Groq key: console.groq.com/keys
                                 # DATABASE_URL is optional locally — without it
                                 # a SQLite file is used, so there's nothing to set up

python seed_demo.py             # optional: creates the Pizza Palace demo
python app.py                   # http://localhost:5001
```

Then sign up at `/signup` to create your own workspace.

Sample FAQs to try are in [`sample_docs/examples/`](sample_docs/examples/) — a florist,
a gym and an online store. Upload one under **Documents** and ask its bot something.

In production it runs under gunicorn rather than Flask's development server:

```bash
gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120 app:app
```

One worker on purpose — each would load its own copy of the embedding model, and a
small instance doesn't have the memory for two. Threads handle concurrency, which
suits a workload that spends most of its time waiting on the LLM API.

## Handling whatever a real user types

Users type anything — "hi", "how are you", insults, trivia, gibberish. A pure RAG bot
scores all of those as irrelevant and refuses, which reads as broken.

An early version used a hardcoded list of greeting words, but that only covers phrasings
you thought of. Instead **one prompt handles routing** and the LLM picks the case:

| Input | Behaviour |
|---|---|
| Greeting ("hi") | Greets back once, asks how it can help |
| Pleasantry ("how are you", "thanks") | Answers warmly — does *not* greet again |
| Question the docs answer | Answers from context, cites the document |
| Question the docs don't answer | Says so, gives the support contact |
| Frustration or abuse ("this is useless", "idiot") | Stays calm, doesn't argue, offers a human |
| Off-topic ("capital of France") | Declines — *even though the model knows the answer* |

Two of those rows came from watching it fail: insults were landing in the small-talk
bucket, so "idiot" got answered with *"Hello, how are you today?"* — tone-deaf exactly
when a customer is angriest. And off-topic questions were being answered from the
model's own knowledge, which proves the bot isn't really grounded in the docs.

## What stops it making things up

1. **Similarity threshold** — below `MIN_SIMILARITY` no context is passed to the LLM at
   all, so there is nothing to embellish.
2. **Prompt constraints** — answer only from context; never invent prices, timings, or
   policies; never claim what the business does or doesn't offer.
3. **Honest citations** — the model labels its own outcome, and a document is only
   credited when it says it answered from the context. Earlier the bot printed
   "from: faq.txt" under answers that never touched the file. If the label is missing
   the code defaults to *not* citing — under-cite rather than mis-cite.

A worked example of why this matters: asked *"do you have gluten free bases"* — a phrase
appearing nowhere in the FAQ — an earlier version replied *"Yes, we offer gluten-free
pizza bases."* Retrieval had scored 0.149, below the threshold, so **no context was passed
at all** and the model filled the silence. The fix was splitting the prompt in two: when
there is no context, the model is given no room to answer rather than merely being told
the documents were empty.

`temperature=0`, so the same question always gets the same answer.

## Storage — why nothing is kept on disk

Free hosting wipes the container's filesystem on every deploy. Anything stored
only as a file would disappear, taking every signup with it. So the database is
the source of truth for **everything durable** — accounts, branding, document
text, and logo images (as bytes, served from `/c/<slug>/logo`).

The FAISS index is the deliberate exception. It's derived data, so it isn't
persisted at all: on a fresh container the first search finds no index on disk
and rebuilds it from the documents in the database.

```
Postgres ── users, tenants, documents, logos, unanswered   (durable)
   │
   └─ rebuilt on demand ──> data/<slug>/index.faiss        (disposable cache)
```

Set `DATABASE_URL` to any Postgres — the database doesn't have to live with the
web host, so a free Neon or Supabase instance works while the app runs on Render.
Without `DATABASE_URL` it falls back to SQLite, which keeps local development
zero-setup. Both are exercised by the test suite.

Verified by wiping every local file and restarting: the account still logs in,
branding and logo are intact, and the bot answers from documents it re-indexed
from the database.

## Tests

Two suites, testing different things.

**`tests/e2e.py` — 71 checks across every user perspective.** Start the app, then:

```bash
python tests/e2e.py
```

It walks a first-time visitor, a business signing up and onboarding, their customers
chatting, a second business, and an attacker — each with its own cookie jar, so
sessions behave like separate browsers. Notable checks:

| Perspective | Verifies |
|---|---|
| Visitor | Landing page, 404s, and that every dashboard route is gated |
| Signup | Each validation rule, duplicate email |
| Owner | Onboarding, add/delete documents, settings, password change |
| Customer | Answers cite sources; refusals don't; escalation; abuse stays calm |
| Isolation | Two businesses can't see each other's documents, bots, or gap lists |
| Attacker | Path traversal, script-as-PNG, SVG, corrupt image, XSS in company name |

It's written with `urllib` rather than curl deliberately: shell quoting silently
mangled test values more than once and produced failures that looked like
application bugs.

**`eval.py` — 41 cases measuring answer quality.** See below.

## Measuring accuracy (`eval.py`)

41 fixed cases: every FAQ topic, plus small talk, abuse, off-topic questions, and
*negative* assertions for things that must never happen (asking the capital of France
must not produce "Paris"; an insult must not be answered with "Hello").

| Change | Score |
|---|---|
| Baseline (fixed 500-char chunks, cut mid-section) | 83.9% (26/31) |
| Chunk by FAQ section instead of raw character count | 87.1% (27/31) |
| Lower `MIN_SIMILARITY` once retrieval was confirmed correct | 100% (31/31) |
| Suite expanded to 37 cases (small talk, off-topic, leak checks) | 97.3% (36/37) |
| Ambiguous FAQ wording fixed + `temperature=0` | 100% (37/37) |
| Suite expanded to 41 cases (abuse handling, tone checks) | **100% (41/41)** |

Two findings worth stating plainly:

- **The first 100% was partly luck.** At `temperature=0.2` a different case failed each
  run — not because the answer was wrong, but because a correct answer phrased
  differently missed its keyword. Keyword matching flatters a nondeterministic model.
- **One failure was a bad document, not a bad bot.** "How late can I report a damaged
  order?" was ambiguous because the FAQ said both "within 30 minutes" and "not after 2
  hours". The fix was rewriting the FAQ. In RAG, answer quality is capped by document
  quality.

```bash
python seed_demo.py && python eval.py
```

## What's next

See [ROADMAP.md](ROADMAP.md) for planned work and the feasibility notes behind it —
an **embed widget** (one `<script>` tag to drop the bot on your own site), a
**website crawler** that drafts your documents from your existing site, and
**confidence-scored answers** that rerank candidates and let certainty drive
behaviour instead of one hard threshold.

## Known limitations

- **No conversation memory.** Each message is handled independently, so follow-ups
  ("how much?" after "do you have Margherita?") don't resolve. This is the next thing
  I'd build.
- **No email verification or password reset** — signup trusts the address given.
- **One workspace per account**, and indexes are rebuilt in-process on upload, which
  would need a background worker at real document volumes.
