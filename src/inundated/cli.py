from __future__ import annotations

import argparse
import sys
import time

from . import config as C
from . import cards, classify, db, discover, evidence, gitinfo, report, walk
from . import roots as R
from .context import judge_context
from .llm import make_client


def log(*a, **k):
    k.setdefault("file", sys.stderr)
    print(*a, **k, flush=True)


def _ctx(args):
    cfg = C.load(args.config)
    con = db.connect(cfg.db_path)
    return cfg, con


def _scan_roots(args, cfg, con) -> list[str]:
    """Roots for a scan: --root flags if given, else [scan] roots. Validated (exist, no nesting, no
    overlap with other roots already in the database; re-scanning the same root is fine)."""
    return R.validate(getattr(args, "root", None) or cfg.roots, con)


def _db_roots(args, con) -> list[str]:
    """Roots to (re)judge: --root flags if given, else every root in the database."""
    have = R.stored(con)
    if getattr(args, "root", None):
        want = [C.norm(r) for r in args.root]
        missing = [r for r in want if C.key(r) not in {C.key(h) for h in have}]
        if missing:
            raise R.RootError(f"not scanned yet: {', '.join(missing)}  (run: inundated scan --root ...)")
        return [h for h in have if C.key(h) in {C.key(w) for w in want}]
    if not have:
        raise R.RootError("nothing scanned yet: run  inundated scan  (or  inundated run)")
    return have


def cmd_scan(args):
    cfg, con = _ctx(args)
    roots = _scan_roots(args, cfg, con)
    t = time.time()
    log(f"walking {', '.join(roots)} …")
    dirs = walk.walk(cfg, roots, log=log)
    walk.save(con, dirs, roots)
    repos = [d.path for d in dirs.values() if d.git]
    for r in roots:
        mine = [d for d in dirs.values() if d.root == r]
        R.record(con, r, len(mine), sum(d.n_files for d in mine), sum(1 for d in mine if d.git))
    log(f"{len(dirs):,} dirs, {sum(d.n_files for d in dirs.values()):,} files, {len(repos)} git roots "
        f"({time.time() - t:.0f}s)")
    if not args.no_git:
        log("collecting git facts …")
        gitinfo.collect_all(con, repos, roots, cfg.git_workers, log=log)
    others = [r for r in R.stored(con) if r not in roots]
    log(f"done in {time.time() - t:.0f}s → {cfg.db_path}"
        + (f"\n(also in this database from earlier runs: {', '.join(others)})" if others else ""))
    return roots


def _loaded(con):
    return walk.load(con), gitinfo.load(con)


def _client(cfg, con, name=None):
    name = name or cfg.judge_backend
    if name not in cfg.backends:
        raise C.ConfigNotFound(f"no [backends.{name}] in {cfg.home / 'inundated.toml'}")
    b = cfg.backends[name]
    if b.api_key_env and not b.api_key:
        raise C.ConfigNotFound(
            f"missing API key for backend '{name}' ({b.model} at {b.base_url}).\n"
            f"  put  {b.api_key_env}=<your key>  in {cfg.home / '.env'}\n"
            f"  or run without models:  inundated run --no-llm")
    return make_client(b, con)


def _preflight(cfg, names) -> None:
    """One tiny call per backend so a bad key/model fails in seconds, not 1,000 calls later."""
    for name in dict.fromkeys(names):
        c = _client(cfg, None, name)
        try:
            if hasattr(c, "decide"):
                c.decide("hello", {"greeting": {"type": "noul", "instructions": "Is this a greeting?"}})
            else:
                c.chat_json('Reply with JSON {"ok": true}', "ping")
        except Exception as e:  # noqa: BLE001 - any failure means "don't start the run"
            raise C.ConfigNotFound(f"backend '{name}' ({c.b.model} at {c.b.base_url}) failed a test call: {e}")
        log(f"  ok  {name}: {c.b.model}")


def cmd_discover(args):
    cfg, con = _ctx(args)
    dirs, gits = _loaded(con)
    judge = args.judge or cfg.judge
    roots = _db_roots(args, con)
    client = None if judge == "heuristic" else _client(cfg, con, args.backend)
    res = discover.discover(cfg, con, dirs, gits, judge=judge, client=client, log=log, roots=roots)
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
        f"{discover.heuristic(d, dirs, cfg, C.key(p) in {C.key(r) for r in R.stored(con)}, gits)}]")


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
    if getattr(args, "open", False):
        import webbrowser
        from pathlib import Path
        webbrowser.open(Path(cp).as_uri())


def cmd_run(args):
    """scan -> discover -> evidence -> classify -> report, in one go."""
    from types import SimpleNamespace as NS
    cfg, con = _ctx(args)
    t = time.time()
    # check roots before anything costs time or money
    roots = _db_roots(args, con) if args.skip_scan else _scan_roots(args, cfg, con)
    llm = not args.no_llm
    if llm:
        log("checking model backends …")
        _preflight(cfg, [cfg.judge_backend, cfg.classify_backend])
    c = args.config
    steps = [
        ("scan", lambda: cmd_scan(NS(config=c, no_git=False, root=roots)), not args.skip_scan),
        ("discover", lambda: cmd_discover(NS(config=c, judge=None if llm else "heuristic", backend=None,
                                             root=roots)), True),
        ("evidence", lambda: cmd_evidence(NS(config=c, dump=False)), llm),
        ("classify", lambda: cmd_classify(NS(config=c, backend=None, limit=args.limit, only=None,
                                             workers=args.workers)), llm),
        ("report", lambda: cmd_report(NS(config=c, open=args.open)), True),
    ]
    todo = [(n, f) for n, f, on in steps if on]
    for i, (name, fn) in enumerate(todo, 1):
        log(f"\n[{i}/{len(todo)}] {name}")
        fn()
    log(f"\nall done in {time.time() - t:.0f}s. Open {cfg.out_dir / 'cards.html'}")


def cmd_roots(args):
    cfg, con = _ctx(args)
    rows = {r["root"]: r for r in con.execute("SELECT * FROM roots")}
    have = R.stored(con)
    if not have:
        log("no roots scanned yet")
    for r in have:
        n = sum(1 for (p,) in con.execute("SELECT path FROM items WHERE verdict IN ('project','collection')")
                if R.is_under_any(p, [r]))
        info = rows.get(r)
        extra = f"scanned {info['scanned']}, {info['dirs']:,} dirs, {info['repos']} repos" if info else "scanned (before roots were tracked)"
        print(f"{r}\n    {n} projects; {extra}")
    cfg_only = [x for x in cfg.roots if C.key(x) not in {C.key(h) for h in have}]
    if cfg_only:
        print("in config but not scanned yet: " + ", ".join(cfg_only))


def cmd_forget(args):
    cfg, con = _ctx(args)
    root = C.norm(args.root)
    if C.key(root) not in {C.key(h) for h in R.stored(con)}:
        raise R.RootError(f"not a root in this database: {root}  (see: inundated roots)")
    root = next(h for h in R.stored(con) if C.key(h) == C.key(root))
    n = R.forget(con, root)
    log(f"removed {root} ({n:,} dirs) and its verdicts, git facts and classifications. "
        f"Cached model answers are kept, so re-adding it later is cheap. Run `inundated report` to refresh.")


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
        q = lambda xs: ", ".join('"' + x.replace("\\", "/") + '"' for x in xs)
        if dst == "inundated.toml" and args.root:
            text = text.replace('roots = ["~/code"]', f"roots = [{q(args.root)}]", 1)
        if dst == "inundated.toml" and args.handle:
            text = text.replace("handles = []", f"handles = [{q(args.handle)}]", 1)
            text = text.replace("names = []", f"names = [{q(args.handle)}]", 1)
        p.write_text(text, encoding="utf-8")
        log(f"  wrote  {p}")
    if args.git and not (ws / ".git").exists():
        subprocess.run(["git", "init", "-q", str(ws)], check=False)
        log(f"  git init {ws}  (a separate repo for your personal data)")
    log(f"\nnext:\n  1. put your API key in {ws / '.env'}\n"
        f"  2. check [scan] roots and [owner] handles in {ws / 'inundated.toml'}\n"
        f"  3. inundated run      (or: inundated run --no-llm  for a free, rules-only pass)")


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
    ap = argparse.ArgumentParser(
        prog="inundated",
        description="Catalogue years of code projects. New here? `inundated init --root PATH`, "
                    "put your API key in local/.env, then `inundated run`.")
    ap.add_argument("-c", "--config", default=None, help="config file or workspace folder (default: ./local)")
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("run", help="do everything: scan, discover, evidence, classify, report")
    p.add_argument("--no-llm", action="store_true", help="rules only: no API calls, no synopses/tags")
    p.add_argument("--root", action="append", help="folder to scan; repeatable; overrides [scan] roots. Must not be inside another root")
    p.add_argument("--skip-scan", action="store_true", help="reuse the last filesystem scan")
    p.add_argument("--limit", type=int, help="classify at most N projects (cheap trial)")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--open", action="store_true", help="open cards.html when done")
    p.set_defaults(f=cmd_run)
    p = sp.add_parser("init", help="create a personal workspace (config, .env, .gitignore)")
    p.add_argument("dir", nargs="?", default="local", help="workspace folder (default ./local, auto-found)")
    p.add_argument("--root", action="append", help="folder to scan (repeatable)")
    p.add_argument("--handle", action="append", help="your username on GitHub/Bitbucket/etc. (repeatable)")
    p.add_argument("--git", action="store_true", help="make the workspace its own git repo")
    p.set_defaults(f=cmd_init)
    p = sp.add_parser("scan", help="walk roots, collect dir stats + git facts")
    p.add_argument("--no-git", action="store_true")
    p.add_argument("--root", action="append", help="folder to scan; repeatable; overrides [scan] roots. Must not be inside another root")
    p.set_defaults(f=cmd_scan)
    p = sp.add_parser("discover", help="decide project boundaries")
    p.add_argument("--judge", choices=["heuristic", "llm-ambiguous", "llm-all"])
    p.add_argument("--root", action="append", help="only re-judge this scanned root (default: all roots in the database)")
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
    p = sp.add_parser("report", help="write out/cards.html, out/report.html, out/report.md")
    p.add_argument("--open", action="store_true", help="open cards.html in your browser")
    p.set_defaults(f=cmd_report)
    p = sp.add_parser("roots", help="list roots in the database")
    p.set_defaults(f=cmd_roots)
    p = sp.add_parser("forget", help="remove a root and everything found under it from the database")
    p.add_argument("root")
    p.set_defaults(f=cmd_forget)
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
