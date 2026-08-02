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

1. **Ingest** (`ingest.py`) — reads a tenant's `.txt` files, splits them into chunks,
   embeds each chunk with `sentence-transformers` (`all-MiniLM-L6-v2`), and writes a
   FAISS index to `data/<slug>/`.
2. **Retrieve** (`rag.py`) — embeds the question, searches **only that tenant's** index,
   and checks a similarity threshold.
3. **Generate** (`rag.py`) — sends question + retrieved chunks to Groq
   (`llama-3.1-8b-instant`), which answers from that context or handles small talk.
4. **Serve** (`app.py`) — Flask: accounts, dashboard, and the public bot at `/c/<slug>`.

## Multi-tenancy

One deployment serves many businesses. Isolation is enforced in three places:

| Layer | How |
|---|---|
| **Data** | `tenant_id` foreign key on chat history; every query is scoped to it |
| **Files** | Documents and indexes live under `documents/<slug>/` and `data/<slug>/` |
| **Access** | Dashboard routes resolve the tenant from the **session user**, never from a URL parameter — so there is no tenant id to tamper with |

Slugs are validated against `^[a-z0-9][a-z0-9-]{1,48}[a-z0-9]$` before they ever touch
the filesystem, uploads go through `secure_filename`, and a reserved-word list stops a
business claiming `/c/admin` or `/c/login`.

Verified by test: Acme Coffee's bot is asked a Pizza Palace question and correctly
refuses, and neither workspace's dashboard or chat history shows the other's data.

## Routes

| Route | Who | What |
|---|---|---|
| `/` | public | Landing page |
| `/signup`, `/login`, `/logout` | public | Accounts |
| `/onboarding` | owner | First-run wizard: branding → first document |
| `/dashboard` | owner | Add/delete documents (re-indexes immediately) |
| `/dashboard/settings` | owner | Name, tagline, logo, colour, support contact |
| `/dashboard/history` | owner | What customers actually asked |
| `/dashboard/profile` | owner | Change email / password |
| `/c/<slug>` | public | The business's live chatbot |
| `/api/c/<slug>/chat` | public | That bot's chat endpoint |

## Tech stack

| Piece | Tool |
|---|---|
| Backend | Python + Flask |
| Auth | Flask sessions + `werkzeug.security` password hashing (scrypt) |
| Embeddings | `sentence-transformers` (`all-MiniLM-L6-v2`) |
| Vector search | FAISS (one index per tenant) |
| LLM | Groq (`llama-3.1-8b-instant`, free API) |
| Database | SQLite (`users`, `tenants`, `chat_history`) |
| Frontend | HTML/CSS/vanilla JS, no framework |

## Project structure

```
├── app.py             # routes: auth, onboarding, dashboard, public bot
├── db.py              # SQLite: users, tenants, chat history
├── rag.py             # embed → search tenant's index → ask LLM → answer + sources
├── ingest.py          # per-tenant chunking and FAISS index building
├── eval.py            # 41-case accuracy + behaviour test suite
├── seed_demo.py       # creates the Pizza Palace demo workspace
├── sample_docs/       # the demo FAQ, copied into the demo tenant on seed
├── documents/<slug>/  # each tenant's uploaded docs   (generated, gitignored)
├── data/<slug>/       # each tenant's FAISS index     (generated, gitignored)
├── templates/         # landing, auth, dashboard, public chat
└── static/            # app.css (dashboard), style.css + script.js (chat widget)
```

## Running it locally

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env            # add your free Groq key: console.groq.com/keys

python seed_demo.py             # optional: creates the Pizza Palace demo
python app.py                   # http://localhost:5001
```

Then sign up at `/signup` to create your own workspace.

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
3. **Honest citations** — the model marks `SOURCED` / `NOSOURCE`, and a document is only
   credited when it says it actually used one. Earlier the bot printed "from: faq.txt"
   under answers that never touched the file. If the marker is missing the code
   defaults to *not* citing — under-cite rather than mis-cite.

`temperature=0`, so the same question always gets the same answer.

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
- **Ephemeral storage on free hosting.** Render wipes the container disk on redeploy,
  taking `chatbot.db`, uploaded documents, and indexes with it. Production would need a
  persistent disk or Postgres + object storage.
- **No email verification or password reset** — signup trusts the address given.
- **One workspace per account**, and indexes are rebuilt in-process on upload, which
  would need a background worker at real document volumes.
