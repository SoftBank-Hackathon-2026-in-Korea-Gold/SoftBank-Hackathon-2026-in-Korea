import os
import platform

from flask import Flask, jsonify

app = Flask(__name__)


@app.route("/")
def index():
    return jsonify(
        message="hello from flask",
        arch=platform.machine(),
        port=int(os.environ.get("PORT", 8080)),
    )


@app.route("/health")
def health():
    raise RuntimeError("database handshake failed (simulated outage)")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
