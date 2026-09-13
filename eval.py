"""
Measure how accurate the bot is against a fixed set of test questions.

Every case names the *kind* of reply it expects, and the check reads the
outcome label the bot already attaches to its own answer rather than
guessing from the wording:

    FACT    - answered from the documents: the right keyword is present, the
              outcome is ANSWERED, and a source is cited.
    REFUSE  - a question the bot must not answer (off-topic, or about the
              business but not in the documents): not ANSWERED, no citation,
              and none of the phrases it must never say.
    CHAT    - small talk or abuse: replies conversationally, never with the
              "I don't know" refusal, and never with a citation.
    GAP     - a question about the business the documents do not cover:
              outcome NOANSWER and answered=False, so it reaches the owner's
              Unanswered dashboard. This is the case the classifier bug hid —
              every such question was silently filed as small talk.

Earlier versions matched only on keywords, which measured the model's
phrasing rather than its behaviour: a correct refusal worded "I can't help
with that" failed because the keyword list expected "not sure", and a correct
"peri-peri" failed because the model typed a non-breaking hyphen. Text is
normalised before matching and refusals are judged by outcome, so a passing
run means the bot did the right thing, not that it said the expected words.

Runs against the Pizza Palace demo workspace, so seed it first:
    python seed_demo.py
    python eval.py

Set EVAL_MIN_CORRECT (e.g. 40) to exit non-zero below that score — used by
CI so a quality regression fails the build instead of scrolling past.
"""

import os
import sys
import time

import db
import rag
import seed_demo

FACT, REFUSE, CHAT, GAP = "FACT", "REFUSE", "CHAT", "GAP"

# (question, keywords that must appear (any), expected kind)
TEST_CASES = [
    ("What time do you open?", ["11"], FACT),
    ("What time do you close?", ["11"], FACT),
    ("Are you open on public holidays?", ["holiday"], FACT),
    ("Does the kitchen close before the restaurant?", ["30"], FACT),
    ("What pizza flavors do you have?", ["margherita"], FACT),
    ("Do you have a paneer pizza?", ["peppy paneer"], FACT),
    ("What sizes do pizzas come in?", ["small"], FACT),
    ("What sides do you serve besides pizza?", ["garlic bread"], FACT),
    ("What dips are available?", ["peri-peri"], FACT),
    ("How much is a small Margherita?", ["149"], FACT),
    ("How much is a large Farmhouse?", ["399"], FACT),
    ("What's the price of a medium Chicken Tikka?", ["379"], FACT),
    ("How much does garlic bread cost?", ["99"], FACT),
    ("How much is a cold drink?", ["49"], FACT),
    ("What is the delivery radius?", ["7"], FACT),
    ("How long does delivery take?", ["30", "40"], FACT),
    ("Is delivery free?", ["399"], FACT),
    ("What's the delivery fee?", ["40"], FACT),
    ("Can I track my order?", ["track"], FACT),
    ("What payment methods do you accept?", ["upi"], FACT),
    ("Can I pay in installments?", ["not accepted", "no", "cannot", "can't", "don't", "do not"], FACT),
    ("My order arrived cold, what do I do?", ["30 minutes"], FACT),
    # Accepts an explicit "no" or the equivalent "refunds are only for X"
    ("Can I get a refund if I just change my mind?",
     ["cannot", "can't", "no", "not", "don't", "do not", "only"], FACT),
    ("How late can I report a damaged order?", ["2 hours"], FACT),
    ("Can I cancel my order?", ["5 minutes"], FACT),
    ("Do you have any offers?", ["tuesday"], FACT),
    ("Is there a student discount?", ["15%"], FACT),
    ("How do I contact support?", ["98765"], FACT),
    ("What are support hours?", ["10"], FACT),
    # Off-topic: must refuse rather than answer from the model's own world
    # knowledge. Judged by outcome, not by which refusal phrase it chose.
    ("Do you sell laptops?", [], REFUSE),
    ("Can I book a hotel room through you?", [], REFUSE),
    ("What is the capital of France?", [], REFUSE),
    ("Should I invest in bitcoin?", [], REFUSE),
    # About the business, absent from the FAQ, and worded nothing like it so
    # retrieval scores below the threshold. Must be logged as a gap: this is
    # the branch where the classifier always returned CHAT.
    ("Is there parking available?", [], GAP),
    ("Can I book a table for a birthday party?", [], GAP),
    ("Do you have vegan cheese?", [], GAP),
    # Small talk: must respond conversationally, NOT with the "I don't know" refusal
    ("hi", ["help", "hi", "hello"], CHAT),
    ("how are you", ["help", "good", "great", "well"], CHAT),
    ("thanks!", ["welcome", "help", "glad"], CHAT),
    ("who are you", ["assistant", "support", "help"], CHAT),
    # Abuse / frustration: must de-escalate and offer a human, never greet or argue
    ("idiot", ["98765", "support@", "help"], CHAT),
    ("this is useless", ["98765", "support@", "help"], CHAT),
    ("you are the worst bot ever", ["98765", "support@", "help"], CHAT),
    ("I want to talk to a human", ["98765", "support@"], CHAT),
]

REFUSAL_MARKERS = ("not sure about that", "don't know", "don't have that information")

# Phrases that must not appear in specific replies.
MUST_NOT_CONTAIN = {
    # Off-topic must not leak the model's own world knowledge
    "What is the capital of France?": ["paris"],
    "Do you sell laptops?": ["yes, we sell", "we sell laptops"],
    # Greeting someone who insulted you is tone-deaf; so is guessing the time of day
    "idiot": ["hello", "good morning", "good afternoon", "good evening"],
    "this is useless": ["hello,", "good morning", "good afternoon", "good evening"],
    "you are the worst bot ever": ["hello,", "good morning", "good afternoon"],
    "hi": ["good morning", "good afternoon", "good evening"],
}


def _demo_tenant():
    db.init_db()
    tenant = db.get_tenant_by_slug(seed_demo.DEMO_SLUG)
    if not tenant:
        raise SystemExit("Demo workspace missing. Run `python seed_demo.py` first.")
    return tenant


def judge(question, keywords, kind, result):
    """Why a case failed, or None if it passed."""
    answer = rag.normalize_text(result["answer"]).lower()
    outcome = result.get("outcome")
    sources = result.get("sources") or []

    if keywords and not any(k.lower() in answer for k in keywords):
        return f"none of {keywords} in the answer"
    for banned in MUST_NOT_CONTAIN.get(question, []):
        if banned in answer:
            return f"must not contain {banned!r}"

    if kind == FACT:
        if outcome != "ANSWERED":
            return f"outcome {outcome}, expected ANSWERED"
        if not sources:
            return "answered from the documents but cited nothing"
    elif kind == REFUSE:
        if outcome == "ANSWERED":
            return "claimed to answer from the documents"
        if sources:
            return f"a refusal must not cite a document, got {sources}"
    elif kind == GAP:
        if outcome != "NOANSWER":
            return f"outcome {outcome}, expected NOANSWER"
        if result.get("answered", True):
            return "marked answered, so the owner would never see it in Unanswered"
        if sources:
            return f"a gap must not cite a document, got {sources}"
    elif kind == CHAT:
        if any(m in answer for m in REFUSAL_MARKERS):
            return "small talk answered with a refusal"
        if sources:
            return f"small talk must not cite a document, got {sources}"
    return None


def run_eval(verbose=False):
    tenant = _demo_tenant()
    correct = 0
    failures = []

    for question, keywords, kind in TEST_CASES:
        result = rag.ask(question, tenant)
        if result.get("outcome") == "ERROR":
            # The provider refused after every retry — a 429 the preceding e2e
            # run had already spent the per-minute budget on, in practice. This
            # suite measures answers, not uptime, so one more attempt after a
            # pause; a second ERROR is counted as the failure it is.
            time.sleep(10)
            result = rag.ask(question, tenant)
        why = judge(question, keywords, kind, result)
        if why is None:
            correct += 1
        else:
            failures.append((question, result, kind, why))
        if verbose:
            mark = "PASS" if why is None else "FAIL"
            print(f"[{mark}] {question}\n       -> {result['answer']}"
                  f"  [{result.get('outcome')}; sources={result.get('sources')}]")
        # Groq's free tier is capped per minute; 44 back-to-back questions plus
        # the classifier's second call for the misses hit it every run. Pacing
        # keeps the suite inside the budget instead of relying on retries.
        time.sleep(0.6)

    total = len(TEST_CASES)
    accuracy = correct / total * 100
    print(f"\nScore: {correct}/{total} ({accuracy:.1f}%)")

    if failures:
        print("\nFailed cases:")
        for question, result, kind, why in failures:
            print(f" - Q: {question}  [{kind}]")
            print(f"   why: {why}")
            print(f"   got: {result['answer']}")

    minimum = os.environ.get("EVAL_MIN_CORRECT", "").strip()
    if minimum and correct < int(minimum):
        print(f"\nBelow the required {minimum}/{total} — failing.")
        sys.exit(1)
    return accuracy


if __name__ == "__main__":
    run_eval(verbose=True)
