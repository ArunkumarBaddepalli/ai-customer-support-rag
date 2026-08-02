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

CMD ["python", "app.py"]
