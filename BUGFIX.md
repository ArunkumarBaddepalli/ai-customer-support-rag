# Bugfix Report — AI Customer Support RAG

**Date:** 2026-08-23
**Scope:** Full backend + frontend audit, plus live testing against a running instance (`python app.py`, SQLite, Groq `openai/gpt-oss-20b`, Brevo mail).
**Status:** Audit of 2026-08-23, updated 2026-09-13. BUG-1, 2, 5, 6 and 8 are fixed on the `improvements` branch, each with a regression check; BUG-3, 4 and 7 remain open and are recorded below with the fix proposed.

---

## How this was tested

- Read every source file (`app.py`, `rag.py`, `db.py`, `ingest.py`, `security.py`, `mailer.py`, all templates, all static JS).
- Ran the app live and exercised flows directly over HTTP (curl + cookie jars): signup → onboarding → dashboard → upload → public chat, plus auth, CSRF, rate-limit, and security probes.
- Ran the bundled suite `tests/e2e.py` against the live app: **133 passed, 1 failed** (the 1 fail is a false negative — see BUG-8).
- 2026-09-13, after the fixes on `improvements`: **142 passed, 0 failed, 142 checks**; `tests/test_units.py` 44 passed; `eval.py` 44 cases.
- Traced RAG internals directly (`rag.search`, `rag.ask`, `rag._classify_message`) to measure retrieval scores, outcomes, and per-call latency.

The e2e suite is green, but it does **not** cover the classifier accuracy, retrieval quality on short docs, the `next` redirect param, or FE/BE extension parity. All bugs below live in those gaps.

---

## Severity summary

| ID | Severity | Area | One-line |
|----|----------|------|----------|
| BUG-1 | **P0** | Security (BE) | Open redirect via login `?next=//evil.com` |
| BUG-2 | **P0** | Core feature (BE) | Classifier always returns CHAT → Unanswered dashboard silently misses real gaps · **FIXED (1f8eefd)** |
| BUG-3 | **P1** | RAG correctness (BE) | In-document facts answered "I don't have that information" on short multi-topic docs |
| BUG-4 | **P1** | Latency + quota (BE) | Every no-context message fires a 2nd LLM call — now correct (1f8eefd), still a second call; merging it is open |
| BUG-5 | **P1** | Perceived latency (BE) | Cold-start first question ~7.5s; model + index load lazily, no pre-warm · **FIXED (dfd4213)** |
| BUG-6 | **P2** | FE/BE mismatch | `.TXT`/uppercase files accepted by UI, rejected by server; whole batch fails · **FIXED (9a25bd2)** |
| BUG-7 | **P2** | UX / perceived latency (FE) | No streaming — answer shown only after full generation |
| BUG-8 | **P3** | Cosmetic (BE) | Model emits narrow-no-break spaces / non-breaking hyphens in answers · **FIXED (b3a68e5)** |

---

## The "answers are very slow" complaint — root cause

Warm answers are actually fast. Measured latency:

| Path | LLM calls | Time |
|------|-----------|------|
| Answerable from docs (warm) | 1 | 0.4–0.8s |
| Greeting / off-topic / miss (warm) | **2** | 0.8–1.2s |
| **First question after start/deploy (cold)** | 1 | **~7.5s** |

Slowness comes from four things, not the model being slow per token:

1. **Cold start (BUG-5).** The SentenceTransformer embedding model and FAISS index load lazily on the *first* request (~7.5s measured). On free hosting the disk is wiped every deploy, so the first real customer after each deploy eats this. This is the single most likely source of "very slow".
2. **Wasted second LLM call (BUG-4).** Any message that retrieval doesn't cover makes an extra classify call (~0.4s + a request against the shared Groq free-tier quota) that currently returns garbage.
3. **No streaming (BUG-7).** The customer sees typing dots until the *entire* answer is generated, then it appears at once. Token streaming would make the same latency feel much faster.
4. **Rate-limit retries.** On Groq's free tier a 429 triggers `_complete_with_retry` sleeps (up to 8s each, up to 5 attempts). Under load a single answer can stall many seconds. This is expected behaviour but compounds the perception.

---

## BUG-1 — Open redirect on login `next` param  ·  P0 · Security  ·  **FIXED (df961e2)**

> Fixed by sharing one `_safe_path` guard between the login route and `_safe_back`,
> which also closed the `/\` case `_safe_back` had missed. Verified by attacking a
> running instance before and after, and by four new checks in the security section
> that were confirmed to fail against the previous code. Suite 134 -> 138.

**File:** [app.py:319-320](app.py#L319-L320)

```python
nxt = request.args.get("next", "")
return redirect(nxt if nxt.startswith("/") else url_for("dashboard"))
```

`//evil.com` starts with `/`, so it passes the check — but a browser reads `//evil.com` as a protocol-relative URL and navigates off-site.

**Live proof:**
```
POST /login?next=//evil.com   →   302 Location: http://evil.com/
```

This is a phishing primitive on an authenticated route ("you were just on our site"). Note the app already has a correct guard in `_safe_back` ([app.py:383](app.py#L383)) — the login path just doesn't use the same rule.

**Fix:** reject `//` (and `/\`, which some browsers normalize to `//`). Reuse the `_safe_back` logic:

```python
nxt = request.args.get("next", "")
safe = nxt.startswith("/") and not nxt.startswith(("//", "/\\"))
return redirect(nxt if safe else url_for("dashboard"))
```

`https:/evil.com` already falls through correctly (doesn't start with `/`) — verified.

---

## BUG-2 — Classifier always returns CHAT; real gaps never logged  ·  P0 · Core feature  ·  **FIXED (1f8eefd)**

> The classify call now goes through `_complete_with_retry`, which carries the per-model
> token budget (`_model_kwargs`) and honours Retry-After — it also had no retry, so the
> same 429 burst the answer survived silently returned CHAT here. Verified live: parking →
> QUESTION, hi → CHAT, capital of France → OFFTOPIC. Three GAP cases in `eval.py` assert
> outcome NOANSWER / answered=False; e2e asks the bookshop about parking and checks
> `/dashboard/gaps`. Suite 41 → 44 cases. The fallback now logs when it runs.

**Files:** [rag.py:268-288](rag.py#L268-L288) (`_classify_message`), [rag.py:473-478](rag.py#L473-L478) (`ask`)

`_classify_message` calls the LLM with `max_tokens=5`. But `GROQ_MODEL` is `openai/gpt-oss-20b`, a **reasoning** model — it spends the completion budget on hidden reasoning tokens *before* the visible answer. With only 5 tokens it never reaches the label.

**Live proof:**
```python
create(model=gpt-oss-20b, max_tokens=5, ...)
  → content=''   finish_reason='length'   reasoning='We need'
```
Every classification therefore hits the `except`/fallback and returns `CHAT`:
```
'is there parking available'  -> CHAT
'can I book a table'          -> CHAT
'do you have wifi'            -> CHAT
```

**Impact.** In `ask()`, when retrieval finds nothing above `MIN_SIMILARITY` (0.20), the message is classified to decide if it's a real gap. Because the classifier always says CHAT, `is_gap` is always False → `record_unanswered` is never called for these. **Real unanswered business questions that also score below 0.20 silently vanish from the Unanswered dashboard** — the owner's entire to-do list is incomplete.

Confirmed live: "is there parking" (a genuine business question, score 0.165) was answered "I don't have that information" but **never appeared in the gaps table**, while "gluten free crust" (score 0.349, above threshold, so it took the other branch) *was* logged. The dashboard is unreliable exactly for the low-similarity questions it most needs to catch.

The customer-facing answer is unaffected (the no-context prompt still produces a correct "I don't have that" reply) — this is an analytics/feature-correctness bug, not a wrong answer.

**Fix (pick one):**
- Give the classify call room for the reasoning model. It already goes through the raw SDK, so mirror `_model_kwargs`: for gpt-oss send `max_tokens≈300, reasoning_effort="low"` instead of `max_tokens=5`. Verify `content` is non-empty and the label parses.
- **Better:** fold classification into the single main no-context call by asking for a trailing marker (as the with-context branch already does via `MARKER_WITH_CONTEXT`), and delete the separate call entirely. This also fixes BUG-4. The code comment at [rag.py:241-245](rag.py#L241-L245) says a combined call was unreliable *on this model in one-shot answer+label*, but a dedicated classify-only marker in the same call is cheaper to make reliable than a second network round-trip. Re-measure with an eval before committing.

Add a regression test that asserts `_classify_message` returns `QUESTION` for a clear business question — the current suite skips all classifier assertions and never caught this.

---

## BUG-3 — In-document facts answered "I don't have that information"  ·  P1 · RAG correctness

**Files:** [ingest.py:70-85](ingest.py#L70-L85) (`chunk_text`), [rag.py:51](rag.py#L51) (`MIN_SIMILARITY`), [rag.py:438](rag.py#L438)

**Live proof.** Onboarded a café whose FAQ literally contains `Wifi:\nFree wifi, password COFFEE123.`:
```
"what is the wifi password"  →  "I'm sorry, but I don't have that information..."
retrieval top score = 0.071   (MIN_SIMILARITY = 0.20)
```

**Cause.** `chunk_text` merges blank-line-separated paragraphs up to 600 chars. A small FAQ (hours + coffee + wifi ≈ 150 chars) collapses into **one** chunk. Its single embedding is an average of three unrelated topics, so a narrow query ("wifi password") has low cosine similarity against the blended vector and lands under the flat 0.20 threshold → treated as no-context. The bot denies knowing something it was explicitly told.

This hits small businesses hardest — exactly the target user — because their whole FAQ fits in one chunk.

**Fix options (combine):**
- Chunk more granularly for small docs: split on single newlines / headings so each `Topic:` section becomes its own chunk, or cap much smaller than 600 chars when the doc is short.
- Retrieve top-K and let the LLM see them even at moderate scores; lower/adaptive `MIN_SIMILARITY`, or keep a chunk if *any* of the top-K clears a lower bar. A flat 0.20 on averaged chunks is the brittle part.
- Add an eval case: a fact that appears verbatim in a short multi-topic doc must be answered with a citation.

---

## BUG-4 — Wasted second LLM call on every no-context message  ·  P1 · Latency + quota

**Files:** [rag.py:474](rag.py#L474), [rag.py:268-288](rag.py#L268-L288)

Every greeting, off-topic message, and retrieval miss makes **two** LLM calls: the answer, then `_classify_message`. Measured: the classify call adds ~0.4s and one request against the Groq free-tier key that **all tenants share** (see the quota note at [db.py:441-452](db.py#L441-L452)). Today that second call is also broken (BUG-2), so it is pure cost for zero value.

**Fix:** merge classification into the main no-context call via a trailing marker (see BUG-2, option 2). Halves calls on the small-talk/miss path and removes the wasted round-trip.

---

## BUG-5 — Cold-start first-question latency (~7.5s)  ·  P1 · Perceived latency  ·  **FIXED (dfd4213)**

> `app.py` starts a daemon thread after `init_db()` that loads the embedder; `_get_embedder`
> is double-checked under a lock so the warm-up and an early request cannot both construct
> the model. `/healthz` reports `embedder_ready`. Measured after the change: model resident
> before the first request; first chat answer 1.58s including the LLM round trip.

**Files:** [rag.py:74-78](rag.py#L74-L78) (`_get_embedder`, lazy), [rag.py:121](rag.py#L121) (`_load_index`)

The embedding model and FAISS index load on the *first* chat request, not at boot. Measured cold: **7.48s**; warm: **0.5s**. On free hosting the disk is wiped each deploy, so the first customer after every deploy pays full freight, and any scale-to-zero cold container repeats it.

**Fix:** pre-warm at startup — after `db.init_db()` in [app.py:113](app.py#L113), call `rag._get_embedder()` (and optionally build/load the demo index) in a background thread so boot isn't blocked but the model is resident before the first customer. Optionally add a tiny warm-up to `/healthz` so the platform's health check triggers the load.

---

## BUG-6 — `.TXT` / uppercase files: UI accepts, server rejects  ·  P2 · FE/BE mismatch  ·  **FIXED (9a25bd2)**

> `_read_upload` compares the extension case-insensitively and stores it lower-cased. Two
> e2e checks upload `PARKING.TXT` and confirm it is saved as `PARKING.txt`.

**Files:** [static/upload.js:25-30](static/upload.js#L25-L30) (`isAccepted`, case-insensitive), [app.py:823-825](app.py#L823-L825) (`_read_upload`, case-sensitive)

`upload.js` marks a staged file valid if its name lowercased ends in `.txt`, so `PARKING.TXT` shows as accepted (no "not a .txt" tag). The server's `_read_upload` does `secure_filename(...).endswith(".txt")` — case-sensitive — and rejects it. Because `_save_documents` validates the whole batch before saving, **one uppercase file fails the entire upload**.

**Live proof:**
```
Upload menu.txt + PARKING.TXT  →  "Only .txt files are supported — 'PARKING.TXT' isn't one."
```
Also inconsistent internally: the *title/paste* path auto-appends `.txt` and normalizes ([app.py:846-847](app.py#L846-L847)), while the *file* path rejects — same content, different rule.

**Fix:** lower-case the extension check in `_read_upload` (`filename.lower().endswith(".txt")`), matching the UI and the title path.

---

## BUG-7 — No streaming; answer appears only when fully generated  ·  P2 · UX

**Files:** [static/script.js:72-83](static/script.js#L72-L83) (single `fetch`, awaits full JSON), [app.py:682-689](app.py#L682-L689), [rag.py:410-417](rag.py#L410-L417)

The client posts and waits for the complete answer; typing dots show until then. With a reasoning model this is the dominant *perceived* slowness even when wall-clock is modest.

**Fix (larger, optional):** stream tokens (Groq supports `stream=True`; serve via SSE/chunked and append to the bubble). Even without full streaming, showing a partial/"thinking…" state or trimming the answer path helps. Lower risk quick win: ship BUG-5 pre-warm first.

---

## BUG-8 — Model emits narrow-no-break spaces / non-breaking hyphens  ·  P3 · Cosmetic  ·  **FIXED (b3a68e5)**

> `rag.normalize_text` maps no-break/thin/narrow spaces, hyphen variants and curly quotes to
> ASCII; `_split_outcome` applies it once where the model's text enters the system. Unit
> tests cover the three characters that had failed eval; the red e2e check is green.

**Observed in live answers:** `7 pm`, `30‑ 40 minutes`, `₹459`.

Renders fine in a browser, but: (a) it broke the one e2e check `answers from docs` — the answer contained `7 pm` with ` `, so the literal substring `"7 pm"` didn't match (this is the "1 FAILED" in the suite, an application-side artifact, not a test bug per se); (b) it can break copy/paste, downstream string matching, and CSV export.

**Fix:** normalize the model output in `_split_outcome` / before returning — replace ` `/` ` with a normal space and `‑` with `-`. Cheap and removes a class of surprises.

---

## Not bugs (verified working)

- CSRF protection, per-session tokens, wrong/cross-session token rejection — solid.
- Login throttle (per-account + per-IP, progressive backoff, no thread-blocking sleep) — solid.
- Tenant isolation (dashboard/docs/gaps all scoped to session user) — solid.
- Chat rate limiting (per-IP + per-tenant, DB-backed) — solid.
- Logo upload validation (byte sniffing, dimension parse, SVG/script rejection) — solid.
- Path traversal on document delete — blocked.
- XSS in company name/tagline — escaped by Jinja autoescaping.
- Password reset / email verification token lifecycle (single-use, expiry, no user enumeration) — solid.
- `SECRET_KEY` / `SESSION_COOKIE_SECURE` / HSTS / `ProxyFix` production hardening — solid.

---

## Suggested fix order

1. **BUG-1** (open redirect) — 3-line security fix, ship immediately.
2. **BUG-2** (classifier) — restore the Unanswered dashboard; add the missing test.
3. **BUG-5** (pre-warm) — biggest win on the "slow" complaint, low risk.
4. **BUG-4** (merge the second call) — do together with BUG-2.
5. **BUG-6** (extension case) — one-line parity fix.
6. **BUG-3** (chunking/threshold) — needs an eval pass to tune safely.
7. **BUG-7 / BUG-8** — polish.
