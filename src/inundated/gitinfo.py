"""Git facts per repo root (read-only commands only)."""
from __future__ import annotations

import re
import subprocess
from concurrent.futures import ThreadPoolExecutor

from .db import j, uj


def git(path: str, *args: str, timeout: float = 30) -> tuple[int, str, str]:
    try:
        r = subprocess.run(
            ["git", "-c", "safe.directory=*", "-c", "core.quotepath=off", "-C", path, *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, stdin=subprocess.DEVNULL,
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except OSError as e:
        return 127, "", str(e)


def collect(path: str) -> dict:
    out = {"path": path, "ok": 0}
    rc, so, se = git(path, "rev-parse", "--show-toplevel")
    if rc:
        out["error"] = (se or so)[:400]
        return out
    out.update(ok=1, toplevel=so.splitlines()[-1])
    remotes: dict[str, str] = {}
    rc, so, _ = git(path, "remote", "-v")
    for line in so.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == "(fetch)":
            remotes[parts[0]] = parts[1]
        elif len(parts) >= 2 and parts[0] not in remotes:
            remotes[parts[0]] = parts[1]
    out["remotes"] = remotes
    rc, so, _ = git(path, "rev-parse", "--abbrev-ref", "HEAD")
    out["branch"] = so if rc == 0 else None
    rc, so, _ = git(path, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    out["branches"] = so.splitlines()[:50] if rc == 0 else []
    rc, so, _ = git(path, "rev-list", "--count", "--all")
    out["commit_count"] = int(so) if rc == 0 and so.isdigit() else 0
    if out["commit_count"]:
        rc, so, _ = git(path, "log", "-1", "--all", "--format=%cI")
        out["last_commit"] = so or None
        rc, so, _ = git(path, "log", "--all", "--max-parents=0", "--format=%cI")
        out["first_commit"] = min(so.splitlines()) if so else None
        rc, so, _ = git(path, "rev-list", "--count", "--branches", "--not", "--remotes")
        out["unpushed"] = int(so) if rc == 0 and so.isdigit() else None
        rc, so, _ = git(path, "shortlog", "-sne", "--all")
        out["authors"] = [l.strip() for l in so.splitlines()[:8]]
        rc, so, _ = git(path, "log", "-12", "--all", "--date-order", "--format=%cs %s")
        out["subjects"] = so.splitlines()
    rc, so, _ = git(path, "status", "--porcelain", timeout=90)
    out["dirty"] = len(so.splitlines()) if rc == 0 else None
    return out


def collect_all(con, paths: list[str], roots: list[str], workers: int = 8, log=print) -> None:
    rows = []
    with ThreadPoolExecutor(workers) as ex:
        for i, info in enumerate(ex.map(collect, paths), 1):
            rows.append(info)
            if i % 25 == 0:
                log(f"  … git {i}/{len(paths)}")
    with con:
        from .roots import is_under_any
        old = [p for (p,) in con.execute("SELECT path FROM git") if is_under_any(p, roots)]
        con.executemany("DELETE FROM git WHERE path=?", [(p,) for p in old])
        con.executemany(
            "INSERT INTO git VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(r["path"], r["ok"], r.get("error"), r.get("toplevel"), r.get("branch"), j(r.get("branches", [])),
              j(r.get("remotes", {})), r.get("first_commit"), r.get("last_commit"), r.get("commit_count"),
              r.get("dirty"), r.get("unpushed"), j(r.get("authors", [])), j(r.get("subjects", [])))
             for r in rows],
        )


def load(con) -> dict[str, dict]:
    out = {}
    for r in con.execute("SELECT * FROM git"):
        d = dict(r)
        for k in ("branches", "remotes", "authors", "subjects"):
            d[k] = uj(d[k], [] if k != "remotes" else {})
        out[d["path"]] = d
    return out


_SCP = re.compile(r"^(?:(?P<user>[^@/\s]+)@)?(?P<host>[^:/\s]+):(?!//)(?P<path>.+)$")
_URL = re.compile(r"^(?P<scheme>[a-z][a-z0-9+.-]*)://(?:(?P<user>[^@/]+)@)?(?P<host>[^/:]+)(?::\d+)?/(?P<path>.*)$", re.I)


def parse_remote(url: str) -> dict:
    """-> {host, owner, repo, kind}; kind in ssh/https/git/local."""
    u = url.strip()
    if re.match(r"^[A-Za-z]:[\\/]", u) or u.startswith(("\\\\", "/", "file://", ".")):
        return {"host": "local", "owner": None, "repo": u.rstrip("/\\").split("/")[-1].split("\\")[-1], "kind": "local"}
    m = _URL.match(u)
    kind = None
    if m:
        kind = m["scheme"].lower()
    else:
        m = _SCP.match(u)
        kind = "ssh"
    if not m:
        return {"host": None, "owner": None, "repo": None, "kind": "unknown"}
    parts = [p for p in m["path"].split("/") if p]
    repo = parts[-1].removesuffix(".git") if parts else None
    owner = parts[-2] if len(parts) >= 2 else None
    if owner is None and m["user"] and m["user"] not in ("git",):
        owner = m["user"]
    return {"host": m["host"].lower(), "owner": owner, "repo": repo, "kind": kind,
            "user": m["user"]}


def ownership(remotes: dict[str, str], handles: set[str], self_hosts: set[str] = frozenset()) -> str:
    """'mine' | 'other' | 'local' | 'none' based on remote owners/users."""
    if not remotes:
        return "none"
    parsed = [parse_remote(u) for u in remotes.values()]
    for p in parsed:
        if (p.get("owner") or "").lower() in handles or (p.get("user") or "").lower() in handles:
            return "mine"
    if all(p["host"] in ("local", None) for p in parsed):
        return "local"
    if all((p["host"] or "") in self_hosts or p["host"] == "local" for p in parsed):
        return "mine"  # only on your own servers
    return "other"
