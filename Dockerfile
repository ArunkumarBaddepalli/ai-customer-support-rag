# 3.13, not 3.11: requirements.txt pins numpy==2.5.1, which publishes no
# wheel below Python 3.12. On 3.11 the build died at pip install with
# "Could not find a version that satisfies the requirement numpy==2.5.1",
# so every deploy since the pins landed failed and the running container
# stayed several commits behind the repository.
FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .

# Install the CPU-only build of torch before anything else can pull torch in.
# PyPI's default Linux wheel is the CUDA build, which drags in ~2.4 GB of NVIDIA
# libraries (cudnn, cublas, cufft, nccl ...). None of it can execute on a CPU
# instance, but the image still has to be pulled on every cold start, and on
# free hosting that pull was taking minutes. CPU wheel: 183 MB vs 2477 MB.
# Separate index-url, so only torch comes from here and everything else in
# requirements.txt still resolves against PyPI.
RUN pip install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cpu \
        torch

RUN pip install --no-cache-dir -r requirements.txt \
    && python -c "import torch; assert '+cpu' in torch.__version__, \
       f'expected the CPU build, got {torch.__version__}'; \
       print('torch', torch.__version__)"

COPY . .

# Bake the embedding model into the image so the first request isn't slow
# (otherwise it downloads ~90 MB from Hugging Face on first use).
RUN python -c "from sentence_transformers import SentenceTransformer; \
    SentenceTransformer('all-MiniLM-L6-v2')"

ENV PORT=7860
EXPOSE 7860

# The demo workspace is seeded at *start*, not at build. Seeding during the
# build ran with no DATABASE_URL set, so it wrote a SQLite file — containing a
# demo account with a known password — into the image layer, where production
# never reads it. The demo it was meant to provide never appeared, and a
# credentialed database file shipped in every image. At start the real
# DATABASE_URL is present, seed_demo.seed() is idempotent, and SEED_DEMO=false
# turns it off for a deployment that does not want it.
ENV SEED_DEMO=true

# A real WSGI server, not Flask's development server.
# One worker: each would load its own copy of the embedding model, and the
# instance doesn't have the memory for two. Threads handle concurrency instead,
# which suits this workload since requests are spent waiting on the LLM API.
CMD if [ "$SEED_DEMO" = "true" ]; then python seed_demo.py || echo "seed skipped"; fi && \
    exec gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120 app:app
