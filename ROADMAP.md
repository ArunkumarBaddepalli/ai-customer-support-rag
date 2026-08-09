# Roadmap

What's built, and what's planned next. Feasibility notes are kept here so the
reasoning behind each decision isn't lost.

---

## Shipped

| Area | Status |
|---|---|
| Accounts — signup, login, logout, change email/password | ✅ |
| Onboarding wizard — branding + first document | ✅ |
| Dashboard — add/delete documents, instant re-index | ✅ |
| Settings — name, tagline, logo image upload, brand colour, support contact | ✅ |
| Chat history — what customers actually asked | ✅ |
| Public bot — branded chat page at `/c/<slug>` | ✅ |
| RAG engine — per-tenant chunking, FAISS index, Groq LLM | ✅ |
| Source citation — only when the model says it used the context | ✅ |
| Behaviour routing — small talk, abuse, off-topic, unknown | ✅ |
| Multi-tenant isolation — data, files and access, verified by test | ✅ |
| Postgres storage — signups, documents and logos survive redeploys | ✅ |
| Email verification + password reset, delivering to any address via Brevo | ✅ |
| Progressive login throttle (per-account + per-IP, rejects rather than sleeps) | ✅ |
| CSRF tokens on every state-changing form | ✅ |
| Chat rate limiting — per-IP and per-tenant, before any LLM call | ✅ |
| Session cookie flags, security headers, fail-loud `SECRET_KEY` | ✅ |
| Multi-file document upload, validated as an atomic batch | ✅ |
| Test suite — 41 eval cases + 115 end-to-end checks | ✅ |

---

## Planned

### 1. Embed widget — *next up*

Let a business drop the bot onto their own site with one line, instead of
sending customers to a separate link:

```html
<script src="https://yourapp.com/embed.js" data-bot="pizza-palace"></script>
```

A floating bubble appears bottom-right; clicking it opens the existing chat page
in an iframe.

**Pieces:** `embed.js` (launcher + iframe injector), a compact `/c/<slug>/widget`
page, and relaxing frame headers on that one route only.

**Why an iframe:** the host page's CSS can't break the widget and the widget's
can't leak into their page. It also avoids CORS entirely, since the iframe is
served from our own origin.

**Complexity: low.** ~150 lines, nothing in the RAG layer changes. Main risk is
cosmetic — the bubble colliding with something on an unusual site.

---

### 2. Website crawler — *removes the blank-page problem*

Instead of asking a non-technical owner to write documentation, take their URL,
read their site, and **draft** the documents for them to review.

**Flow:** enter URL → fetch same-domain pages (bounded) → strip nav/footer/scripts
→ extract main text → present as editable drafts → owner approves → indexed.

**Framing matters:** the output is a *draft the owner reviews*, never content
silently indexed. Crawled marketing copy usually answers worse than a short
curated FAQ, because homepage prose is persuasion, not facts. The crawler's job
is to remove the blank page, not to replace human review.

**Complexity: moderate.** The code is small; the failure modes are the hard part:

| Risk | Mitigation |
|---|---|
| **JS-rendered sites** (Wix, Shopify, React) return an empty HTML shell — a plain server fetch gets nothing | Detect near-empty extractions and tell the user plainly rather than silently indexing nothing. A headless browser would fix it properly but won't fit free-tier hosting |
| **Boilerplate pollution** — nav, footers, cookie banners get indexed and drag answer quality down | Main-content extraction; drop very short and repeated blocks |
| **SSRF** — the user supplies a URL and *our server* fetches it, so `http://169.254.169.254/` could expose cloud credentials | Resolve DNS first, then block loopback/private/link-local ranges; http(s) only; no redirects to private hosts; timeouts and size caps. **Non-negotiable before this ships** |
| **Staleness** — their prices change, our index doesn't | Show last-crawled date; offer re-crawl; schedule it later |
| **Politeness / legality** | Respect `robots.txt`, rate-limit, identify our user-agent, cap page count |

**Needs:** `httpx` (already installed) plus a small HTML extractor.

---

### 3. Confidence-scored answers — *pick the best answer, and say how sure it is*

Today retrieval takes the top chunks and a single similarity threshold decides
answer-vs-refuse. That's binary and blunt.

**Planned:** score candidate answers by probability and use that score properly —
choose the strongest supported answer rather than just the nearest chunk, and let
confidence drive behaviour instead of one hard cutoff.

**Ideas to evaluate, cheapest first:**

- **Rerank retrieved chunks** with a cross-encoder before generation. Embedding
  similarity answers "is this text similar?", not "does this answer the
  question" — reranking targets the second, and is usually the single biggest
  retrieval quality win.
- **Use the model's token log-probabilities** as a confidence signal on the
  generated answer, rather than relying only on retrieval distance.
- **Graduated behaviour instead of one threshold:** high confidence → answer;
  medium → answer but flag it as uncertain and offer the support contact; low →
  refuse. Right now medium and low behave identically.
- **Surface confidence in the dashboard**, not necessarily to the customer — the
  owner seeing "these 12 questions were answered with low confidence" is a
  direct, prioritised list of what documentation to write next. That pairs well
  with the existing chat-history page.

**Complexity: moderate.** A cross-encoder adds a second model and latency, so it
needs measuring, not assuming. Every change here must be validated against
`eval.py` — the suite exists precisely so retrieval changes can be proven rather
than eyeballed, and it has already caught one "improvement" that wasn't.

---

## Also worth doing

- **Conversation memory** — each message is currently handled independently, so
  follow-ups ("how much?" after "do you have Margherita?") don't resolve.
- **Index cache is single-worker** — `rag._indexes` has no lock and never
  evicts. Correct for one gunicorn worker; two would race on a cold rebuild,
  and the cache grows without bound as tenants accumulate. Needs a per-tenant
  build lock, an LRU bound, and an index version on the tenant row so
  invalidation doesn't depend on which process handled the upload.
- **No timeout on the LLM client** — a hung call holds a thread until
  gunicorn's 120s.
- **Background indexing** — re-indexing happens in-process on upload and
  re-embeds the whole corpus, not just what changed. Won't hold up at real
  document volumes.
- **No pruning** on `tokens`, `login_attempts` or `rate_limits` — rows
  accumulate forever. Harmless at small scale, worth a periodic cleanup before
  real traffic.
- **Verification gates nothing** — addresses are confirmed and the dashboard
  says so, but no route checks the flag. The right gate is the *public bot*,
  not the dashboard: locking an owner out of their own workspace over an email
  they may never receive is worse than the problem it solves.
- **No CI** — both suites are run by hand, which is how a test asserting
  against a path from an old machine survived for weeks, silently failing and
  taking a path-traversal assertion down with it.
- **Unpinned dependencies** — builds aren't reproducible; a minor release can
  change behaviour between two deploys of identical source.
- **`print()` instead of logging** — no levels, no timestamps, no request
  correlation.
- **Neon has leftover test data** — signups from earlier testing
  (`postfix-a2e3d3@probe.test`, `live-3768d1@probe.test`, `neon-test-843fdf`,
  and the `acme-books`/`zen-spa` pairs from E2E runs) are still in the
  production database alongside the real pizza-palace demo. Harmless, but
  worth deleting before treating the DB as real production data.

See [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) for the full audit these
came from, including the reasoning and the order to take them in.
