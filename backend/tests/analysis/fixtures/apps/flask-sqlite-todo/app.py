import os
import sqlite3

from flask import Flask, flash, redirect, render_template_string, request, url_for

DB_PATH = "todo.db"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev")

PAGE = """
<!doctype html>
<title>할 일 목록</title>
<h1>할 일 목록</h1>
{% for msg in get_flashed_messages() %}<p style="color:green">{{ msg }}</p>{% endfor %}
<form method="post" action="{{ url_for('add') }}">
  <input name="title" placeholder="할 일" required>
  <button>추가</button>
</form>
<ul>
{% for t in todos %}
  <li>
    {% if t['done'] %}<s>{{ t['title'] }}</s>{% else %}{{ t['title'] }}
    <form method="post" action="{{ url_for('done', todo_id=t['id']) }}" style="display:inline">
      <button>완료</button>
    </form>{% endif %}
  </li>
{% endfor %}
</ul>
"""


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS todos ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, done INTEGER DEFAULT 0)"
        )


init_db()


@app.get("/")
def index():
    with get_db() as conn:
        todos = conn.execute("SELECT * FROM todos ORDER BY id DESC").fetchall()
    return render_template_string(PAGE, todos=todos)


@app.post("/add")
def add():
    title = request.form.get("title", "").strip()
    if title:
        with get_db() as conn:
            conn.execute("INSERT INTO todos (title) VALUES (?)", (title,))
        flash("추가했습니다.")
    return redirect(url_for("index"))


@app.post("/done/<int:todo_id>")
def done(todo_id):
    with get_db() as conn:
        conn.execute("UPDATE todos SET done = 1 WHERE id = ?", (todo_id,))
    flash("완료 처리했습니다.")
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(debug=True)
