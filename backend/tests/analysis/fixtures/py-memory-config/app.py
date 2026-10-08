import psycopg
from flask import Flask, jsonify

DB_URL = "postgresql://admin:supersecret@10.0.0.5:5432/shop"
API_TOKEN = "sk-live-1234567890"

app = Flask(__name__)
visits = {}


@app.get("/")
def index():
    visits["home"] = visits.get("home", 0) + 1
    with psycopg.connect(DB_URL) as conn:
        n = conn.execute("SELECT count(*) FROM orders").fetchone()[0]
    return jsonify(orders=n, visits=visits["home"])


@app.get("/health")
def health():
    return {"ok": True}


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000)
