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

from werkzeug.security import check_password_hash, generate_password_hash

PASSWORD_HASH_METHOD = "pbkdf2:sha256:600000"


def hash_password(password):
    return generate_password_hash(password, method=PASSWORD_HASH_METHOD)


def verify_password(stored_hash, password):
    return check_password_hash(stored_hash, password)
