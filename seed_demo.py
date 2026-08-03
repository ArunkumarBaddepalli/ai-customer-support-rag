"""Creates the Pizza Palace demo workspace so there's something to look at
(and something for eval.py to run against).

    python seed_demo.py
"""

from security import hash_password

import db
import ingest

DEMO_EMAIL = "demo@pizzapalace.example"
DEMO_PASSWORD = "demo12345"
DEMO_SLUG = "pizza-palace"
SAMPLE_FAQ = "sample_docs/faq.txt"


def seed():
    db.init_db()

    user = db.get_user_by_email(DEMO_EMAIL)
    if user:
        user_id = user["id"]
    else:
        user_id = db.create_user(DEMO_EMAIL, hash_password(DEMO_PASSWORD))

    tenant = db.get_tenant_by_slug(DEMO_SLUG)
    if not tenant:
        db.create_tenant(user_id, "Pizza Palace", DEMO_SLUG)
        tenant = db.get_tenant_by_slug(DEMO_SLUG)

    db.update_tenant(
        tenant["id"],
        company_tagline="Ask me anything about our menu, timings, delivery, or returns.",
        brand_color="#2563eb",
        support_phone="+91 98765 43210",
        support_email="support@pizzapalace.example",
        onboarded=1,
    )

    with open(SAMPLE_FAQ, encoding="utf-8") as f:
        db.save_document(tenant["id"], "faq.txt", f.read())

    chunks = ingest.build_index(tenant["id"], DEMO_SLUG, verbose=True)

    print(f"\nDemo workspace ready ({chunks} chunks).")
    print(f"  Chat page : /c/{DEMO_SLUG}")
    print(f"  Login     : {DEMO_EMAIL} / {DEMO_PASSWORD}")


if __name__ == "__main__":
    seed()
