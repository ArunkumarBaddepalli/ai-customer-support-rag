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
from functools import wraps

from dotenv import load_dotenv

load_dotenv()

from flask import (
    Flask, abort, jsonify, redirect, render_template, request, session, url_for
)
from werkzeug.utils import secure_filename

import db
import ingest
import rag
from security import hash_password, verify_password

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24))
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # 2 MB upload cap

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
            if len(form["company_name"]) < 2:
                raise ValueError("Enter your business name.")
            if len(password) < MIN_PASSWORD_LENGTH:
                raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
            if password != confirm:
                raise ValueError("Passwords don't match.")

            user_id = db.create_user(form["email"], hash_password(password))
            slug = db.unique_slug(form["company_name"])
            db.create_tenant(user_id, form["company_name"], slug)

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
        user = db.get_user_by_email(email)

        if user and verify_password(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            tenant = db.get_tenant_for_user(user["id"])
            if tenant and not tenant["onboarded"]:
                return redirect(url_for("onboarding"))
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") else url_for("dashboard"))

        error = "Wrong email or password."

    return render_template("login.html", error=error, email=email)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


# ------------------------------------------------------------- onboarding


@app.route("/onboarding", methods=["GET", "POST"])
@with_tenant
def onboarding(tenant):
    error = None

    if request.method == "POST":
        try:
            company_name = (request.form.get("company_name") or "").strip()
            if len(company_name) < 2:
                raise ValueError("Enter your business name.")

            db.update_tenant(
                tenant["id"],
                company_name=company_name,
                company_tagline=(request.form.get("company_tagline") or "").strip(),
                logo_emoji=(request.form.get("logo_emoji") or "💬").strip()[:4] or "💬",
                logo_path=_handle_logo_fields(tenant),
                brand_color=_clean_color(request.form.get("brand_color")),
                support_contact=(request.form.get("support_contact") or "").strip(),
            )

            text = (request.form.get("text") or "").strip()
            upload = request.files.get("file")
            if (upload and upload.filename) or text:
                _save_document(
                    tenant["slug"],
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
                tenant["slug"],
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
        documents=ingest.list_documents(tenant["slug"]),
        message=message,
        error=error,
    )


@app.route("/dashboard/documents/<name>/delete", methods=["POST"])
@with_tenant
def delete_document(tenant, name):
    filename = secure_filename(name)
    path = os.path.join(ingest.docs_dir(tenant["slug"]), filename)
    if os.path.isfile(path):
        os.remove(path)
        ingest.build_index(tenant["slug"])
        rag.reload_index(tenant["slug"])
    return redirect(url_for("dashboard"))


@app.route("/dashboard/settings", methods=["GET", "POST"])
@with_tenant
def settings(tenant):
    message = error = None

    if request.method == "POST":
        try:
            company_name = (request.form.get("company_name") or "").strip()
            if len(company_name) < 2:
                raise ValueError("Enter your business name.")
            logo_path = _handle_logo_fields(tenant)
            db.update_tenant(
                tenant["id"],
                company_name=company_name,
                company_tagline=(request.form.get("company_tagline") or "").strip(),
                logo_emoji=(request.form.get("logo_emoji") or "💬").strip()[:4] or "💬",
                logo_path=logo_path,
                brand_color=_clean_color(request.form.get("brand_color")),
                support_contact=(request.form.get("support_contact") or "").strip(),
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


@app.route("/dashboard/history")
@with_tenant
def history(tenant):
    return render_template(
        "history.html", tenant=tenant, rows=db.get_history(tenant["id"])
    )


# ------------------------------------------------------- the public chatbot


@app.route("/c/<slug>")
def chatbot(slug):
    tenant = db.get_tenant_by_slug(slug)
    if not tenant:
        abort(404)
    return render_template("chat.html", tenant=tenant)


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
    db.save_message(tenant["id"], question, result["answer"], result["sources"])
    return jsonify(result)


# ------------------------------------------------------------------ helpers


def _clean_color(value):
    value = (value or "").strip()
    return value if re.match(r"^#[0-9a-fA-F]{6}$", value) else "#2563eb"


LOGO_DIR = os.path.join("static", "logos")
MAX_LOGO_BYTES = 1024 * 1024  # 1 MB

# Sniff the actual bytes rather than trusting the extension. SVG is deliberately
# not allowed — it can carry script and we serve these files to the public.
IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)


def _sniff_image(data):
    for signature, ext in IMAGE_SIGNATURES:
        if data.startswith(signature):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _save_logo(slug, upload):
    """Store a tenant's logo and return its path relative to /static."""
    data = upload.read(MAX_LOGO_BYTES + 1)
    if len(data) > MAX_LOGO_BYTES:
        raise ValueError("Logo must be under 1 MB.")
    if not data:
        raise ValueError("That logo file is empty.")

    ext = _sniff_image(data)
    if not ext:
        raise ValueError("Logo must be a PNG, JPG, GIF or WebP image.")

    os.makedirs(LOGO_DIR, exist_ok=True)
    # filename comes from the validated slug, never from user input
    for old in os.listdir(LOGO_DIR):
        if old.rsplit(".", 1)[0] == slug:
            os.remove(os.path.join(LOGO_DIR, old))

    filename = f"{slug}.{ext}"
    with open(os.path.join(LOGO_DIR, filename), "wb") as f:
        f.write(data)
    return f"logos/{filename}"


def _remove_logo(tenant):
    if tenant["logo_path"]:
        path = os.path.join("static", tenant["logo_path"])
        if os.path.isfile(path):
            os.remove(path)
    db.update_tenant(tenant["id"], logo_path="")


def _handle_logo_fields(tenant):
    """Shared by onboarding and settings. Returns the logo_path to store."""
    if request.form.get("remove_logo"):
        _remove_logo(tenant)
        return ""

    upload = request.files.get("logo")
    if upload and upload.filename:
        return _save_logo(tenant["slug"], upload)
    return tenant["logo_path"]


def _save_document(slug, upload, title, text):
    """Write a .txt document into the tenant's folder and rebuild its index."""
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

    with open(os.path.join(ingest.docs_dir(slug), filename), "w", encoding="utf-8") as f:
        f.write(content)

    ingest.build_index(slug)
    rag.reload_index(slug)
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
