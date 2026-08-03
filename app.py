"""Flask app — multi-tenant customer-support chatbot.

Public:
    /                     landing page
    /signup /login        account creation and sign-in
    /c/<slug>             a business's live chatbot
    /api/c/<slug>/chat    that bot's chat endpoint

Signed in (owner only):
    /onboarding           first-run wizard: branding -> first document
    /dashboard            documents
    /dashboard/settings   branding + support contact
    /dashboard/profile    email + password

Every dashboard route resolves the tenant from the *session user*, never from
a URL parameter, so a signed-in user can only ever touch their own workspace.
"""

import os
import re
import time
from functools import wraps

from dotenv import load_dotenv

load_dotenv()

from flask import (
    Flask, Response, abort, jsonify, redirect, render_template, request,
    session, url_for
)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

import db
import ingest
import mailer
import rag
from security import hash_password, verify_password

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24))
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # 2 MB upload cap
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# Render (and any host behind a reverse proxy) terminates TLS at the edge and
# forwards plain HTTP to the container. Without this, Flask has no way to know
# the original request was HTTPS: request.is_secure is wrong, and url_for(...,
# _external=True) generates http:// links. That silently affected every
# password-reset and email-verification link this app sends — a sensitive
# one-time token in a link that claims to be insecure. Confirmed via a real
# WSGI request with X-Forwarded-Proto, not assumed.
#
# x_for=1 also makes request.remote_addr the real client IP instead of
# Render's edge — needed for per-IP login throttling below. Trusting exactly
# one hop is correct here because there is exactly one proxy (Render's) in
# front of this container.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_for=1)

db.init_db()

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LENGTH = 8

def current_user():
    user_id = session.get("user_id")
    return db.get_user(user_id) if user_id else None


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def with_tenant(view):
    """Resolve the signed-in user's own workspace and pass it in."""
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        tenant = db.get_tenant_for_user(session["user_id"])
        if not tenant:
            return redirect(url_for("signup"))
        return view(tenant, *args, **kwargs)
    return wrapped


@app.context_processor
def inject_user():
    return {"user": current_user()}


# ------------------------------------------------------------ public site


@app.route("/")
def home():
    if current_user():
        return redirect(url_for("dashboard"))
    return render_template("landing.html")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user():
        return redirect(url_for("dashboard"))

    error = None
    form = {"email": "", "company_name": ""}

    if request.method == "POST":
        form["email"] = (request.form.get("email") or "").strip()
        form["company_name"] = (request.form.get("company_name") or "").strip()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""

        try:
            if not EMAIL_RE.match(form["email"]):
                raise ValueError("Enter a valid email address.")
            if not (2 <= len(form["company_name"]) <= 100):
                raise ValueError("Business name must be 2-100 characters.")
            if len(password) < MIN_PASSWORD_LENGTH:
                raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
            if password != confirm:
                raise ValueError("Passwords don't match.")

            user_id = db.create_user(form["email"], hash_password(password))
            slug = db.unique_slug(form["company_name"])
            db.create_tenant(user_id, form["company_name"], slug)
            _send_verification_email(user_id, form["email"])

            session.clear()
            session["user_id"] = user_id
            return redirect(url_for("onboarding"))
        except ValueError as exc:
            error = str(exc)

    return render_template("signup.html", error=error, form=form)


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("dashboard"))

    error = None
    email = ""
    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        password = request.form.get("password") or ""

        # Progressive delay per account and per source IP — see the note on
        # login_attempts in db.py for why this throttles rather than locks.
        email_key = f"email:{email.lower()}"
        ip_key = f"ip:{request.remote_addr or 'unknown'}"
        delay = max(db.get_login_delay(email_key), db.get_login_delay(ip_key))
        if delay:
            time.sleep(delay)

        user = db.get_user_by_email(email)

        if user and verify_password(user["password_hash"], password):
            db.clear_login_failures(email_key)
            db.clear_login_failures(ip_key)
            session.clear()
            session["user_id"] = user["id"]
            tenant = db.get_tenant_for_user(user["id"])
            if tenant and not tenant["onboarded"]:
                return redirect(url_for("onboarding"))
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") else url_for("dashboard"))

        db.record_login_failure(email_key)
        db.record_login_failure(ip_key)
        error = "Wrong email or password."

    return render_template("login.html", error=error, email=email)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


def _send_verification_email(user_id, email):
    """Best-effort — a failed send should never block signup or a resend click."""
    token = db.create_token(user_id, "verify")
    url = url_for("verify_email", token=token, _external=True)
    mailer.send_verification(email, url)


@app.route("/verify-email/<token>")
def verify_email(token):
    user_id = db.consume_token(token, "verify")
    if user_id:
        db.set_email_verified(user_id)
        status = "verified"
    else:
        status = "invalid"
    return render_template("token_result.html", status=status, purpose="verify")


@app.route("/resend-verification", methods=["POST"])
@login_required
def resend_verification():
    user = current_user()
    if not user["email_verified"] and not db.recently_sent(user["id"], "verify"):
        _send_verification_email(user["id"], user["email"])
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    sent = False
    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        user = db.get_user_by_email(email)
        # Always claim success either way — confirming or denying that an
        # email has an account is exactly what lets an attacker enumerate users.
        if user and not db.recently_sent(user["id"], "reset"):
            token = db.create_token(user["id"], "reset")
            url = url_for("reset_password", token=token, _external=True)
            mailer.send_password_reset(email, url)
        sent = True
    return render_template("forgot_password.html", sent=sent)


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    if not db.token_valid(token, "reset"):
        return render_template("token_result.html", status="invalid", purpose="reset")

    error = None
    if request.method == "POST":
        new = request.form.get("new_password") or ""
        confirm = request.form.get("confirm_password") or ""
        if len(new) < MIN_PASSWORD_LENGTH:
            error = f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        elif new != confirm:
            error = "Passwords don't match."
        else:
            # Consuming here (rather than up front) means a mistyped
            # confirmation doesn't burn the link before the user gets it right.
            user_id = db.consume_token(token, "reset")
            if not user_id:
                return render_template("token_result.html", status="invalid", purpose="reset")
            db.update_user_password(user_id, hash_password(new))
            # Proving control of the email is at least as strong as a correct
            # password — any login backoff built up before this shouldn't
            # follow the user into their first login with the new password.
            user = db.get_user(user_id)
            db.clear_login_failures(f"email:{user['email'].lower()}")
            session.clear()
            session["user_id"] = user_id
            return redirect(url_for("dashboard"))
    return render_template("reset_password.html", error=error, token=token)


# ------------------------------------------------------------- onboarding


@app.route("/onboarding", methods=["GET", "POST"])
@with_tenant
def onboarding(tenant):
    error = None

    if request.method == "POST":
        try:
            company_name = (request.form.get("company_name") or "").strip()
            if not (2 <= len(company_name) <= 100):
                raise ValueError("Business name must be 2-100 characters.")

            db.update_tenant(
                tenant["id"],
                company_name=company_name,
                company_tagline=(request.form.get("company_tagline") or "").strip(),
                brand_color=_clean_color(request.form.get("brand_color")),
                support_phone=_clean_support_phone(request.form.get("support_phone")),
                support_email=_clean_support_email(request.form.get("support_email")),
            )
            _handle_logo_fields(tenant)

            text = (request.form.get("text") or "").strip()
            upload = request.files.get("file")
            if (upload and upload.filename) or text:
                _save_document(
                    tenant,
                    upload=upload,
                    title=(request.form.get("title") or "faq").strip(),
                    text=text,
                )

            db.update_tenant(tenant["id"], onboarded=1)
            return redirect(url_for("dashboard"))
        except ValueError as exc:
            error = str(exc)
            tenant = db.get_tenant_for_user(session["user_id"])

    return render_template("onboarding.html", tenant=tenant, error=error)


# -------------------------------------------------------------- dashboard


@app.route("/dashboard", methods=["GET", "POST"])
@with_tenant
def dashboard(tenant):
    message = error = None

    if request.method == "POST":
        try:
            filename = _save_document(
                tenant,
                upload=request.files.get("file"),
                title=(request.form.get("title") or "").strip(),
                text=(request.form.get("text") or "").strip(),
            )
            message = f"Saved '{filename}' and rebuilt the search index."
        except ValueError as exc:
            error = str(exc)

    return render_template(
        "dashboard.html",
        tenant=tenant,
        documents=db.list_document_names(tenant["id"]),
        message=message,
        error=error,
    )


@app.route("/dashboard/documents/<name>/delete", methods=["POST"])
@with_tenant
def delete_document(tenant, name):
    db.delete_document(tenant["id"], secure_filename(name))
    ingest.build_index(tenant["id"], tenant["slug"])
    rag.reload_index(tenant["slug"])
    return redirect(url_for("dashboard"))


@app.route("/dashboard/settings", methods=["GET", "POST"])
@with_tenant
def settings(tenant):
    message = error = None

    if request.method == "POST":
        try:
            company_name = (request.form.get("company_name") or "").strip()
            if not (2 <= len(company_name) <= 100):
                raise ValueError("Business name must be 2-100 characters.")
            _handle_logo_fields(tenant)
            db.update_tenant(
                tenant["id"],
                company_name=company_name,
                company_tagline=(request.form.get("company_tagline") or "").strip(),
                brand_color=_clean_color(request.form.get("brand_color")),
                support_phone=_clean_support_phone(request.form.get("support_phone")),
                support_email=_clean_support_email(request.form.get("support_email")),
            )
            message = "Settings saved."
            tenant = db.get_tenant_for_user(session["user_id"])
        except ValueError as exc:
            error = str(exc)

    return render_template("settings.html", tenant=tenant, message=message, error=error)


@app.route("/dashboard/profile", methods=["GET", "POST"])
@with_tenant
def profile(tenant):
    message = error = None
    user = current_user()

    if request.method == "POST":
        try:
            if request.form.get("form_type") == "email":
                email = (request.form.get("email") or "").strip()
                if not EMAIL_RE.match(email):
                    raise ValueError("Enter a valid email address.")
                db.update_user_email(user["id"], email)
                message = "Email updated."
            else:
                current = request.form.get("current_password") or ""
                new = request.form.get("new_password") or ""
                confirm = request.form.get("confirm_password") or ""
                if not verify_password(user["password_hash"], current):
                    raise ValueError("Current password is incorrect.")
                if len(new) < MIN_PASSWORD_LENGTH:
                    raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
                if new != confirm:
                    raise ValueError("New passwords don't match.")
                db.update_user_password(user["id"], hash_password(new))
                message = "Password updated."
            user = current_user()
        except ValueError as exc:
            error = str(exc)

    return render_template(
        "profile.html", tenant=tenant, account=user, message=message, error=error
    )


@app.route("/dashboard/gaps")
@with_tenant
def gaps(tenant):
    """Questions the bot couldn't answer — the owner's to-do list."""
    return render_template(
        "gaps.html",
        tenant=tenant,
        rows=db.get_unanswered(tenant["id"]),
        show_resolved=False,
    )


@app.route("/dashboard/gaps/resolved")
@with_tenant
def gaps_resolved(tenant):
    return render_template(
        "gaps.html",
        tenant=tenant,
        rows=db.get_unanswered(tenant["id"], include_resolved=True),
        show_resolved=True,
    )


@app.route("/dashboard/gaps/<int:row_id>/resolve", methods=["POST"])
@with_tenant
def resolve_gap(tenant, row_id):
    db.resolve_unanswered(tenant["id"], row_id)
    return redirect(url_for("gaps"))


# ------------------------------------------------------- the public chatbot


@app.route("/c/<slug>")
def chatbot(slug):
    tenant = db.get_tenant_by_slug(slug)
    if not tenant:
        abort(404)
    return render_template("chat.html", tenant=tenant)


@app.route("/c/<slug>/logo")
def tenant_logo(slug):
    """Serve a tenant's logo from the database."""
    data, content_type = db.get_logo(slug)
    if not data:
        abort(404)
    return Response(data, mimetype=content_type or "image/png",
                    headers={"Cache-Control": "public, max-age=300"})


@app.route("/api/c/<slug>/chat", methods=["POST"])
def chat_api(slug):
    tenant = db.get_tenant_by_slug(slug)
    if not tenant:
        abort(404)

    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"error": "question is required"}), 400
    if len(question) > 1000:
        return jsonify({"error": "question is too long"}), 400

    result = rag.ask(question, tenant)

    # Record only what the bot failed to answer. Customer conversations are
    # never stored — see the note in db.init_db().
    if not result.get("answered", True):
        db.record_unanswered(tenant["id"], question)

    return jsonify({"answer": result["answer"], "sources": result["sources"]})


# ------------------------------------------------------------------ helpers


def _clean_color(value):
    value = (value or "").strip()
    return value if re.match(r"^#[0-9a-fA-F]{6}$", value) else "#2563eb"


# A phone number customers can actually dial: digits, with the usual separators.
PHONE_CHARS_RE = re.compile(r"^[0-9+()\-.\s]+$")


def _clean_support_phone(value):
    value = (value or "").strip()
    if not value:
        return ""
    digits = re.sub(r"\D", "", value)
    if not PHONE_CHARS_RE.match(value) or not (7 <= len(digits) <= 15):
        raise ValueError(
            "Support phone doesn't look like a real number — use digits, "
            "optionally with + ( ) - and spaces."
        )
    # collapse runs of separators so "660----88" can't reach a customer
    return re.sub(r"[\-.\s]{2,}", " ", value).strip()


def _clean_support_email(value):
    value = (value or "").strip()
    if value and not EMAIL_RE.match(value):
        raise ValueError("Support email isn't a valid email address.")
    return value


MAX_LOGO_BYTES = 1024 * 1024  # 1 MB

# Sniff the actual bytes rather than trusting the extension. SVG is deliberately
# not allowed — it can carry script and we serve these files to the public.
IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)

MIME_TYPES = {"png": "image/png", "jpg": "image/jpeg",
              "gif": "image/gif", "webp": "image/webp"}


def _sniff_image(data):
    for signature, ext in IMAGE_SIGNATURES:
        if data.startswith(signature):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _image_dimensions(data, ext):
    """Read width/height straight from the header.

    Magic bytes alone aren't enough: a file can start with a valid PNG
    signature and be truncated garbage after it. That passes a signature check,
    then renders as a broken image on the customer-facing chat page. Parsing
    dimensions proves the header is structurally real, without pulling in an
    image library (memory is tight on small instances).
    """
    try:
        if ext == "png":
            if data[12:16] != b"IHDR":
                return None
            return (int.from_bytes(data[16:20], "big"),
                    int.from_bytes(data[20:24], "big"))
        if ext == "gif":
            return (int.from_bytes(data[6:8], "little"),
                    int.from_bytes(data[8:10], "little"))
        if ext == "webp":
            if data[12:16] == b"VP8X":
                return (int.from_bytes(data[24:27], "little") + 1,
                        int.from_bytes(data[27:30], "little") + 1)
            if data[12:16] == b"VP8L":
                bits = int.from_bytes(data[21:25], "little")
                return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
            if data[12:16] == b"VP8 ":
                return (int.from_bytes(data[26:28], "little") & 0x3FFF,
                        int.from_bytes(data[28:30], "little") & 0x3FFF)
            return None
        if ext == "jpg":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    return None
                marker = data[i + 1]
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    return (int.from_bytes(data[i + 7:i + 9], "big"),
                            int.from_bytes(data[i + 5:i + 7], "big"))
                i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
            return None
    except (IndexError, ValueError):
        return None
    return None


def _handle_logo_fields(tenant):
    """Store the logo in the database so it survives a redeploy."""
    if request.form.get("remove_logo"):
        db.clear_logo(tenant["id"])
        return

    upload = request.files.get("logo")
    if not (upload and upload.filename):
        return

    data = upload.read(MAX_LOGO_BYTES + 1)
    if len(data) > MAX_LOGO_BYTES:
        raise ValueError("Logo must be under 1 MB.")
    if not data:
        raise ValueError("That logo file is empty.")

    ext = _sniff_image(data)
    if not ext:
        raise ValueError("Logo must be a PNG, JPG, GIF or WebP image.")

    size = _image_dimensions(data, ext)
    if not size or not all(1 <= n <= 8000 for n in size):
        raise ValueError("That image looks corrupted — try re-saving or exporting it again.")

    db.save_logo(tenant["id"], data, MIME_TYPES[ext])


def _save_document(tenant, upload, title, text):
    """Store a .txt document in the database and rebuild the tenant's index."""
    if upload and upload.filename:
        filename = secure_filename(upload.filename)
        if not filename.endswith(".txt"):
            raise ValueError("Only .txt files are supported.")
        try:
            content = upload.read().decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("That file isn't readable as UTF-8 text.")
    elif title and text:
        filename = secure_filename(title)
        if not filename.endswith(".txt"):
            filename += ".txt"
        content = text
    else:
        raise ValueError("Provide either a .txt file or a title and some text.")

    if not filename or filename == ".txt":
        raise ValueError("Give the document a valid name.")
    if not content.strip():
        raise ValueError("The document is empty.")

    db.save_document(tenant["id"], filename, content)
    ingest.build_index(tenant["id"], tenant["slug"])
    rag.reload_index(tenant["slug"])
    return filename


@app.errorhandler(404)
def not_found(_):
    return render_template("404.html"), 404


@app.errorhandler(413)
def too_large(_):
    return "That file is too large (2 MB max).", 413


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", debug=debug, port=port)
