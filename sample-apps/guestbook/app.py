"""Guestbook: the smallest app with real state. Reads DATABASE_URL (PostgreSQL) and falls back to a local
SQLite file, so the *same image* runs against a Postgres sidecar (local/node) or Cloud SQL (cloudrun)."""

import os
import platform
import sqlite3
from datetime import datetime, timezone

from flask import Flask, jsonify, redirect, request

app = Flask(__name__)
DATABASE_URL = os.environ.get("DATABASE_URL", "")
SQLITE_PATH = os.environ.get("SQLITE_PATH", "guestbook.db")
IS_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))


def connect():
    if IS_PG:
        import psycopg

        return psycopg.connect(DATABASE_URL, autocommit=True)
    conn = sqlite3.connect(SQLITE_PATH)
    conn.isolation_level = None
    return conn


def q(sql: str) -> str:
    return sql if IS_PG else sql.replace("%s", "?")


def init_db() -> None:
    with connect() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS entries (id SERIAL PRIMARY KEY, name TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL)"
            if IS_PG
            else "CREATE TABLE IF NOT EXISTS entries (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL)"
        )


def storage_label() -> str:
    if IS_PG:
        host = DATABASE_URL.split("@", 1)[-1].split("/", 1)[0] or "unix-socket"
        return f"postgresql @ {host}"
    return f"sqlite @ {SQLITE_PATH} (ephemeral on Cloud Run)"


@app.get("/health")
def health():
    try:
        with connect() as c:
            c.execute("SELECT 1")
        return jsonify(status="ok", storage=storage_label()), 200
    except Exception as e:  # noqa: BLE001
        return jsonify(status="db_error", error=str(e)[:200]), 500


@app.get("/api/entries")
def api_entries():
    with connect() as c:
        rows = c.execute("SELECT id, name, message, created_at FROM entries ORDER BY id DESC LIMIT 50").fetchall()
    return jsonify([{"id": r[0], "name": r[1], "message": r[2], "created_at": r[3]} for r in rows])


@app.post("/entries")
def add_entry():
    data = request.form if request.form else (request.get_json(silent=True) or {})
    name, message = (data.get("name") or "anon").strip()[:40], (data.get("message") or "").strip()[:280]
    if message:
        with connect() as c:
            c.execute(q("INSERT INTO entries (name, message, created_at) VALUES (%s, %s, %s)"),
                      (name, message, datetime.now(timezone.utc).isoformat(timespec="seconds")))
    return redirect("/") if request.form else (jsonify(ok=True), 201)


@app.get("/")
def index():
    with connect() as c:
        rows = c.execute("SELECT name, message, created_at FROM entries ORDER BY id DESC LIMIT 50").fetchall()
    items = "".join(f"<li><b>{n}</b>: {m} <small>{t}</small></li>" for n, m, t in rows) or "<li><i>아직 글이 없습니다</i></li>"
    return f"""<!doctype html><meta charset=utf-8><title>CloudMorph Guestbook</title>
<style>body{{font-family:system-ui;max-width:640px;margin:40px auto;padding:0 16px}}small{{color:#888}}input,button{{padding:8px}}</style>
<h1>방명록</h1><p>storage: <code>{storage_label()}</code> · arch: {platform.machine()} · host: {platform.node()}</p>
<form method=post action=/entries><input name=name placeholder=이름 size=10> <input name=message placeholder=메시지 size=40 required> <button>남기기</button></form>
<ul>{items}</ul>"""


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
