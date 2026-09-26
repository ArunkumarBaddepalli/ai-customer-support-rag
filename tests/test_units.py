"""Unit tests for the pure functions.

tests/e2e.py needs a running server, a database and (for a third of its
checks) an LLM key. The functions here need none of that: they take a value
and return a value, and they carry the most logic per line in the codebase —
chunk boundaries, outcome parsing, text normalisation, slug rules, redirect
safety, backoff, CSRF. A regression in any of them is reported in seconds,
before the app is even started.

    python -m pytest -q tests/test_units.py
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db          # noqa: E402
import ingest      # noqa: E402
import rag         # noqa: E402
import security    # noqa: E402


# ------------------------------------------------------------- chunking

class TestChunkText:
    def test_empty_document_yields_no_chunks(self):
        assert ingest.chunk_text("") == []
        assert ingest.chunk_text("\n\n   \n\n") == []

    def test_short_paragraphs_merge_into_one_chunk(self):
        text = "Timings:\nOpen 11 to 11.\n\nMenu:\nMargherita and Farmhouse."
        chunks = ingest.chunk_text(text, max_size=600)
        assert len(chunks) == 1
        assert "Timings:" in chunks[0] and "Menu:" in chunks[0]

    def test_merging_stops_at_max_size(self):
        a = "A" * 400
        b = "B" * 400
        chunks = ingest.chunk_text(f"{a}\n\n{b}", max_size=600)
        assert chunks == [a, b]

    def test_a_paragraph_is_never_split(self):
        # The whole point of the rewrite: a section longer than max_size stays
        # intact rather than being cut mid-sentence like the old 500-char slicer.
        long = "L" * 900
        assert ingest.chunk_text(long, max_size=600) == [long]

    def test_oversize_paragraph_does_not_swallow_neighbours(self):
        long = "L" * 900
        short = "S" * 10
        assert ingest.chunk_text(f"{short}\n\n{long}\n\n{short}", max_size=600) \
            == [short, long, short]


# ------------------------------------------------------- outcome parsing

class TestSplitOutcome:
    def test_plain_label(self):
        assert rag._split_outcome("A large Farmhouse is 399.\nANSWERED") \
            == ("A large Farmhouse is 399.", "ANSWERED")

    def test_decorated_label_is_still_recognised(self):
        assert rag._split_outcome("Sorry, no idea.\n**NOANSWER**")[1] == "NOANSWER"
        assert rag._split_outcome("Sorry, no idea.\n[OFFTOPIC].")[1] == "OFFTOPIC"

    def test_missing_label_falls_back_to_chat(self):
        # CHAT neither cites a document nor logs a gap — the safe default.
        assert rag._split_outcome("Hello! How can I help?") == ("Hello! How can I help?", "CHAT")
        assert rag._split_outcome("") == ("", "CHAT")

    def test_typography_is_normalised_before_the_label_check(self):
        answer, outcome = rag._split_outcome("We close at 7 pm.\nANSWERED")
        assert answer == "We close at 7 pm."
        assert outcome == "ANSWERED"


class TestNormalizeText:
    def test_the_three_eval_failures(self):
        # Each of these was a correct answer that failed a keyword match.
        assert rag.normalize_text("peri‑peri") == "peri-peri"
        assert rag.normalize_text("don’t") == "don't"
        assert rag.normalize_text("7 pm") == "7 pm"

    def test_quotes_and_spaces(self):
        assert rag.normalize_text("“free” delivery") == '"free" delivery'

    def test_words_and_dashes_are_untouched(self):
        assert rag.normalize_text("Sorry — try again") == "Sorry — try again"
        assert rag.normalize_text("plain ascii, unchanged.") == "plain ascii, unchanged."


# ------------------------------------------------------------ retry maths

class _Exc(Exception):
    def __init__(self, headers):
        class _R:  # noqa: D401 — stand-in for an HTTP response
            pass
        self.response = _R()
        self.response.headers = headers


class TestRetryAfter:
    def test_honours_the_header(self):
        assert rag._retry_after_seconds(_Exc({"retry-after": "2"})) == 2.0

    def test_caps_a_long_wait(self):
        # Someone is staring at a chat box; a 60s wait is a request to fail fast.
        assert rag._retry_after_seconds(_Exc({"Retry-After": "60"})) == rag.MAX_RETRY_WAIT_SECONDS

    def test_missing_or_garbage_header(self):
        assert rag._retry_after_seconds(_Exc({})) is None
        assert rag._retry_after_seconds(_Exc({"retry-after": "soon"})) is None
        assert rag._retry_after_seconds(Exception("no response attribute")) is None


# ---------------------------------------------------------------- slugs

class TestSlugs:
    @pytest.mark.parametrize("good", ["pizza-palace", "ab1", "zen-spa-2", "x" * 50])
    def test_valid_slugs(self, good):
        assert db.SLUG_RE.match(good)

    @pytest.mark.parametrize("bad", ["Pizza", "a", "a1", "-lead", "trail-", "has space",
                                     "under_score", "x" * 51, "../etc", ""])
    def test_invalid_slugs(self, bad):
        assert not db.SLUG_RE.match(bad)

    def test_slugify_produces_a_valid_lowercase_slug(self):
        slug = db.slugify("Pizza Palace")
        assert slug == slug.lower() and " " not in slug
        assert db.SLUG_RE.match(slug)

    def test_routes_are_reserved(self):
        for word in ("admin", "login", "signup", "dashboard", "api", "static", "c"):
            assert word in db.RESERVED_SLUGS


# --------------------------------------------------------------- backoff

class TestLoginBackoff:
    def test_first_three_failures_are_free(self):
        assert [db._backoff_seconds(n) for n in range(4)] == [0, 0, 0, 0]

    def test_doubles_then_caps(self):
        assert [db._backoff_seconds(n) for n in (4, 5, 6, 7)] == [2, 4, 8, 16]
        assert db._backoff_seconds(8) == db.LOGIN_BACKOFF_CAP_SECONDS
        assert db._backoff_seconds(50) == db.LOGIN_BACKOFF_CAP_SECONDS


# ------------------------------------------------------------------ CSRF

class TestCsrf:
    def test_token_is_minted_once_per_session(self):
        session = {}
        first = security.issue_csrf_token(session)
        assert len(first) >= 32
        assert security.issue_csrf_token(session) == first

    def test_only_the_exact_token_passes(self):
        session = {}
        token = security.issue_csrf_token(session)
        assert security.csrf_ok(session, token)
        assert not security.csrf_ok(session, token[:-1] + "x")
        assert not security.csrf_ok(session, "")
        assert not security.csrf_ok(session, None)
        assert not security.csrf_ok({}, token)


# -------------------------------------------------------- redirect safety

@pytest.fixture(scope="module")
def flask_app():
    # Imported lazily: app.py initialises the database on import, and only
    # this class needs it.
    import app as app_module
    return app_module


class TestSafePath:
    @pytest.mark.parametrize("path", ["/dashboard", "/dashboard/gaps?x=1", "/c/pizza-palace"])
    def test_same_site_paths_pass_through(self, flask_app, path):
        with flask_app.app.test_request_context():
            assert flask_app._safe_path(path) == path

    @pytest.mark.parametrize("evil", ["//evil.com", "/\\evil.com", "https://evil.com",
                                      "javascript:alert(1)", "", "evil.com/x"])
    def test_anything_else_falls_back_to_our_own_route(self, flask_app, evil):
        with flask_app.app.test_request_context():
            assert flask_app._safe_path(evil) == "/dashboard"


# ----------------------------------------------------------- small talk

class TestSmallTalk:
    T = "Timings, Menu and Prices"

    @pytest.mark.parametrize("q", [
        "wt is. ur job", "what is your job", "who r u", "who are you?", "what can u do",
        "What do you do", "how can you help me", "help", "are you a bot", "whats ur role",
    ])
    def test_identity_questions_are_answered_without_a_model(self, q):
        answer, outcome = rag.smalltalk_answer(q, "Pizza Palace", "", self.T)
        assert outcome == "CHAT"
        assert "Pizza Palace assistant" in answer and "Timings" in answer

    @pytest.mark.parametrize("q, word", [
        ("hi", "Hi!"), ("Hello there", "Hi!"), ("good morning", "Hi!"), ("hii", "Hi!"),
        ("thanks", "welcome"), ("thank you so much", "welcome"), ("thx", "welcome"),
        ("bye", "Bye"), ("ok bye", "Bye"),
    ])
    def test_greetings_thanks_and_goodbyes(self, q, word):
        answer, outcome = rag.smalltalk_answer(q, "Pizza Palace", "", self.T)
        assert outcome == "CHAT" and word in answer

    @pytest.mark.parametrize("q", [
        "what is your address", "what are your hours", "what is the price of a large pizza",
        "am i pretty", "do you deliver", "who is the owner", "what do you charge for delivery",
        "hi can i get a large pepperoni delivered", "help me find parking",
    ])
    def test_business_and_other_questions_go_to_the_model(self, q):
        assert rag.smalltalk_answer(q, "Pizza Palace", "", self.T) is None

    @pytest.mark.parametrize("q", ["ok", "okay", "k", "got it", "fine thanks", "oh ok", "Sure"])
    def test_acknowledgements_get_a_nudge_not_a_refusal(self, q):
        answer, outcome = rag.smalltalk_answer(q, "Pizza Palace", "", self.T)
        assert outcome == "CHAT" and "anything else" in answer

    @pytest.mark.parametrize("q, word", [("haa", "Sure"), ("yes", "Sure"), ("yeah", "Sure"),
                                         ("no", "No problem"), ("nope", "No problem"), ("haha", "Glad"), ("lol", "Glad")])
    def test_yes_no_and_laughs(self, q, word):
        answer, outcome = rag.smalltalk_answer(q, "Pizza Palace", "", self.T)
        assert outcome == "CHAT" and word in answer

    @pytest.mark.parametrize("q", ["one pizza", "ok what are your hours", "menu", "food"])
    def test_orders_and_topics_are_not_small_talk(self, q):
        assert rag.smalltalk_answer(q, "Pizza Palace", "", self.T) is None

    def test_identity_answer_mentions_a_person_when_contact_is_set(self):
        answer, _ = rag.smalltalk_answer("who are you", "Acme", "help@acme.test", "Fees")
        assert "help@acme.test" in answer

    def test_normalise_expands_shorthand(self):
        assert rag.normalise_message("Wt is. UR job??") == "what is your job"

    def test_topics_line(self):
        assert rag.topics_line([]) == ""
        assert rag.topics_line(["Fees"]) == "Fees"
        assert rag.topics_line(["A", "B", "C"]) == "A, B and C"


class TestSectionHeadings:
    DOC = "Riverside Clinic FAQ\n\nOpening hours:\nMon-Fri 8-6.\n\nFees:\nStandard $75.\n\nNot a heading: text\n\nEmpty heading:\n\nMore:\nx\n"

    def test_headings_need_a_colon_and_a_following_line(self):
        assert ingest.section_headings(self.DOC) == ["Opening hours", "Fees", "More"]

    @pytest.mark.parametrize("raw, answer, label", [
        ("The cheapest is the Small Margherita at ₹149. ANSWERED", "The cheapest is the Small Margherita at ₹149.", "ANSWERED"),
        ("We open at 11.\nANSWERED", "We open at 11.", "ANSWERED"),
        ("Sorry, no.\n\n**NOANSWER**", "Sorry, no.", "NOANSWER"),
        ("Hi there! (CHAT)", "Hi there!", "CHAT"),
        ("I can only help with Pizza Palace questions. - OFFTOPIC", "I can only help with Pizza Palace questions.", "OFFTOPIC"),
    ])
    def test_inline_and_decorated_labels_are_stripped(self, raw, answer, label):
        assert rag._split_outcome(raw) == (answer, label)
        assert rag._has_label(raw)

    def test_a_word_inside_the_answer_is_not_a_label(self):
        raw = "We chat with customers daily and answered every call."
        assert rag._split_outcome(raw) == (raw, "CHAT") and not rag._has_label(raw)

    def test_labels_get_chat_and_offtopic_through(self):
        assert rag._has_label("Sure.\nCHAT") and rag._has_label("No.\n**OFFTOPIC**")
        assert not rag._has_label("Just an answer with no label")
