"""Password hashing.

Werkzeug defaults to scrypt, which is deliberately memory-hard and allocates
~32 MB per hash. Alongside the resident embedding model that overruns a 512 MB
container, and the process gets OOM-killed mid-login with no traceback.

PBKDF2-HMAC-SHA256 at 600k iterations is the OWASP-recommended alternative and
uses negligible memory. It is CPU-hard rather than memory-hard, so it offers
less resistance to GPU-based cracking than scrypt — an explicit trade to stay
within the memory budget of small instances.

check_password_hash reads the algorithm from the stored hash, so accounts
created under the old scrypt scheme keep working.
"""

import hmac
import secrets

from werkzeug.security import check_password_hash, generate_password_hash

PASSWORD_HASH_METHOD = "pbkdf2:sha256:600000"


def hash_password(password):
    return generate_password_hash(password, method=PASSWORD_HASH_METHOD)


def verify_password(stored_hash, password):
    return check_password_hash(stored_hash, password)


# ------------------------------------------------------------------- CSRF
#
# A per-session token, minted on first render and required on every
# state-changing request. SESSION_COOKIE_SAMESITE="Lax" already blocks the
# classic cross-site auto-submitted form in a current browser, and that is
# genuinely most of the attack — but it is a browser behaviour, not a control
# this app enforces. It does not cover a same-site subdomain, a client that
# ships a different default, or any future route that takes a GET side effect.
#
# Written by hand rather than pulling in Flask-WTF: that would mean WTForms and
# a form-class rewrite of every template, for something that is thirty lines.


CSRF_FIELD = "csrf_token"


def issue_csrf_token(session):
    """The session's token, created on first use. Safe to call on every render."""
    if CSRF_FIELD not in session:
        session[CSRF_FIELD] = secrets.token_urlsafe(32)
    return session[CSRF_FIELD]


def csrf_ok(session, submitted):
    """compare_digest, not ==, so a wrong token cannot be found byte by byte."""
    expected = session.get(CSRF_FIELD)
    if not expected or not submitted:
        return False
    return hmac.compare_digest(expected, submitted)
