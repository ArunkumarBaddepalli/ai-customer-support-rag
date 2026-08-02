# AI Customer-Support Chatbot (RAG)

A customer-support chatbot that answers questions using **your own documents** instead of
guessing. Built with Retrieval-Augmented Generation (RAG): it looks up the most relevant
pieces of your docs first, then asks an LLM to answer using only that context — and shows
which document each answer came from.

**Live demo:** https://ai-customer-support-rag-mpki.onrender.com
*(free-tier hosting sleeps after ~15 min idle — first request after a while can take 30-60s to wake up)*

## Why RAG instead of just calling ChatGPT/Gemini directly?

A plain LLM only knows what it was trained on — it has never seen your company's FAQ,
prices, or policies, so it will guess or make things up. RAG fixes that by retrieving the
actual relevant text from your documents and handing it to the LLM as context, so answers
are grounded in facts you control.

## How it works

1. **Ingest** (`ingest.py`) — reads `.txt` files from `documents/`, splits them into small
   chunks, turns each chunk into a vector ("embedding") using `sentence-transformers`
   (`all-MiniLM-L6-v2`), and stores the vectors in a FAISS index for fast similarity search.
2. **Retrieve** (`rag.py`) — embeds the user's question the same way, searches FAISS for the
   top matching chunks, and checks a similarity threshold. If nothing matches well enough,
   the bot says it doesn't know instead of guessing.
3. **Generate** (`rag.py`) — sends the question + retrieved chunks to Groq's LLM
   (`llama-3.1-8b-instant`), which writes the final answer using only that context.
4. **Serve** (`app.py`) — a Flask API + simple chat page. Every answer is saved to a SQLite
   database (`chatbot.db`) along with which document(s) it used.

## Tech stack

| Piece | Tool |
|---|---|
| Backend | Python + Flask |
| Embeddings | `sentence-transformers` (`all-MiniLM-L6-v2`) |
| Vector search | FAISS |
| LLM | Groq (`llama-3.1-8b-instant`, free API) |
| Chat history | SQLite |
| Frontend | HTML/CSS/vanilla JS |

## Project structure

```
├── app.py            # Flask app — routes + wiring + /admin auth
├── rag.py             # embed question → search FAISS → ask LLM → answer + sources
├── ingest.py          # build the FAISS index from documents/
├── db.py              # SQLite chat history
├── config.py          # branding (company name, tagline, logo, color)
├── documents/         # your source-of-truth text files
├── data/              # generated index + chunk store (gitignored)
├── static/            # style.css, script.js, admin.css
├── templates/          # index.html, admin_login.html, admin_upload.html
├── requirements.txt
├── .env.example       # copy to .env and add your Groq key
└── .gitignore
```

## Running it locally

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env            # then paste your free Groq API key into .env
                                 # get one at https://console.groq.com/keys

python ingest.py                # builds the search index from documents/
python rag.py                   # optional: test in the terminal first
python app.py                   # starts the web app on http://localhost:5001
                                 # set FLASK_DEBUG=true for Flask's debug/reload mode
```

Add your own documents by dropping `.txt` files into `documents/` and re-running
`python ingest.py` — or use the admin page below, no terminal needed.

## Admin page — adding documents without touching code

Visit `/admin` (e.g. http://localhost:5001/admin), log in with the password set in
`ADMIN_PASSWORD`, then either upload a `.txt` file or paste a title + text. Saving
rebuilds the search index immediately — the bot uses the new content on the very next
question, no restart needed.

- If `ADMIN_PASSWORD` isn't set, `/admin` is disabled entirely (returns 503) — this is
  meant for technical staff/managers, not public access.
- **On free-tier hosts (Render, etc.), uploads only persist until the next restart or
  redeploy** — the container's disk is wiped on redeploy. For a permanent change, also
  commit the `.txt` file to `documents/` in git so it's baked into the next deploy.

## Branding — reskinning for a different company

`config.py` (or the matching env vars) control the company name, tagline, logo emoji,
and header color shown on the chat page — no template edits needed:

```bash
COMPANY_NAME=Acme Inc.
COMPANY_TAGLINE=Ask me about orders, shipping, or returns.
LOGO_EMOJI=📦
BRAND_COLOR=#16a34a
```

## What if the bot is wrong / doesn't know?

Every retrieved chunk has a similarity score. If the best match is below a threshold
(see `MIN_SIMILARITY` in `rag.py`), the bot replies that it isn't sure instead of
fabricating an answer — this is the main defense against hallucination in this project.
The LLM is also instructed to answer only from the given context.

## Measuring accuracy (`eval.py`)

`eval.py` runs 31 fixed test questions (covering every FAQ topic, plus a few
deliberately out-of-scope ones) and checks whether the expected keyword shows up in
the bot's answer. Two real fixes moved the score:

| Change | Score |
|---|---|
| Baseline (fixed 500-char chunks, cut mid-section) | 83.9% (26/31) |
| Chunk by FAQ section instead of raw character count | 87.1% (27/31) |
| Lower the confidence threshold (`MIN_SIMILARITY`) once retrieval was confirmed correct | **100% (31/31)** |

Root cause for the remaining misses after the chunking fix: the right chunk *was*
being retrieved, but its similarity score sat just under the cutoff, so the bot
refused to answer before the LLM ever saw the context. Out-of-scope questions
("Do you sell laptops?") still correctly get refused — the threshold was tuned
down, not removed.

```bash
python eval.py
```

## Status

- [x] Runs locally
- [x] Answers from your own documents
- [x] Shows the source document per answer
- [x] Saves chat history (SQLite)
- [x] Deployed with a public link
- [x] README

## Roadmap / possible extensions

- Rewrite vague follow-up questions using recent chat context
- Persist admin-uploaded documents outside the container (S3, persistent disk, or a DB)
  so they survive redeploys
- Multi-language replies
