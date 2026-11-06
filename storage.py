import hashlib
import json
import sqlite3
from pathlib import Path

from exams import Question


SCHEMA = """
CREATE TABLE IF NOT EXISTS exams (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    exam_date TEXT NOT NULL,
    digest TEXT NOT NULL UNIQUE,
    published INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS questions (
    exam_id INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
    number INTEGER NOT NULL,
    text TEXT NOT NULL,
    options TEXT NOT NULL,
    correct TEXT NOT NULL CHECK(correct IN ('a','b','c','d')),
    PRIMARY KEY (exam_id, number)
);
CREATE TABLE IF NOT EXISTS images (
    exam_id INTEGER NOT NULL,
    number INTEGER NOT NULL,
    position INTEGER NOT NULL,
    content BLOB NOT NULL,
    PRIMARY KEY (exam_id, number, position),
    FOREIGN KEY (exam_id, number) REFERENCES questions(exam_id, number) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS login_attempts (
    address TEXT PRIMARY KEY,
    failures INTEGER NOT NULL,
    started REAL NOT NULL
);
"""


def connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def insert_exam(
    db: sqlite3.Connection, content: bytes, questions: list[Question],
    title: str, exam_date: str, published: bool = False,
) -> int:
    with db:
        cursor = db.execute(
            "INSERT INTO exams(title, exam_date, digest, published) VALUES (?, ?, ?, ?)",
            (title, exam_date, hashlib.sha256(content).hexdigest(), int(published)),
        )
        exam_id = cursor.lastrowid
        if exam_id is None:
            raise RuntimeError("No se pudo crear el examen.")
        for question in questions:
            db.execute(
                "INSERT INTO questions VALUES (?, ?, ?, ?, ?)",
                (exam_id, question.number, question.text, json.dumps(question.options), question.correct),
            )
            db.executemany(
                "INSERT INTO images VALUES (?, ?, ?, ?)",
                [(exam_id, question.number, i, data) for i, data in enumerate(question.images)],
            )
    return exam_id


def load_questions(db: sqlite3.Connection, exam_id: int) -> list[dict]:
    result = []
    image_counts = {
        row["number"]: row["count"] for row in db.execute(
            "SELECT number, COUNT(*) AS count FROM images WHERE exam_id = ? GROUP BY number",
            (exam_id,),
        )
    }
    for row in db.execute("SELECT * FROM questions WHERE exam_id = ? ORDER BY number", (exam_id,)):
        result.append({
            "number": row["number"], "text": row["text"],
            "options": json.loads(row["options"]), "correct": row["correct"],
            "image_count": image_counts.get(row["number"], 0),
        })
    return result
