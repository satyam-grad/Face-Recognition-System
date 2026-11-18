import sqlite3
from datetime import datetime

import numpy as np
from config import DB_PATH


class FaceDatabase:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_table()

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def _init_table(self):
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS faces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    encoding BLOB NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS students (
                    name TEXT PRIMARY KEY,
                    class_name TEXT NOT NULL DEFAULT '',
                    year TEXT NOT NULL DEFAULT '',
                    roll_no TEXT NOT NULL DEFAULT '',
                    created TEXT NOT NULL
                )
                """
            )

    def add_face(self, name: str, encoding: np.ndarray):
        blob = np.asarray(encoding, dtype=np.float64).tobytes()
        with self._connect() as conn:
            conn.execute("INSERT INTO faces (name, encoding) VALUES (?, ?)", (name, blob))

    def load_all(self):
        
        with self._connect() as conn:
            rows = conn.execute("SELECT name, encoding FROM faces ORDER BY id").fetchall()
        names = [r[0] for r in rows]
        encodings = [np.frombuffer(r[1], dtype=np.float64) for r in rows]
        return names, encodings

    def delete_face(self, name: str):
        
        with self._connect() as conn:
            conn.execute("DELETE FROM faces WHERE name = ?", (name,))

    def list_people(self):
        
        with self._connect() as conn:
            return conn.execute(
                "SELECT name, COUNT(*) FROM faces GROUP BY name ORDER BY name"
            ).fetchall()

    # ---------- students (details around a face identity) ----------
    def upsert_student(self, name, class_name="", year="", roll_no=""):
        created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO students (name, class_name, year, roll_no, created)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    class_name = excluded.class_name,
                    year = excluded.year,
                    roll_no = excluded.roll_no
                """,
                (name, class_name, year, roll_no, created),
            )

    def get_student(self, name):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT name, class_name, year, roll_no FROM students WHERE name = ?", (name,)
            ).fetchone()
        if row is None:
            return None
        return {"name": row[0], "class_name": row[1], "year": row[2], "roll_no": row[3]}

    def list_students(self):
        """Everyone with face samples, joined with their details, sorted by class then roll no."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT f.name,
                       COALESCE(s.class_name, ''),
                       COALESCE(s.year, ''),
                       COALESCE(s.roll_no, ''),
                       COUNT(f.id)
                FROM faces f
                LEFT JOIN students s ON s.name = f.name
                GROUP BY f.name
                ORDER BY s.class_name COLLATE NOCASE,
                         s.roll_no COLLATE NOCASE,
                         f.name COLLATE NOCASE
                """
            ).fetchall()
        return [
            {"name": r[0], "class_name": r[1], "year": r[2], "roll_no": r[3], "samples": r[4]}
            for r in rows
        ]

    def delete_student(self, name):
        with self._connect() as conn:
            conn.execute("DELETE FROM students WHERE name = ?", (name,))