"""
Measure how accurate the bot is against a fixed set of test questions.

Each test case has a question and one or more keywords that MUST appear in
the answer for it to count as correct. This is a simple but honest way to
score a RAG bot without needing a human to grade every run.

Runs against the Pizza Palace demo workspace, so seed it first:
    python seed_demo.py
    python eval.py
"""

import db
import rag
import seed_demo

TEST_CASES = [
    ("What time do you open?", ["11"]),
    ("What time do you close?", ["11"]),
    ("Are you open on public holidays?", ["holiday"]),
    ("Does the kitchen close before the restaurant?", ["30"]),
    ("What pizza flavors do you have?", ["margherita"]),
    ("Do you have a paneer pizza?", ["peppy paneer"]),
    ("What sizes do pizzas come in?", ["small"]),
    ("What sides do you serve besides pizza?", ["garlic bread"]),
    ("What dips are available?", ["peri-peri"]),
    ("How much is a small Margherita?", ["149"]),
    ("How much is a large Farmhouse?", ["399"]),
    ("What's the price of a medium Chicken Tikka?", ["379"]),
    ("How much does garlic bread cost?", ["99"]),
    ("How much is a cold drink?", ["49"]),
    ("What is the delivery radius?", ["7"]),
    ("How long does delivery take?", ["30", "40"]),
    ("Is delivery free?", ["399"]),
    ("What's the delivery fee?", ["40"]),
    ("Can I track my order?", ["track"]),
    ("What payment methods do you accept?", ["upi"]),
    ("Can I pay in installments?", ["not accepted", "no", "cannot"]),
    ("My order arrived cold, what do I do?", ["30 minutes"]),
    # Accepts either an explicit "no" or the equivalent "refunds are only for X"
    ("Can I get a refund if I just change my mind?", ["cannot", "no", "not", "only"]),
    ("How late can I report a damaged order?", ["2 hours"]),
    ("Can I cancel my order?", ["5 minutes"]),
    ("Do you have any offers?", ["tuesday"]),
    ("Is there a student discount?", ["15%"]),
    ("How do I contact support?", ["98765"]),
    ("What are support hours?", ["10"]),
    # Off-topic: must refuse rather than answer from the model's own world knowledge
    ("Do you sell laptops?", ["not sure", "don't know", "only", "contact"]),
    ("Can I book a hotel room through you?", ["not sure", "don't know", "only", "contact"]),
    ("What is the capital of France?", ["only", "don't know", "not sure"]),
    ("Should I invest in bitcoin?", ["only", "don't know", "not sure"]),
    # Small talk: must respond conversationally, NOT with the "I don't know" refusal
    ("hi", ["help", "hi", "hello"]),
    ("how are you", ["help", "good", "great", "well"]),
    ("thanks!", ["welcome", "help", "glad"]),
    ("who are you", ["assistant", "support", "help"]),
    # Abuse / frustration: must de-escalate and offer a human, never greet or argue
    ("idiot", ["98765", "support@", "help"]),
    ("this is useless", ["98765", "support@", "help"]),
    ("you are the worst bot ever", ["98765", "support@", "help"]),
    ("I want to talk to a human", ["98765", "support@"]),
]

# Small talk must never trigger the can't-help fallback — checked separately
# because a reply can contain "help" while still being a refusal.
MUST_NOT_REFUSE = {"hi", "how are you", "thanks!", "who are you"}
REFUSAL_MARKERS = ("not sure about that", "don't know")

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


def run_eval(verbose=False):
    tenant = _demo_tenant()
    correct = 0
    failures = []

    for question, keywords in TEST_CASES:
        result = rag.ask(question, tenant)
        answer_lower = result["answer"].lower()
        hit = any(k.lower() in answer_lower for k in keywords)

        if question in MUST_NOT_REFUSE and any(m in answer_lower for m in REFUSAL_MARKERS):
            hit = False  # small talk answered with a refusal = fail

        banned = MUST_NOT_CONTAIN.get(question, [])
        if any(b in answer_lower for b in banned):
            hit = False  # leaked world knowledge instead of staying in scope

        if hit:
            correct += 1
        else:
            failures.append((question, result["answer"], keywords))

        if verbose:
            mark = "PASS" if hit else "FAIL"
            print(f"[{mark}] {question}\n       -> {result['answer']}")

    total = len(TEST_CASES)
    accuracy = correct / total * 100
    print(f"\nScore: {correct}/{total} ({accuracy:.1f}%)")

    if failures:
        print("\nFailed cases:")
        for question, answer, keywords in failures:
            print(f" - Q: {question}")
            print(f"   expected one of: {keywords}")
            print(f"   got: {answer}")

    return accuracy


if __name__ == "__main__":
    run_eval(verbose=True)
