# AI Customer-Support Chatbot (RAG)

A customer-support chatbot that answers questions using **your own documents** instead of
guessing. Built with Retrieval-Augmented Generation (RAG): it looks up the most relevant
pieces of your docs first, then asks an LLM to answer using only that context — and shows
which document each answer came from.

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
├── app.py            # Flask app — routes + wiring
├── rag.py             # embed question → search FAISS → ask LLM → answer + sources
├── ingest.py          # build the FAISS index from documents/
├── db.py              # SQLite chat history
├── documents/         # your source-of-truth text files
├── data/              # generated index + chunk store (gitignored)
├── static/            # style.css, script.js
├── templates/          # index.html
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
```

Add your own documents by dropping `.txt` files into `documents/` and re-running
`python ingest.py`.

## What if the bot is wrong / doesn't know?

Every retrieved chunk has a similarity score. If the best match is below a threshold
(see `MIN_SIMILARITY` in `rag.py`), the bot replies that it isn't sure instead of
fabricating an answer — this is the main defense against hallucination in this project.
The LLM is also instructed to answer only from the given context.

## Status

- [x] Runs locally
- [x] Answers from your own documents
- [x] Shows the source document per answer
- [x] Saves chat history (SQLite)
- [ ] Deployed with a public link
- [x] README

## Roadmap / possible extensions

- Measure accuracy on a fixed test set and report before/after improvement
- Rewrite vague follow-up questions using recent chat context
- Simple upload page for non-technical document updates
- Multi-language replies
