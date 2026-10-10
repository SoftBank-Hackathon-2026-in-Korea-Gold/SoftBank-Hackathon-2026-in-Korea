import os
import platform

from flask import Flask, jsonify

import config

app = Flask(__name__)
app.config.from_mapping(config.load())


@app.route("/")
def index():
    return jsonify(
        message="hello from flask",
        env=os.environ.get("APP_ENV", "production"),
        db_host=app.config["DB_HOST"],
        arch=platform.machine(),
    )


@app.route("/health")
def health():
    return jsonify(status="ok"), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
