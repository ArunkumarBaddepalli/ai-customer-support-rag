FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Bake the embedding model into the image so the first request isn't slow
# (otherwise it downloads ~90 MB from Hugging Face on first use).
RUN python -c "from sentence_transformers import SentenceTransformer; \
    SentenceTransformer('all-MiniLM-L6-v2')"

# Seed the Pizza Palace demo workspace so a fresh deploy has something to show.
RUN python seed_demo.py

ENV PORT=7860
EXPOSE 7860

# A real WSGI server, not Flask's development server.
# One worker: each would load its own copy of the embedding model, and the
# instance doesn't have the memory for two. Threads handle concurrency instead,
# which suits this workload since requests are spent waiting on the LLM API.
CMD gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120 app:app
