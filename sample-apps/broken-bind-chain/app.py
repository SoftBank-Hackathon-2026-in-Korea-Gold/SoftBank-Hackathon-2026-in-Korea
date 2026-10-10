import json
import os
from pathlib import Path

from flask import Flask, jsonify

app = Flask(__name__)


def build_info():
    path = Path(__file__).with_name("build-info.json")
    return json.loads(path.read_text()) if path.exists() else {}


@app.route("/")
def index():
    return jsonify(
        message="hello from flask",
        port=int(os.environ.get("PORT", 8080)),
        build=build_info(),
    )


@app.route("/health")
def health():
    return jsonify(status="ok"), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
