from flask import Flask, jsonify

app = Flask(__name__)


@app.route("/")
def index():
    return jsonify(message="hello from broken app")


@app.route("/health")
def health():
    return jsonify(status="ok"), 200


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000)  # 의도된 결함: 호스트/포트 하드코딩
