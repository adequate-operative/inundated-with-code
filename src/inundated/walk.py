"""Single filesystem pass: per-directory aggregates, markers, pruned folders, git roots."""
from __future__ import annotations

import os
import sys
import time
from collections import Counter
from dataclasses import dataclass, field

from . import filetypes as ft
from .config import Config, key
from .db import j, uj


@dataclass
class Dir:
    path: str
    parent: str | None
    root: str
    depth: int
    n_files: int = 0
    n_bytes: int = 0
    newest: float = 0.0
    oldest: float = 0.0
    ext: Counter = field(default_factory=Counter)
    markers: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    git: str | None = None          # "dir" | "file" (worktree/submodule pointer) | None
    error: str | None = None
    children: list[str] = field(default_factory=list)
    # subtree totals (self included)
    t_files: int = 0
    t_bytes: int = 0
    t_newest: float = 0.0
    t_ext: Counter = field(default_factory=Counter)
    t_projecty: int = 0             # descendants (not self) that look like project roots

    @property
    def name(self) -> str:
        return os.path.basename(self.path) or self.path

    @property
    def strong(self) -> list[str]:
        return [m for m in self.markers if m in ft.STRONG_MARKERS]

    @property
    def projecty(self) -> bool:
        return bool(self.git or self.strong)


def _is_env(path: str) -> bool:
    return os.path.isfile(os.path.join(path, "pyvenv.cfg")) or os.path.isdir(os.path.join(path, "conda-meta"))


def walk(cfg: Config, roots: list[str], log=print) -> dict[str, Dir]:
    dirs: dict[str, Dir] = {}
    skip = cfg.prune_always | cfg.prune
    t0, last = time.time(), time.time()
    for root in roots:
        stack = [(root, None, 0)]
        while stack:
            path, parent, depth = stack.pop()
            d = Dir(path=path, parent=parent, root=root, depth=depth)
            dirs[path] = d
            fnames, dnames = [], []
            try:
                it = os.scandir(path)
            except OSError as e:
                d.error = f"{type(e).__name__}: {e}"
                continue
            with it:
                for e in it:
                    low = e.name.lower()
                    try:
                        if e.is_symlink() or (hasattr(e, "is_junction") and e.is_junction()):
                            continue
                        if e.is_dir(follow_symlinks=False):
                            dnames.append(e.name)
                            if low == ".git":
                                d.git = "dir"
                            elif low in cfg.exclude_names or (cfg.exclude_paths and key(e.path) in cfg.exclude_paths):
                                d.pruned.append(e.name + " (excluded)")
                            elif low in skip:
                                d.pruned.append(e.name)
                            elif _is_env(e.path):
                                d.pruned.append(e.name + " (python env)")
                            else:
                                child = os.path.join(path, e.name)
                                d.children.append(child)
                                stack.append((child, path, depth + 1))
                            continue
                        if low == ".git":
                            d.git = "file"
                            continue
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    fnames.append(e.name)
                    d.n_files += 1
                    d.n_bytes += st.st_size
                    m = st.st_mtime
                    d.newest = max(d.newest, m)
                    d.oldest = m if not d.oldest else min(d.oldest, m)
                    d.ext[os.path.splitext(low)[1] or (low if low in ft.TEXT_NAMES else "")] += 1
            d.markers = ft.markers_for(path, fnames, dnames)
            if any(p.lower() == ".godot" for p in d.pruned) and "godot" not in d.markers:
                d.markers.append("godot-cache")
            if time.time() - last > 5:
                last = time.time()
                log(f"  … {len(dirs):,} dirs, {time.time() - t0:.0f}s  ({path[:90]})", file=sys.stderr)

    # bottom-up aggregation
    for d in sorted(dirs.values(), key=lambda x: -x.depth):
        d.t_files += d.n_files
        d.t_bytes += d.n_bytes
        d.t_newest = max(d.t_newest, d.newest)
        d.t_ext.update(d.ext)
        if d.parent and d.parent in dirs:
            p = dirs[d.parent]
            p.t_files += d.t_files
            p.t_bytes += d.t_bytes
            p.t_newest = max(p.t_newest, d.t_newest)
            p.t_ext.update(d.t_ext)
            p.t_projecty += d.t_projecty + (1 if d.projecty else 0)
    return dirs


def _insert(con, ds) -> None:
    con.executemany(
        "INSERT OR REPLACE INTO dirs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(d.path, d.parent, d.root, d.depth, d.n_files, d.n_bytes, d.newest, d.oldest,
          d.t_files, d.t_bytes, d.t_newest, d.t_projecty,
          j(dict(d.ext.most_common(25))), j(dict(d.t_ext.most_common(25))),
          j(d.markers), j(d.pruned), d.git, d.error) for d in ds],
    )


def save(con, dirs: dict[str, Dir], roots: list[str]) -> None:
    """Replace the rows of these roots only; other roots in the database are untouched."""
    with con:
        con.executemany("DELETE FROM dirs WHERE root=?", [(r,) for r in roots])
        _insert(con, dirs.values())


def save_subtree(con, ds: list[Dir], sub: str, label: str) -> None:
    """Replace one subtree of a stored root with a fresh walk of it, then fix the totals of
    every folder above it. `ds` was walked with `sub` as its own root."""
    from .roots import is_under_any
    parent, base = con.execute("SELECT parent, depth FROM dirs WHERE path=?", (sub,)).fetchone()
    for d in ds:
        d.root = label
        d.depth += base
        if d.path == sub:
            d.parent = parent
    with con:
        old = [p for (p,) in con.execute("SELECT path FROM dirs WHERE root=?", (label,)) if is_under_any(p, [sub])]
        con.executemany("DELETE FROM dirs WHERE path=?", [(p,) for p in old])
        _insert(con, ds)
        _reaggregate(con, parent)


def _reaggregate(con, path: str | None) -> None:
    """Recompute subtree totals for `path` and each ancestor, from their children's rows."""
    while path:
        row = con.execute("SELECT n_files, n_bytes, newest, ext, parent FROM dirs WHERE path=?", (path,)).fetchone()
        if not row:
            return
        n_files, n_bytes, newest, ext, parent = row
        t_files, t_bytes, t_newest, t_ext, t_proj = n_files, n_bytes, newest, Counter(uj(ext, {})), 0
        for cf, cb, cn, cext, cproj, cmark, cgit in con.execute(
                "SELECT t_files, t_bytes, t_newest, t_ext, t_projecty, markers, git FROM dirs WHERE parent=?", (path,)):
            t_files += cf
            t_bytes += cb
            t_newest = max(t_newest, cn)
            t_ext.update(uj(cext, {}))
            t_proj += cproj + (1 if cgit or any(m in ft.STRONG_MARKERS for m in uj(cmark, [])) else 0)
        con.execute("UPDATE dirs SET t_files=?, t_bytes=?, t_newest=?, t_ext=?, t_projecty=? WHERE path=?",
                    (t_files, t_bytes, t_newest, j(dict(t_ext.most_common(25))), t_proj, path))
        path = parent


def load(con) -> dict[str, Dir]:
    dirs: dict[str, Dir] = {}
    for r in con.execute("SELECT * FROM dirs"):
        dirs[r["path"]] = Dir(
            path=r["path"], parent=r["parent"], root=r["root"], depth=r["depth"],
            n_files=r["n_files"], n_bytes=r["n_bytes"], newest=r["newest"], oldest=r["oldest"],
            ext=Counter(uj(r["ext"], {})), markers=uj(r["markers"], []), pruned=uj(r["pruned"], []),
            git=r["git"], error=r["error"], t_files=r["t_files"], t_bytes=r["t_bytes"],
            t_newest=r["t_newest"], t_ext=Counter(uj(r["t_ext"], {})), t_projecty=r["t_projecty"],
        )
    for d in dirs.values():
        if d.parent in dirs:
            dirs[d.parent].children.append(d.path)
    for d in dirs.values():
        d.children.sort(key=str.lower)
    return dirs


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def ext_summary(c: Counter, k: int = 6) -> str:
    return ", ".join(f"{e or '(none)'}×{n}" for e, n in c.most_common(k))


def category_mix(c: Counter) -> Counter:
    mix: Counter = Counter()
    for e, n in c.items():
        mix[ft.category(e) if e.startswith(".") else ("docs" if e in ft.TEXT_NAMES else "other")] += n
    return mix
