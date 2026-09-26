# Production Readiness Audit — SupportBot (ai-customer-support-rag)

**Date:** 2026-09-26
**Branch audited:** `improvements` (12 commits ahead of `main`, pushed today)
**Live instance audited:** https://ai-customer-support-rag-mpki.onrender.com (running `main`)
**Goal:** a live link that a first-time visitor can open, sign up on, upload documents to, and ask questions of — with no failures, no visible slowness, and answers that hold up for *any* kind of business.
**Status:** findings and proposals only. Nothing has been changed.

---

## 1. How this was tested

Everything below was exercised against a running app, not only read from source.

| What | How |
|---|---|
| **Live production** | Cold-start timing, health, 4 chat questions, security headers, signup page |
| **Local, `improvements` branch** | Booted `python app.py`, ran every flow over HTTP with cookie jars |
| **Mock enterprises, 4 industries** | Scripted signup → onboarding → document upload → 5–9 questions each → Unanswered dashboard. Medical clinic, real-estate agency, e-commerce store, gym. Real FAQ documents (2–4 KB), 28 questions total |
| **UI** | Headless-Chrome screenshots of landing, signup, login, onboarding, dashboard, settings, unanswered, chat — desktop (1280px) and phone (390px) |
| **Retrieval internals** | Direct calls to `rag.search`, `build_prompt`, `ingest.chunk_text`, embedding cosine scores — to measure, not guess |
| **Provider limits** | Groq rate-limit headers read directly; a 10-call burst; per-prompt token cost measured |
| **Suites** | `tests/test_units.py` 44/44 · `eval.py` 43/44 · `tests/e2e.py` 142/142 |

Prior audits ([BUGFIX.md](BUGFIX.md), [IMPROVEMENT_PLAN.md](IMPROVEMENT_PLAN.md), [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)) were read and re-verified; items already fixed on `improvements` are not repeated here.

---

## 2. Executive summary — the five things that decide whether the link is safe to send

| # | Finding | Why it matters to a first-time visitor |
|---|---|---|
| **1** | **Live site cold-starts in >90 seconds.** Render free tier spins down after 15 min idle; the first visitor waits 1.5+ min on a blank page. Measured: `/healthz` timed out at 90s, then answered. | They will close the tab before it loads. |
| **2** | **Answers slow to 6–10 s after ~8 questions.** The whole platform shares one Groq free-tier key: 8,000 tokens/min, refilling at ~133 tokens/s. One answer costs ~1,000 tokens, so steady-state is **one answer every ~7 s, across all tenants**. Measured: first 9 answers 0.5–1.0 s, next 19 answers 6.1–9.7 s. This is the "very very slow" you see. | Their 5th question hangs. Looks broken. |
| **3** | **Live site ran code from Aug 21 and auto-deploy is dead.** *Update 2026-09-26 17:23 UTC: `main` deployed manually; open redirect verified closed on live.* The 12 `improvements` fixes are still not live, and pushes still won't auto-deploy until the GitHub link is reconnected. See PROD-2, PROD-8. | Fixed for the redirect; the rest waits on the merge. |
| **4** | **Live demo bot cites a gym document.** `/c/pizza-palace` answers a pizza question with `sources: ["faq.txt", "nova-fitness-faq.txt"]`. Two causes: a fitness FAQ was uploaded into the pizza tenant on prod, *and* the app cites every retrieved file, not the one used (reproduced locally on a clean 2-doc tenant). | First impression: "the citations are wrong." |
| **5** | ~~Signups on live never get their verification email.~~ **Fixed 2026-09-26** — Brevo configured on Render; live reports `mail: brevo`. | Emails deliver. |

Also: [README.md:10](README.md#L10) says *"the hosted demo is offline — the free-tier deployment lapsed"* while the demo is up. A reader who checks will trust nothing else in the README.

**Answer quality itself is good.** 28/28 questions across four unrelated industries were handled correctly (facts cited, gaps refused *and* logged, off-topic and abuse handled). The product works; the deployment and the provider budget are what fail.

---

## 3. The "very very slow" — root cause, measured

Three distinct causes, in order of impact:

### 3.1 Groq free-tier token budget (the steady-state slowness)

```
x-ratelimit-limit-tokens:     8000        (per minute)
x-ratelimit-remaining-tokens: 399         (mid-test)
x-ratelimit-reset-tokens:     57s
```

The bucket refills continuously at 8000/60 ≈ **133 tokens per second**. Measured cost per answer:

| Prompt | Size | Tokens (≈chars/4) |
|---|---|---|
| With-context answer | 4,457 chars | **~1,114** — of which rule boilerplate 2,689 chars (60%), retrieved context 1,768 chars |
| No-context answer | 1,769 chars | ~442 |
| Classifier (second call on every miss/greeting) | 731 chars | ~182 |

1,114 tokens ÷ 133 tokens/s ≈ **8.4 s of refill per answer**. Mock-bot timings once the bucket emptied: 7.65, 8.64, 8.40, 7.68, 8.73, 8.67, 7.70, 9.71, 8.72, 7.78, 8.54, 8.73, 8.65 s — the refill time, exactly. The retry loop in `_complete_with_retry` sleeps on `Retry-After` and **logs nothing**, so this never appears in any log.

**Levers, cheapest first:**

| Lever | Effect on tokens/answer | Effort |
|---|---|---|
| Trim the rule text in `build_prompt` (currently 2,689 chars; half is restating the same rule in different words) | −300 to −400 | S |
| `TOP_K` 4 → 3, and drop any chunk below 0.25 when the top one is above 0.40 (the 4th chunk scored 0.21 in the sample) | −100 to −200 | S |
| Fold the classifier into the no-context prompt via a trailing marker (removes the 2nd call on every greeting/off-topic/miss) | −182 per miss | M — measure with `eval.py` |
| Cache identical `(tenant, normalised question)` answers for 60 s — FAQ bots get the same 10 questions | −100% on repeats | S |
| Log every retry with the wait — makes the next slowdown diagnosable in one grep | 0, observability | S |
| Fallback to a second *free* provider (Cerebras / Gemini) on 429 — pluggable OpenAI-compatible client | second independent bucket | M — see §8 |
| Classifier via the local embedding model instead of an LLM call | −182 per miss, zero quota | S–M |

Trim + TOP_K + cache alone takes an answer from ~1,100 to ~650 tokens: steady-state **one answer per ~5 s → per ~2 s**, and a demo of 10 questions never hits the ceiling at all.

### 3.2 Render free-tier cold start (the "blank page for a minute")

Container spin-down after 15 min idle; a cold boot pulls the image, starts gunicorn, imports torch + sentence-transformers. Measured **>90 s**. Warm, the landing page answers in 0.29 s.

**Options:** (a) Render paid instance — no spin-down; (b) an external uptime pinger hitting `/healthz` every 5–10 min (free, keeps it warm); (c) accept it and say so on the landing page. Paid services are ruled out, so the plan is (b) — see §8 for the hour-budget caveat.

### 3.3 Old code on live (the 8 s first answer)

`main` has no pre-warm; the first chat after boot loads the embedder on the request thread (8.06 s measured live). Fixed on `improvements` (`dfd4213`) — but note the pre-warm only *loads* the model; the first `encode()` still pays ~5 s (first mock tenant's index build took 6.3 s, the rest 0.7 s). Warm with a dummy `encode(["warm"])` too.

---

## 4. Test results

### 4.1 Mock enterprises — four industries, one product

Each was created through the real UI flow: signup → onboarding (branding + file upload) → questions on the public chat page → Unanswered dashboard read back.

| Enterprise | Doc | Facts | Gap logged? | Off-topic | Chat/abuse | Latency (bucket full → empty) |
|---|---|---|---|---|---|---|
| Riverside Clinic (medical) | 4,061 ch | 5/5 cited | ✅ "do you do dental work" | ✅ refused | ✅ | 0.50–1.02 s |
| Skyline Realty (real estate) | 2,616 ch | 5/5 cited | ✅ "properties in Mumbai" | ✅ refused | ✅ | 3.85–8.73 s |
| PixelCart Store (e-commerce) | 2,428 ch | 4/4 cited | ✅ "do you sell furniture" | ✅ refused | — | 2.03–9.71 s |
| Northgate Gym (fitness) | 2,232 ch | 3/3 cited | ✅ "swimming pool" | — | ✅ "you are useless" handled calmly | 5.98–8.73 s |

**28/28 behaviourally correct.** Every fact answer cited its document; every gap was refused with the configured contact *and* appeared in the owner's Unanswered list; every off-topic question was refused without leaking world knowledge; insults were absorbed. The classifier fix (`1f8eefd`) is doing its job — all four gaps reached the dashboard.

One nuance: "I want to speak to a person" at the clinic returned the doc's own contact (`hello@…`) rather than the Settings contact (`help@…`) and cited the doc. Harmless when they agree; confusing if an owner's Settings contact differs from what their document says. See BE-9.

Follow-up questions ("and in Whitefield?", "how long does that take?") worked — **by retrieval luck**, because the follow-up carried a keyword. There is no conversation memory; "and the second one?" has nothing to retrieve on. See BE-6.

### 4.2 Suites

| Suite | Result |
|---|---|
| `tests/test_units.py` | **44 passed** (3.5 s) |
| `eval.py` (44 cases, Pizza Palace) | **43 passed, 1 failed (97.7%)** — "How do I contact support?" answered correctly but self-labelled NOANSWER, so it was filed as a gap. See BE-17 |
| `tests/e2e.py` (16 sections) | **142 passed, 0 failed** — visitor, signup, onboarding, dashboard, customer, edge, settings, profile, gaps, isolation, auth, throttle, rate-limit, CSRF, reliability, security |

---

## 5. Bugs and gaps

Severity: **P0** blocks sharing the link · **P1** a visitor would notice · **P2** should fix · **P3** polish.

### 5.1 Production / deployment

| ID | Sev | Finding | Evidence | Fix |
|---|---|---|---|---|
| PROD-1 | **P0** | Cold start >90 s on Render free tier | `/healthz` timeout at 90 s, then 200 | Keep-warm pinger (§8); trial `fastembed` to shorten the boot itself |
| PROD-2 | **P0** | Live runs `main`; the 12 fixes aren't deployed | live `/healthz` lacks `embedder_ready`; first chat 8.06 s | Merge `improvements` → `main` (triggers CI + Render deploy) |
| PROD-3 | **P0** | Verification emails don't deliver on live | live `mail: resend`; local `.env` has Brevo | **FIXED 2026-09-26** — Brevo env set on Render, Resend removed; live `/healthz` now `mail: brevo` |
| PROD-4 | **P1** | Pizza Palace demo tenant contains `nova-fitness-faq.txt` | live `sources` on a pizza question | Delete it from the prod tenant (dashboard → Documents → Delete) |
| PROD-5 | **P1** | Shared 8K-TPM Groq key for every tenant | headers above | §3.1 levers + second free provider fallback (§8) |
| PROD-6 | **P2** | CI only fires on `main` and PRs — `improvements` has never had a CI run | `ci.yml` `on.push.branches: [main]` | Open the PR (CI runs), or add `improvements` to the trigger |
| PROD-7 | **P2** | README claims demo is offline | [README.md:10](README.md#L10) | Delete the sentence; link the live URL |
| PROD-8 | **P0** | **Auto-deploy is dead and the open redirect is live.** Render's event log shows no build since 2026-08-21; the deployed commit `d1a612b` is an orphan (`main` was rebased afterwards). Four pushes to `main` on Sep 7–8, including the open-redirect fix `e750752`, never deployed. Verified on the hosted site: `POST /login?next=//evil.com` → `302 https://evil.com/` | Render API: last event 2026-08-21T12:19; `git branch --contains d1a612b` → none | **PARTLY FIXED 2026-09-26** — `main` (`e750752`) deployed manually at 17:23 UTC; verified live: `login?next=//evil.com` → `/dashboard`. **Still open:** the GitHub→Render link is dead — reconnect in Render → Settings → Build & Deploy (browser OAuth, owner only). **Mitigated on `improvements`:** `ci.yml` now has a `deploy` job that calls Render's Deploy Hook after the test job passes on `main`; needs the `RENDER_DEPLOY_HOOK` repository secret (Render → service → Settings → Deploy Hook) — until it's set the job skips with a message |
| PROD-9 | **P3** | Two free web services on the workspace (`ai-customer-support-rag`, `vera-bot`, idle since May) share Render's 750 free hours/month | Render API | **FIXED 2026-09-26** — `vera-bot` suspended; `ADMIN_PASSWORD` removed |

### 5.2 Backend

| ID | Sev | Finding | Evidence | Fix |
|---|---|---|---|---|
| BE-1 | **P1** | **Citations list every retrieved file, not the one used.** `sources = sorted({r["source"] for r in results})` over all TOP_K | 2-doc tenant, pizza question → cites gym file (local + live) | Ask the model to name the source it used (it already emits an outcome label — add `SOURCE: <file>` on the same trailing line), or cite only files whose chunk score ≥ top − 0.1 |
| BE-2 | **P1** | **Chunker merges 2–4 unrelated topics per chunk** (600-char merge). Every sample doc affected: florist 13 topics → 4 chunks. A narrow question scores against a blended embedding | "wifi password" on a 3-topic doc: **0.049 → refused**; same doc, one chunk per topic: **0.638 → answered** | `chunk_text`: one chunk per blank-line paragraph; merge only when a paragraph is < ~80 chars; hard-split only above 600. Gate with `eval.py` |
| BE-3 | **P1** | Retry loop is silent — the exact slowness you're chasing leaves no trace | `_complete_with_retry`: only `time.sleep(wait)` | `print`/log `[rag] 429 — waiting {wait}s (attempt n)` |
| BE-4 | **P1** | Prompt boilerplate is 60% of every request's token cost | 2,689 of 4,457 chars | Rewrite `build_prompt` rules tersely; target ≤ 1,200 chars |
| BE-5 | **P2** | Second LLM call on every greeting/off-topic/miss (classifier) — correct now, still doubles cost and latency on that path | `ask()` → `_classify_message` | Trailing marker in the no-context prompt; measure with eval before switching |
| BE-6 | **P2** | No conversation memory | code: each request independent | Keep the last 3 turns per browser session in the request body (client-held, never stored — consistent with "conversations are never stored"); include them in the prompt as "Earlier in this chat:" |
| BE-7 | **P2** | No answer cache — identical FAQ questions re-hit the LLM | — | 60-s in-process LRU on `(tenant_id, index_version, normalised question)` |
| BE-8 | **P2** | Pre-warm loads the model but not the first `encode()` | first tenant index build 6.3 s vs 0.7 s after | `_prewarm`: `rag._get_embedder().encode(["warm"])` |
| BE-9 | **P3** | Contact requests answer from the document rather than the configured support contact | clinic test | Either prefer Settings contact in rule 5b, or surface the doc contact in Settings as a hint |
| BE-10 | **P2** | No 500 handler — an exception on a branded customer page returns Flask's default | `errorhandler`: 404, 413 only | Add one rendering the styled error page; log the traceback |
| BE-11 | **P3** | 413 returns bare text | [app.py:926](app.py#L926) | Render the error template |
| BE-12 | **P2** | `.txt` only | `_read_upload` | `.md` (trivial), `.pdf` via `pypdf`, `.docx` via `python-docx` — most businesses have their FAQ as a PDF |
| BE-13 | **P3** | Unanswered list capped at 100, no paging | [db.py:745](db.py#L745) | "Showing 100 of N" + offset |
| BE-14 | **P3** | No `robots.txt` | — | Allow `/`, disallow `/c/`, `/dashboard` |
| BE-15 | **P3** | `print()` throughout; no request id, no levels | 15 call sites | `logging` with a per-request id |
| BE-16 | **P3** | No account deletion / data export | — | Password-confirmed delete; JSON export (schema already cascades) |
| BE-17 | **P3** | **Correct answers sometimes self-labelled NOANSWER → false gap entries.** The model answered "How do I contact support?" from the doc, then wrote NOANSWER; the owner's Unanswered list gains a question that *was* answered | `eval.py` 43/44; commit `2c6f488` already narrowed this once | When outcome is NOANSWER but the reply contains a fact from a retrieved chunk (phone/email/price match), treat as ANSWERED; or add a "was that answered? y/n" toggle on the gaps page so owners can clear false ones |

### 5.3 Frontend / UI

| ID | Sev | Finding | Evidence | Fix |
|---|---|---|---|---|
| FE-1 | **P1** | **Dashboard overflows horizontally on a phone** — nav cut off, banner text clipped, Save button past the edge | 390 px screenshot | `app.css` has no dashboard breakpoints (only `prefers-reduced-motion`). Add `@media (max-width: 600px)`: `.wrap` padding 16px, `.topbar` padding 12px 16px, `.card` padding 18px, `.verify-banner` `flex-wrap: wrap`, `.tabs` scrollable |
| FE-2 | **P1** | **Support phone input is unstyled** on onboarding and settings — narrow, browser default border, next to fully styled inputs | screenshots; [app.css:662](static/app.css#L662) selector lists `text, email, password, file` but not `tel` | Add `input[type="tel"]` to the selector |
| FE-3 | **P1** | **Suggested-question chips are restaurant-specific** ("timings / delivery / refund") on every tenant's bot — wrong for a clinic, a realtor, a SaaS | [chat.html:31-33](templates/chat.html#L31-L33) hardcoded | Generate from the tenant's document section headings (first 3 `Topic:` lines), or a "Suggested questions" field in Settings |
| FE-4 | **P2** | Copy is restaurant-flavoured elsewhere too: tagline placeholder "menu, timings, or delivery" ([onboarding.html:23](templates/onboarding.html#L23)); title placeholder "delivery-policy" ([dashboard.html:38](templates/dashboard.html#L38)) | — | Neutral placeholders: "Ask me about our services, hours, or pricing" / "e.g. faq, pricing, policies" |
| FE-5 | **P2** | Bot answers render as one flat paragraph — newlines and lists collapse | [script.js](static/script.js) `bubble.textContent = text` | Split on `\n` into `<br>` / `<p>`; render `- ` lines as a list. No HTML from the model, keep it escaped |
| FE-6 | **P2** | Brand colour is a full-width native colour bar; file picker is the native "Choose files / No file chosen" | onboarding + dashboard screenshots | Small swatch + hex field; styled drop-zone button (the staged list already exists) |
| FE-7 | **P2** | Mobile chat header truncates the tagline with an ellipsis | 390 px screenshot | Two-line clamp, or hide tagline under 400 px |
| FE-8 | **P3** | Bot avatar is a tiny emoji; the header uses the initial/logo — inconsistent | chat screenshot | Reuse the tenant logo/initial as the avatar |
| FE-9 | **P3** | No timestamps, no copy button, no "new conversation" on the chat page | — | Small additions; timestamps help support hand-off |
| FE-10 | **P3** | Verify-email banner on every dashboard page, full width, amber — dominates the screen | screenshots | Slimmer single-line bar, dismissible for the session |
| FE-11 | **P3** | Dashboard shows document names only — no size, chunk count, last updated, no preview | — | "faq.txt · 4.1 KB · 9 sections · updated 2 min ago" |
| FE-12 | **P2** | No way to test the bot from the dashboard — owner must open a new tab | — | "Try it" panel or embedded chat on the Documents page |
| FE-13 | **P3** | Chat page error text "Could not reach the server. Is app.py running?" is developer wording shown to customers | [script.js:89](static/script.js#L89) | "Couldn't reach the assistant — please try again" |
| FE-14 | **P3** | Chat input has no `maxlength`; server rejects at 1,000 with an error bubble | — | `maxlength="1000"` + counter |

### 5.4 Truth / docs

| ID | Sev | Finding | Fix |
|---|---|---|---|
| DOC-1 | P1 | README says demo offline (it's live) | Fix the sentence, link the URL |
| DOC-2 | P2 | ROADMAP says "41 eval cases + 134 e2e checks"; actual 44 / 142 | Update numbers |
| DOC-3 | P3 | Three overlapping planning docs at repo root (`BUGFIX.md`, `IMPROVEMENT_PLAN.md`, `IMPLEMENTATION_PLAN.md`) plus this one | Fold into a `docs/` folder with one index |

---

## 6. Improvements — making it hold for *any* business

The product is pitched as "any business". Four industries worked. What stops it being genuinely general:

### 6.1 Onboarding for any industry
- **Industry starter packs.** `sample_docs/examples/` already has clinic, diagnostics lab, florist, gym, e-store FAQs (+ the realty one written for this audit). Offer them at onboarding: "Start from a template → edit → go live." Turns a blank page into a 30-second demo for any vertical.
- **Neutral copy everywhere** (FE-3, FE-4). Chips from the tenant's own headings.
- **Multiple contacts** — sales vs support vs billing, with the bot choosing by intent. The clinic doc already has `billing@`; Settings can only hold one.
- **Business hours** in Settings → the bot can say "we're closed now, opens 8 AM" instead of just a phone number.
- **Disclaimer line per tenant** — a clinic needs "not medical advice; emergencies call 911", a law firm "not legal advice", a bank "never share your PIN". One optional Settings field, prepended to every no-context answer.
- **Accepted formats:** PDF and DOCX (BE-12). Most businesses have their FAQ as a PDF, not a `.txt`.

### 6.2 Answer quality
- Chunk per topic (BE-2) — the single biggest retrieval win, proven above.
- Cite the source actually used (BE-1).
- Conversation memory (BE-6), client-held.
- Confidence: answer / answer-with-caveat / refuse instead of one 0.20 cutoff (ROADMAP item 3). Surface low-confidence answers to the owner.
- `eval.py` cases per vertical — one doc per industry with 8–10 questions, so a retrieval change is measured across all of them, not just pizza.

### 6.3 Speed
- §3.1 in full: trim prompt, TOP_K 3, answer cache, log retries, warm `encode()`.
- Streaming (`stream=True` + SSE) so the first word appears in ~300 ms — the outcome label arrives last, so citation rendering waits for end-of-stream.

### 6.4 Owner value
- **Analytics tile** on the dashboard: questions today / answered % / top 5 gaps / average confidence. "Deflection rate" is the number that sells a support bot.
- Document metadata (FE-11), inline "Try it" (FE-12).
- Embed widget (ROADMAP item 1) — a business wants the bot on *their* site, not a link.

### 6.5 UI design direction
The visual language is already good — dark hero landing, clean indigo dashboard, branded chat. Keep it. What's needed is a **responsive pass** (FE-1, FE-7), **input consistency** (FE-2, FE-6), **generic copy** (FE-3, FE-4), and **richer answers** (FE-5). Not a redesign.

---

## 7. Recommended order

**Before sending the link to anyone (one evening):**
1. PROD-3 Brevo on Render · PROD-4 delete the gym doc from Pizza Palace · PROD-7/DOC-1 README sentence
2. Merge `improvements` → `main` (PROD-2, runs CI, redeploys)
3. BE-3 log retries · BE-4 trim prompt · TOP_K 3 · BE-7 answer cache — the slowness fix
4. FE-2 `tel` input · FE-3 neutral chips · FE-4 placeholders · FE-13 error text
5. Set up the keep-warm pinger for PROD-1 (§8)

**Next (a weekend):**
6. BE-2 chunk per topic + per-vertical eval cases
7. BE-1 accurate citations
8. FE-1 responsive pass · FE-5 answer formatting · FE-6 pickers
9. BE-10 500 handler · BE-8 warm encode · BE-12 PDF upload

**Then:** BE-6 memory · streaming · analytics tile · industry starter packs · embed widget.

Every item lands as one commit on `improvements` with a test where one applies, for review before push.

---

## 8. Free-tier plan — no paid services (constraint set 2026-09-26)

Everything must run on free tiers. Both "money" items in §3 have free answers; the result is bounded but sufficient for a demo link.

| Problem | Free fix | Expected result | Caveat |
|---|---|---|---|
| Cold start >90 s (PROD-1) | External pinger (UptimeRobot / cron-job.org) hits `/healthz` every 5–10 min so Render never spins down | Page loads instantly for visitors | Consumes ~744 of Render's 750 free hours/month — fits one service only; stops working if Render changes the rule |
| Cold-start *duration* | Replace `sentence-transformers` + torch (~180 MB, slow import) with `fastembed` (ONNX, same `all-MiniLM-L6-v2`, no torch) | Smaller image, import in seconds, less memory | Measure on a branch; `eval.py` must hold 43/44 |
| 8K TPM ceiling (PROD-5) | (a) Trim prompt + `TOP_K` 3 → ~40% fewer tokens · (b) **classifier via the local embedding model** — nearest-centroid over CHAT / OFFTOPIC / QUESTION examples — replaces the 2nd LLM call with zero quota · (c) 60-s answer cache | Bucket lasts ~15 questions instead of ~7; sustained ~3–4 s | Still one bucket platform-wide |
| Ceiling for real | Pluggable LLM provider (OpenAI-compatible base URL + key) and **fallback to a second free provider on 429** — Cerebras or Gemini both publish free tiers with far higher TPM than 8K | Two independent free buckets; a demo effectively never stalls | Not a second Groq account (against their terms). Free limits change — verify current numbers when wiring |
| Emails (PROD-3) | Brevo is free; key already in local `.env` — set on Render | Delivers | None |

**Net at zero cost:** single-user answers 0.5–1 s; a 10–15-question demo never hits a limit; site always awake. Not unlimited, but safe to send.

Sequence: pinger (5 min, do first) → Brevo env → merge → prompt trim + TOP_K + cache + embedding classifier (measured with eval) → provider fallback → fastembed trial.

---

## 9. What I need from you

1. **Render:** is auto-deploy on for `main`? Confirm you have only one free web service (the pinger plan depends on the 750-hour allowance).
2. **Second free LLM provider:** create a free Cerebras or Gemini API key (either; Cerebras is the simpler signup) and add it to Render as `LLM_FALLBACK_API_KEY` — I'll wire the fallback to whichever you pick.
3. **Prod database:** delete `nova-fitness-faq.txt` from Pizza Palace yourself, or give me the `DATABASE_URL` read-only to list what's there first.
4. **Mail on Render:** add `BREVO_API_KEY` and `MAIL_FROM` from your local `.env` (I can't see Render's env).
5. **Merge approval:** OK to open the PR `improvements → main`? That's what puts the fixes live.
6. **Demo tenants on live:** which 3 industries should be pre-seeded so a visitor sees more than pizza? I'd pick clinic, realty, e-store.
7. **Go-ahead to implement** the "before sending" list in §7 — you said doc first, so nothing has been touched.
