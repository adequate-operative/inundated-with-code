"""Scan roots: validation, and keeping several roots (from one or many runs) in one database."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
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


def validate(new: list[str]) -> list[str]:
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
    return out


@dataclass
class Target:
    """One folder to walk, and how its results go into the database."""
    path: str                 # folder to walk
    label: str                # root it belongs to in the database
    kind: str                 # "root": (re)scan a whole root | "refresh": a subtree of an existing root
    absorbs: list[str] = field(default_factory=list)   # stored roots inside this one, replaced by it


def plan(requested: list[str], con) -> list[Target]:
    """Decide what each requested path means given what is already stored.

    - same as a stored root, or new and unrelated: scan it as a root
    - inside a stored root: refresh that subtree; it stays part of the stored root
    - contains stored roots: scan it as a root and absorb them (their data is replaced)
    Nested paths within ONE request are an error (see validate)."""
    have = stored(con)
    targets: list[Target] = []
    for r in validate(requested):
        outer = next((e for e in have if under(r, e)), None)
        if outer:
            known = {key(p): p for (p,) in con.execute("SELECT path FROM dirs WHERE root=?", (outer,))}
            p = r
            while key(p) not in known and key(p) != key(outer):
                p = os.path.dirname(p)   # nearest folder the database already knows
            p = known.get(key(p), outer)
            targets.append(Target(p, outer, "refresh" if key(p) != key(outer) else "root"))
        else:
            same = next((e for e in have if key(e) == key(r)), r)
            targets.append(Target(same, same, "root", [e for e in have if under(e, r)]))
    # a full rescan of a root makes refreshes inside it redundant
    full = {key(t.label) for t in targets if t.kind == "root"}
    return [t for t in targets if t.kind == "root" or key(t.label) not in full]


def drop_root_rows(con, root: str) -> None:
    """Remove a stored root's directory rows and roots entry (used when a parent absorbs it)."""
    with con:
        con.execute("DELETE FROM dirs WHERE root=?", (root,))
        con.execute("DELETE FROM roots WHERE root=?", (root,))


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
