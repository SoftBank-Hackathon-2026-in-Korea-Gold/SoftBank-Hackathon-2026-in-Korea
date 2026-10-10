import os
import platform

from flask import Flask, jsonify

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = Flask(__name__)


@app.route("/")
def index():
    return jsonify(
        message="hello from flask",
        arch=platform.machine(),
        port=int(os.environ.get("PORT", 8080)),
        uploads=len(os.listdir(UPLOAD_DIR)),
    )


@app.route("/health")
def health():
    return jsonify(status="ok"), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
