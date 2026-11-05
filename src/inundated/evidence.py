"""Evidence packs: the per-project text that the synopsis/classification model will read."""
from __future__ import annotations

import hashlib
import os
import re

from .config import Config
from .context import ascii_tree, header, heads, nested_git_summary
from .walk import Dir


def build(d: Dir, dirs: dict[str, Dir], gits: dict[str, dict], cfg: Config) -> str:
    h, mimes = heads(d, cfg, cfg.ctx["evidence_head_files"], cfg.ctx["evidence_head_chars"])
    nested = nested_git_summary(d, dirs, gits)
    return "\n\n".join(filter(None, [
        "## Facts\n" + header(d, dirs, gits.get(d.path)),
        "## Tree\n" + ascii_tree(d, dirs, cfg, depth=3, max_lines=cfg.ctx["tree_max_lines"]),
        ("## Nested repos\n" + nested) if nested else "",
        f"## File heads (sniffed: {dict(mimes)})\n" + (h or "(no readable text files)"),
    ]))


def build_all(con, cfg: Config, dirs, gits, dump: bool = False, log=print) -> int:
    rows = con.execute("SELECT path FROM items WHERE verdict IN ('project','collection')").fetchall()
    outdir = cfg.out_dir / "evidence"
    if dump:
        outdir.mkdir(parents=True, exist_ok=True)
    n = 0
    for i, r in enumerate(rows, 1):
        d = dirs.get(r["path"])
        if not d:
            continue
        text = build(d, dirs, gits, cfg)
        hsh = hashlib.sha256(text.encode()).hexdigest()[:16]
        with con:
            con.execute("INSERT OR REPLACE INTO evidence VALUES (?,?,?,?)", (d.path, hsh, text, len(text)))
        if dump:
            slug = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.relpath(d.path, d.root))[:150]
            (outdir / f"{slug}.md").write_text(text, encoding="utf-8")
        n += 1
        if i % 50 == 0:
            log(f"  … evidence {i}/{len(rows)}")
    return n
