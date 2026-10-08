import os
from datetime import datetime
from html import escape

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import DateTime, Integer, String, create_engine, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)


class Base(DeclarativeBase):
    pass


class Note(Base):
    __tablename__ = "notes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    author_email: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


app = FastAPI(title="Notes")


@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(engine)


@app.get("/", response_class=HTMLResponse)
def index():
    with Session(engine) as s:
        notes = s.scalars(select(Note).order_by(Note.id.desc())).all()
    items = "".join(
        f"<li>{escape(n.title)} — {escape(n.author_email)} ({n.created_at:%Y-%m-%d %H:%M})</li>"
        for n in notes
    )
    return f"""<h1>메모장</h1>
<form method="post" action="/notes">
  <input name="title" placeholder="제목" required>
  <input name="author_email" type="email" placeholder="이메일" required>
  <button>저장</button>
</form>
<ul>{items}</ul>"""


@app.post("/notes")
def create_note(title: str = Form(...), author_email: str = Form(...)):
    with Session(engine) as s:
        s.add(Note(title=title, author_email=author_email))
        s.commit()
    return RedirectResponse("/", status_code=303)


@app.get("/health")
def health():
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return {"status": "ok"}
