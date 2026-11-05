"""Scan roots: validation, and keeping several roots (from one or many runs) in one database."""
from __future__ import annotations

import os
import time
from itertools import combinations

from .config import ConfigNotFound, key, norm


class RootError(ConfigNotFound):
    pass


def under(child: str, parent: str) -> bool:
    """True if child is strictly inside parent (case-insensitive on Windows)."""
    c, p = key(child), key(parent)
    return c != p and c.startswith(p.rstrip(os.sep) + os.sep)


def is_under_any(path: str, roots) -> bool:
    k = key(path)
    return any(k == key(r) or under(path, r) for r in roots)


def stored(con) -> list[str]:
    """Roots already in the database (falls back to dirs.root for databases from before the roots table)."""
    rows = [r[0] for r in con.execute("SELECT root FROM roots ORDER BY root")]
    return rows or [r[0] for r in con.execute("SELECT DISTINCT root FROM dirs ORDER BY root")]


def validate(new: list[str], con=None) -> list[str]:
    """Normalise, de-duplicate and check roots. Raises RootError with a fix-it message."""
    out: list[str] = []
    for r in map(norm, new):
        if key(r) not in {key(x) for x in out}:
            out.append(r)
    if not out:
        raise RootError("no roots given: set [scan] roots in inundated.toml or pass --root PATH")
    for r in out:
        if not os.path.isdir(r):
            raise RootError(f"root does not exist or is not a folder: {r}")
    for a, b in combinations(out, 2):
        if under(a, b) or under(b, a):
            inner, outer = (a, b) if under(a, b) else (b, a)
            raise RootError(f"roots overlap in this run: {inner} is inside {outer}. Scan only one of them.")
    if con is not None:
        for r in out:
            for e in stored(con):
                if under(r, e) or under(e, r):
                    inner, outer = (r, e) if under(r, e) else (e, r)
                    raise RootError(
                        f"{r} overlaps a root already in the database ({e}): {inner} is inside {outer}.\n"
                        f"  remove the old one first:  inundated forget \"{e}\"")
    return out


def record(con, root: str, dirs: int, files: int, repos: int) -> None:
    with con:
        con.execute("INSERT OR REPLACE INTO roots VALUES (?,?,?,?,?)",
                    (root, time.strftime("%Y-%m-%d %H:%M:%S"), dirs, files, repos))


def forget(con, root: str) -> int:
    """Delete everything recorded under a root. Returns number of directories removed."""
    root = norm(root)
    n = con.execute("SELECT COUNT(*) FROM dirs WHERE root=?", (root,)).fetchone()[0]
    with con:
        for table in ("git", "items", "evidence", "classes"):
            if not con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                continue
            paths = [p for (p,) in con.execute(f"SELECT path FROM {table}") if is_under_any(p, [root])]
            con.executemany(f"DELETE FROM {table} WHERE path=?", [(p,) for p in paths])
        con.execute("DELETE FROM dirs WHERE root=?", (root,))
        con.execute("DELETE FROM roots WHERE root=?", (root,))
    return n
