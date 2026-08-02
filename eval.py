"""
Measure how accurate the bot is against a fixed set of test questions.

Each test case has a question and one or more keywords that MUST appear in
the answer for it to count as correct. This is a simple but honest way to
score a RAG bot without needing a human to grade every run.

Run:
    python eval.py
"""

import rag

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
    ("Can I get a refund if I just change my mind?", ["cannot", "no", "not"]),
    ("How late can I report a damaged order?", ["2 hours"]),
    ("Can I cancel my order?", ["5 minutes"]),
    ("Do you have any offers?", ["tuesday"]),
    ("Is there a student discount?", ["15%"]),
    ("How do I contact support?", ["98765"]),
    ("What are support hours?", ["10"]),
    ("Do you sell laptops?", ["not sure", "don't know", "contact support"]),
    ("Can I book a hotel room through you?", ["not sure", "don't know", "contact support"]),
]


def run_eval(verbose=False):
    correct = 0
    failures = []

    for question, keywords in TEST_CASES:
        result = rag.ask(question)
        answer_lower = result["answer"].lower()
        hit = any(k.lower() in answer_lower for k in keywords)

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
