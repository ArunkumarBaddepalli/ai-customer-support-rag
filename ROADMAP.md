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
| Email verification + password reset via Resend | ✅ |
| Progressive login throttle (per-account + per-IP, no lockout) | ✅ |
| Test suite — 41 eval cases + 96 end-to-end checks | ✅ |

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

- **CSRF tokens** on state-changing forms (settings, document upload/delete,
  password change). `SESSION_COOKIE_SAMESITE=Lax` blocks the classic cross-site
  auto-submit vector in modern browsers, but that's not the same as real CSRF
  protection. Not done — touches every form in the app, wanted explicit sign-off
  before taking it on.
- **Conversation memory** — each message is currently handled independently, so
  follow-ups ("how much?" after "do you have Margherita?") don't resolve.
- **Background indexing** — re-indexing happens in-process on upload, which won't
  hold up at real document volumes.
- **login_attempts table has no pruning** — old throttle rows accumulate forever.
  Harmless at small scale (one row per email/IP that's ever failed a login), but
  worth a periodic cleanup before real traffic.
