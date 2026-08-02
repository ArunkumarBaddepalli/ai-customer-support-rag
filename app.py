"""Flask app — ties the chat page to rag.py, stores history in SQLite,
and exposes a password-gated /admin page for uploading new FAQ documents."""

import os
from functools import wraps

from dotenv import load_dotenv

load_dotenv()

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from werkzeug.utils import secure_filename

import config
import db
import ingest
import rag

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24))
db.init_db()

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not ADMIN_PASSWORD:
            return "Admin page is not configured (set ADMIN_PASSWORD).", 503
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)
    return wrapped


@app.route("/")
def index():
    return render_template(
        "index.html",
        company_name=config.COMPANY_NAME,
        company_tagline=config.COMPANY_TAGLINE,
        logo_emoji=config.LOGO_EMOJI,
        brand_color=config.BRAND_COLOR,
    )


@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"error": "question is required"}), 400

    result = rag.ask(question)
    db.save_message(question, result["answer"], result["sources"])
    return jsonify(result)


@app.route("/api/history")
def history():
    return jsonify(db.get_history())


@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    if not ADMIN_PASSWORD:
        return "Admin page is not configured (set ADMIN_PASSWORD).", 503

    error = None
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["is_admin"] = True
            return redirect(url_for("admin_upload"))
        error = "Wrong password."

    return render_template("admin_login.html", error=error, company_name=config.COMPANY_NAME)


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_login"))


@app.route("/admin/upload", methods=["GET", "POST"])
@admin_required
def admin_upload():
    message = None
    error = None

    if request.method == "POST":
        upload = request.files.get("file")
        title = (request.form.get("title") or "").strip()
        text = (request.form.get("text") or "").strip()

        try:
            if upload and upload.filename:
                filename = secure_filename(upload.filename)
                if not filename.endswith(".txt"):
                    raise ValueError("Only .txt files are supported.")
                content = upload.read().decode("utf-8")
            elif title and text:
                filename = secure_filename(title)
                if not filename.endswith(".txt"):
                    filename += ".txt"
                content = text
            else:
                raise ValueError("Provide either a .txt file or a title + text.")

            if not content.strip():
                raise ValueError("Document is empty.")

            path = os.path.join(ingest.DOCS_DIR, filename)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)

            ingest.build_index()
            rag.reload_index()
            message = f"Saved '{filename}' and rebuilt the search index."
        except Exception as exc:
            error = str(exc)

    documents = sorted(
        f for f in os.listdir(ingest.DOCS_DIR) if f.endswith(".txt")
    )
    return render_template(
        "admin_upload.html",
        documents=documents,
        message=message,
        error=error,
        company_name=config.COMPANY_NAME,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", debug=debug, port=port)
