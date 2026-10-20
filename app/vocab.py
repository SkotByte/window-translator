"""Personal vocabulary list + spaced-repetition flashcards.

Every word you look up is saved here. Review uses a simplified SM-2 schedule (the
algorithm Anki is based on): words you remember come back after growing intervals,
words you forget come back soon.
"""
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

AGAIN, HARD, GOOD, EASY = 1, 2, 3, 4
RELEARN_DELAY = timedelta(minutes=10)
MIN_EASE = 1.3


@dataclass
class CardState:
    interval: float = 0.0   # days
    ease: float = 2.5
    reps: int = 0           # successful reviews in a row
    lapses: int = 0


def schedule(state: CardState, grade: int) -> tuple[CardState, timedelta]:
    """Return the new card state and how long until it is due again."""
    s = CardState(state.interval, state.ease, state.reps, state.lapses)
    if grade == AGAIN:
        s.reps, s.interval = 0, 0.0
        s.lapses += 1
        s.ease = max(MIN_EASE, s.ease - 0.2)
        return s, RELEARN_DELAY
    if grade == HARD:
        s.interval = 1.0 if s.reps == 0 else max(1.0, s.interval * 1.2)
        s.ease = max(MIN_EASE, s.ease - 0.15)
    elif grade == GOOD:
        s.interval = 1.0 if s.reps == 0 else 3.0 if s.reps == 1 else s.interval * s.ease
    else:  # EASY
        s.interval = 4.0 if s.reps == 0 else max(4.0, s.interval * s.ease * 1.3)
        s.ease += 0.15
    s.reps += 1
    return s, timedelta(days=s.interval)


def describe_delay(delay: timedelta) -> str:
    """Human-friendly Thai text for a delay, e.g. '10 นาที', '3 วัน'."""
    minutes = delay.total_seconds() / 60
    if minutes < 60:
        return f"{round(minutes)} นาที"
    days = delay.total_seconds() / 86400
    if days < 30:
        return f"{round(days)} วัน"
    if days < 365:
        return f"{round(days / 30)} เดือน"
    return f"{days / 365:.1f} ปี"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Vocab:
    def __init__(self, db_path: Path):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.lock = threading.Lock()
        self.conn.execute("""CREATE TABLE IF NOT EXISTS words (
            word TEXT PRIMARY KEY,
            meaning TEXT,
            cat TEXT,
            context_en TEXT,
            context_th TEXT,
            lookups INTEGER NOT NULL DEFAULT 1,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL)""")
        # Flashcard columns (added later; migrate older databases in place)
        existing = {r[1] for r in self.conn.execute("PRAGMA table_info(words)")}
        for col, ddl in (("due", "TEXT"), ("interval", "REAL NOT NULL DEFAULT 0"),
                         ("ease", "REAL NOT NULL DEFAULT 2.5"), ("reps", "INTEGER NOT NULL DEFAULT 0"),
                         ("lapses", "INTEGER NOT NULL DEFAULT 0")):
            if col not in existing:
                self.conn.execute(f"ALTER TABLE words ADD COLUMN {col} {ddl}")
        self.conn.execute("UPDATE words SET due = first_seen WHERE due IS NULL")
        self.conn.commit()

    def record(self, word: str, meaning: str, cat: str, context_en: str, context_th: str):
        """Insert a new word or bump its lookup counter (keeps the newest context sentence)."""
        now = _now()
        with self.lock:
            self.conn.execute(
                """INSERT INTO words (word, meaning, cat, context_en, context_th, first_seen, last_seen, due)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(word) DO UPDATE SET lookups = lookups + 1, last_seen = excluded.last_seen,
                       context_en = excluded.context_en, context_th = excluded.context_th""",
                (word, meaning, cat, context_en, context_th, now, now, now))
            self.conn.commit()

    def all(self) -> list[tuple]:
        with self.lock:
            return self.conn.execute(
                "SELECT word, meaning, cat, lookups, last_seen, context_en, context_th, due "
                "FROM words ORDER BY last_seen DESC").fetchall()

    def delete(self, word: str):
        with self.lock:
            self.conn.execute("DELETE FROM words WHERE word = ?", (word,))
            self.conn.commit()

    # ---- flashcards -------------------------------------------------------------
    def due_cards(self, limit: int = 50) -> list[dict]:
        """Cards due now; never-reviewed (new) words come after overdue ones."""
        with self.lock:
            cur = self.conn.execute(
                """SELECT word, meaning, cat, context_en, context_th, interval, ease, reps, lapses
                   FROM words WHERE due <= ? ORDER BY reps = 0, due LIMIT ?""", (_now(), limit))
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur]

    def stats(self) -> dict:
        with self.lock:
            due, new, total, learned = self.conn.execute(
                """SELECT SUM(due <= ?), SUM(reps = 0), COUNT(*), SUM(interval >= 21) FROM words""",
                (_now(),)).fetchone()
        return {"due": due or 0, "new": new or 0, "total": total or 0, "learned": learned or 0}

    def review(self, word: str, grade: int) -> timedelta:
        """Save a review result; returns the delay until the card is due again."""
        with self.lock:
            row = self.conn.execute("SELECT interval, ease, reps, lapses FROM words WHERE word = ?",
                                    (word,)).fetchone()
            if row is None:
                return timedelta(0)
            state, delay = schedule(CardState(*row), grade)
            self.conn.execute(
                "UPDATE words SET interval = ?, ease = ?, reps = ?, lapses = ?, due = ? WHERE word = ?",
                (state.interval, state.ease, state.reps, state.lapses,
                 (datetime.now() + delay).isoformat(timespec="seconds"), word))
            self.conn.commit()
        return delay
