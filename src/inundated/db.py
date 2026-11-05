from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS dirs (
    path TEXT PRIMARY KEY, parent TEXT, root TEXT, depth INTEGER,
    n_files INTEGER, n_bytes INTEGER, newest REAL, oldest REAL,
    t_files INTEGER, t_bytes INTEGER, t_newest REAL, t_projecty INTEGER,
    ext TEXT, t_ext TEXT, markers TEXT, pruned TEXT, git TEXT, error TEXT
);
CREATE INDEX IF NOT EXISTS dirs_parent ON dirs(parent);
CREATE TABLE IF NOT EXISTS git (
    path TEXT PRIMARY KEY, ok INTEGER, error TEXT, toplevel TEXT, branch TEXT, branches TEXT,
    remotes TEXT, first_commit TEXT, last_commit TEXT, commit_count INTEGER,
    dirty INTEGER, unpushed INTEGER, authors TEXT, subjects TEXT
);
CREATE TABLE IF NOT EXISTS items (
    path TEXT PRIMARY KEY, verdict TEXT, kind TEXT, confidence REAL, source TEXT,
    reason TEXT, parent_item TEXT, container TEXT, heuristic TEXT
);
CREATE TABLE IF NOT EXISTS overrides (path TEXT PRIMARY KEY, verdict TEXT, kind TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS llm_cache (
    k TEXT PRIMARY KEY, backend TEXT, model TEXT, prompt TEXT, response TEXT,
    tokens_in INTEGER, tokens_out INTEGER, created TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS evidence (path TEXT PRIMARY KEY, hash TEXT, text TEXT, chars INTEGER);
"""


def connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def j(x) -> str:
    return json.dumps(x, ensure_ascii=False)


def uj(s, default=None):
    return json.loads(s) if s else default
