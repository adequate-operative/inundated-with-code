from __future__ import annotations

import argparse
import sys
import time

from . import config as C
from . import cards, classify, db, discover, evidence, gitinfo, report, walk
from .context import judge_context
from .llm import make_client


def log(*a, **k):
    k.setdefault("file", sys.stderr)
    print(*a, **k, flush=True)


def _ctx(args):
    cfg = C.load(args.config)
    con = db.connect(cfg.db_path)
    return cfg, con


def cmd_scan(args):
    cfg, con = _ctx(args)
    t = time.time()
    log(f"walking {cfg.roots} …")
    dirs = walk.walk(cfg, log=log)
    walk.save(con, dirs)
    repos = [d.path for d in dirs.values() if d.git]
    log(f"{len(dirs):,} dirs, {sum(d.n_files for d in dirs.values()):,} files, {len(repos)} git roots "
        f"({time.time() - t:.0f}s)")
    if not args.no_git:
        log("collecting git facts …")
        gitinfo.collect_all(con, repos, cfg.git_workers, log=log)
    log(f"done in {time.time() - t:.0f}s → {cfg.db_path}")


def _loaded(con):
    return walk.load(con), gitinfo.load(con)


def _client(cfg, con, name=None):
    name = name or cfg.judge_backend
    if name not in cfg.backends:
        sys.exit(f"no [backends.{name}] in config")
    return make_client(cfg.backends[name], con)


def cmd_discover(args):
    cfg, con = _ctx(args)
    dirs, gits = _loaded(con)
    judge = args.judge or cfg.judge
    client = None if judge == "heuristic" else _client(cfg, con, args.backend)
    res = discover.discover(cfg, con, dirs, gits, judge=judge, client=client, log=log)
    from collections import Counter
    c = Counter(v.verdict for v in res.values())
    s = Counter(v.source for v in res.values())
    log(f"verdicts: {dict(c)}   sources: {dict(s)}")
    if client:
        log(f"judge {client.b.model}: {client.calls} calls, {client.cache_hits} cached, {client.tokens_in} tok in / {client.tokens_out} out"
            + (f", {client.truncated} truncated" if getattr(client, "truncated", 0) else ""))


def cmd_context(args):
    cfg, con = _ctx(args)
    dirs, gits = _loaded(con)
    p = C.norm(args.path)
    d = dirs.get(p) or next((v for k, v in dirs.items() if C.key(k) == C.key(p)), None)
    if not d:
        sys.exit(f"not in scan: {p}")
    if args.evidence:
        text = evidence.build(d, dirs, gits, cfg)
    else:
        text = judge_context(d, dirs, gits, cfg)
        if args.system:
            text = discover.JUDGE_SYSTEM + "\n\n=====\n\n" + text
    sys.stdout.reconfigure(encoding="utf-8")
    print(text)
    log(f"[{len(text):,} chars ≈ {len(text) // 4:,} tokens; heuristic says: "
        f"{discover.heuristic(d, dirs, cfg, C.key(p) in {C.key(r) for r in cfg.roots}, gits)}]")


def cmd_evidence(args):
    cfg, con = _ctx(args)
    dirs, gits = _loaded(con)
    n = evidence.build_all(con, cfg, dirs, gits, dump=args.dump, log=log)
    tot = con.execute("SELECT SUM(chars), AVG(chars), MAX(chars) FROM evidence").fetchone()
    log(f"{n} packs; total {tot[0]:,} chars (≈{tot[0] // 4:,} tokens), avg {tot[1]:.0f}, max {tot[2]:,}")


def cmd_classify(args):
    cfg, con = _ctx(args)
    dirs = walk.load(con)
    client = _client(cfg, con, args.backend or cfg.classify_backend)
    st = classify.run(con, cfg, dirs, client, limit=args.limit, only=args.only, workers=args.workers, log=log)
    log(f"{dict(st)}; {client.calls} calls, {client.cache_hits} cached, "
        f"{client.tokens_in:,} tok in / {client.tokens_out:,} out ({client.b.model})")


def cmd_report(args):
    cfg, con = _ctx(args)
    dirs, gits = _loaded(con)
    md, html = report.write(con, cfg, dirs, gits)
    cp = cards.write(con, cfg, dirs, gits)
    log(f"wrote {cp}\n      {md}\n      {html}")


def cmd_override(args):
    cfg, con = _ctx(args)
    with con:
        if args.verdict == "clear":
            con.execute("DELETE FROM overrides WHERE path=?", (C.norm(args.path),))
        else:
            con.execute("INSERT OR REPLACE INTO overrides VALUES (?,?,?,?)",
                        (C.norm(args.path), args.verdict, args.kind, args.note))
    log("ok — re-run `discover` to apply")


def cmd_init(args):
    from importlib.resources import files
    import subprocess
    from pathlib import Path
    ws = Path(args.dir).resolve()
    ws.mkdir(parents=True, exist_ok=True)
    tpl = files("inundated") / "templates"
    for src, dst in (("inundated.toml", "inundated.toml"), ("env.example", ".env"), ("gitignore", ".gitignore")):
        p = ws / dst
        if p.exists():
            log(f"  keep   {p}")
            continue
        text = (tpl / src).read_text(encoding="utf-8")
        if dst == "inundated.toml" and args.root:
            text = text.replace('roots = ["~/code"]', "roots = [" + ", ".join(f'"{r}"' for r in args.root) + "]", 1)
        p.write_text(text, encoding="utf-8")
        log(f"  wrote  {p}")
    if args.git and not (ws / ".git").exists():
        subprocess.run(["git", "init", "-q", str(ws)], check=False)
        log(f"  git init {ws}  (a separate repo for your personal data)")
    log(f"next: edit {ws / 'inundated.toml'} ([scan] roots, [owner]) and put API keys in {ws / '.env'}")


def cmd_ping(args):
    cfg, con = _ctx(args)
    c = _client(cfg, None, args.backend)
    if hasattr(c, "decide"):
        r = c.decide("Hello there, how are you?", {"greeting": {"type": "noul", "instructions": "Is this a greeting?"}})
    else:
        r = c.chat_json('Reply with JSON {"ok": true, "model": "<your model name>"}', "ping")
    log(r, f"tokens in/out {c.tokens_in}/{c.tokens_out}")


def main(argv=None):
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="inundated")
    ap.add_argument("-c", "--config", default=None)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("init", help="create a personal workspace (config, .env, .gitignore)")
    p.add_argument("dir", nargs="?", default="local", help="workspace folder (default ./local, auto-found)")
    p.add_argument("--root", action="append", help="folder to scan (repeatable)")
    p.add_argument("--git", action="store_true", help="make the workspace its own git repo")
    p.set_defaults(f=cmd_init)
    p = sp.add_parser("scan", help="walk roots, collect dir stats + git facts")
    p.add_argument("--no-git", action="store_true")
    p.set_defaults(f=cmd_scan)
    p = sp.add_parser("discover", help="decide project boundaries")
    p.add_argument("--judge", choices=["heuristic", "llm-ambiguous", "llm-all"])
    p.add_argument("--backend")
    p.set_defaults(f=cmd_discover)
    p = sp.add_parser("context", help="print what the judge (or synopsis) model would see for a folder")
    p.add_argument("path")
    p.add_argument("--evidence", action="store_true", help="show the evidence pack instead")
    p.add_argument("--system", action="store_true", help="include the system prompt")
    p.set_defaults(f=cmd_context)
    p = sp.add_parser("evidence", help="build evidence packs for discovered projects")
    p.add_argument("--dump", action="store_true", help="also write out/evidence/*.md")
    p.set_defaults(f=cmd_evidence)
    p = sp.add_parser("classify", help="synopsis + tags + labels per project (needs evidence)")
    p.add_argument("--backend")
    p.add_argument("--limit", type=int)
    p.add_argument("--only", help="substring filter on path")
    p.add_argument("--workers", type=int, default=8)
    p.set_defaults(f=cmd_classify)
    p = sp.add_parser("report", help="write out/report.md + out/report.html")
    p.set_defaults(f=cmd_report)
    p = sp.add_parser("override", help="force a verdict for a folder")
    p.add_argument("path")
    p.add_argument("verdict", choices=sorted(discover.VERDICTS | {"clear"}))
    p.add_argument("--kind")
    p.add_argument("--note")
    p.set_defaults(f=cmd_override)
    p = sp.add_parser("ping", help="test a model backend")
    p.add_argument("--backend")
    p.set_defaults(f=cmd_ping)
    args = ap.parse_args(argv)
    try:
        args.f(args)
    except C.ConfigNotFound as e:
        sys.exit(f"inundated: {e}")


if __name__ == "__main__":
    main()
