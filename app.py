"""Flask app — ties the chat page to rag.py and stores history in SQLite."""

from flask import Flask, jsonify, render_template, request

import db
import rag

app = Flask(__name__)
db.init_db()


@app.route("/")
def index():
    return render_template("index.html")


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


if __name__ == "__main__":
    app.run(debug=True, port=5001)
