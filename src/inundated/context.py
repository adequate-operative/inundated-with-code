"""Builds the text the models see: ascii subtree, metadata header, file heads."""
from __future__ import annotations

import os
import time
from collections import Counter

from . import filetypes as ft
from .config import Config
from .gitinfo import parse_remote
from .walk import Dir, category_mix, ext_summary, human


def _date(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else "?"


def ascii_tree(d: Dir, dirs: dict[str, Dir], cfg: Config, depth: int | None = None,
               max_lines: int | None = None, files_per_dir: int = 10, dirs_per_dir: int = 24) -> str:
    depth = cfg.ctx["tree_depth"] if depth is None else depth
    max_lines = cfg.ctx["tree_max_lines"] if max_lines is None else max_lines
    lines = [f"{d.name}/  ({d.t_files} files, {human(d.t_bytes)}){'  [git]' if d.git else ''}"]

    def rec(node: Dir, prefix: str, lvl: int):
        if len(lines) >= max_lines:
            return
        try:
            files = sorted((e.name for e in os.scandir(node.path) if e.is_file(follow_symlinks=False)
                            and e.name.lower() != ".git"), key=str.lower)
        except OSError:
            files = []
        kids = [dirs[c] for c in node.children if c in dirs]
        entries: list[tuple[str, Dir | None]] = []
        for k in kids[:dirs_per_dir]:
            entries.append(("", k))
        if len(kids) > dirs_per_dir:
            entries.append((f"… +{len(kids) - dirs_per_dir} more dirs", None))
        for p in node.pruned:
            entries.append((f"{p}/  [skipped]", None))
        # breadth first in spirit: deeper levels get fewer file lines so every top-level child shows
        cap = files_per_dir if lvl == 0 else max(2, files_per_dir // (2 * lvl + 1))
        if len(d.children) > 8 and lvl >= 1:
            cap = 2
        shown = files[:cap]
        for f in shown:
            entries.append((f, None))
        if len(files) > len(shown):
            rest = Counter(os.path.splitext(f.lower())[1] or "(none)" for f in files[len(shown):])
            entries.append((f"… +{len(files) - len(shown)} more files ({ext_summary(rest, 5)})", None))
        for i, (label, kid) in enumerate(entries):
            if len(lines) >= max_lines:
                lines.append(prefix + "… (tree truncated)")
                return
            last = i == len(entries) - 1
            branch, cont = ("└── ", "    ") if last else ("├── ", "│   ")
            if kid is not None:
                tags = []
                if kid.git:
                    tags.append("git")
                if kid.strong:
                    tags.append("+".join(kid.strong))
                info = f"  ({kid.t_files} files, {human(kid.t_bytes)}; {ext_summary(kid.t_ext, 4)})"
                lines.append(f"{prefix}{branch}{kid.name}/{info}{'  [' + ', '.join(tags) + ']' if tags else ''}")
                if lvl + 1 < (2 if len(d.children) > 8 else depth):
                    rec(kid, prefix + cont, lvl + 1)
                elif kid.children:
                    lines.append(f"{prefix}{cont}└── … {len(kid.children)} subdirs")
            else:
                lines.append(prefix + branch + label)

    rec(d, "", 0)
    return "\n".join(lines[: max_lines + 1])


def git_block(path: str, gi: dict | None) -> str:
    if not gi:
        return "git: not a repository"
    if not gi.get("ok"):
        return f"git: BROKEN ({gi.get('error', '')[:160]})"
    out = []
    rem = gi.get("remotes") or {}
    if rem:
        for name, url in rem.items():
            p = parse_remote(url)
            out.append(f"git remote {name}: {url}  (host={p['host']}, owner={p['owner']}, repo={p['repo']})")
    else:
        out.append("git remotes: none")
    out.append(f"git: {gi.get('commit_count', 0)} commits, {gi.get('first_commit', '?')[:10] if gi.get('first_commit') else '?'}"
               f" → {gi.get('last_commit', '?')[:10] if gi.get('last_commit') else '?'}, branch={gi.get('branch')},"
               f" uncommitted={gi.get('dirty')}, unpushed={gi.get('unpushed')}")
    if gi.get("authors"):
        out.append("git authors: " + "; ".join(gi["authors"][:4]))
    if gi.get("subjects"):
        out.append("recent commits:\n" + "\n".join("  " + s for s in gi["subjects"][:10]))
    return "\n".join(out)


def candidate_files(d: Dir, cfg: Config, max_depth: int = 4, max_scan: int = 4000) -> list[tuple[float, str, int]]:
    """Score files for head extraction: docs/manifests/entry points first, shallow before deep."""
    skip = cfg.prune_always | cfg.prune
    out: list[tuple[float, str, int]] = []
    stack = [(d.path, 0)]
    scanned = 0
    while stack and scanned < max_scan:
        path, lvl = stack.pop()
        try:
            it = list(os.scandir(path))
        except OSError:
            continue
        for e in it:
            scanned += 1
            low = e.name.lower()
            try:
                if e.is_symlink():
                    continue
                if e.is_dir(follow_symlinks=False):
                    if low not in skip and low not in cfg.exclude_names and lvl + 1 <= max_depth:
                        # nested repos are their own items; don't mine their files here
                        if not os.path.exists(os.path.join(e.path, ".git")):
                            stack.append((e.path, lvl + 1))
                    continue
                size = e.stat(follow_symlinks=False).st_size
            except OSError:
                continue
            if ft.is_secretish(low) or ft.SKIP_HEAD_NAME.search(low) or size == 0 or size > 3_000_000:
                continue
            stem, ext = os.path.splitext(low)
            score = None
            if stem == "readme" or low.startswith("readme"):
                score = 100
            elif low in ft.NAME_MARKERS or ext in ft.EXT_MARKERS and ext in {".uproject", ".csproj", ".vcxproj", ".pde", ".ino", ".uplugin"}:
                score = 85
            elif ext in ft.DOC_EXT or stem in ft.TEXT_NAMES:
                score = 65
            elif ext in ft.SOURCE_EXT:
                score = 50
                if stem in {"main", "index", "app", "__main__", "program", "background", "content",
                            "server", "game", "player", "cli", "lib", "mod", "init", "__init__", "setup"}:
                    score = 72
            elif ext == "" and size < 200_000:
                score = 30  # unknown: sniff decides
            if score is None:
                continue
            score -= lvl * 9
            score += min(size, 40_000) / 4000  # slight preference for substantive files
            out.append((score, e.path, size))
    out.sort(key=lambda t: -t[0])
    return out


def heads(d: Dir, cfg: Config, max_files: int, max_chars: int) -> tuple[str, Counter]:
    lines_n = cfg.ctx["head_lines"]
    parts, used, mimes = [], 0, Counter()
    picked = 0
    for score, path, size in candidate_files(d, cfg):
        if picked >= max_files or used >= max_chars:
            break
        mime, is_text = ft.sniff(path)
        mimes[mime.split(";")[0]] += 1
        if not is_text:
            continue
        h = ft.head(path, lines_n)
        if not h.strip():
            continue
        rel = os.path.relpath(path, d.path).replace("\\", "/")
        block = f"--- {rel} ({human(size)}, {mime.split(';')[0]}) ---\n{h}\n"
        if used + len(block) > max_chars:
            block = block[: max(0, max_chars - used)] + "\n…\n"
        parts.append(block)
        used += len(block)
        picked += 1
    return "\n".join(parts), mimes


def header(d: Dir, dirs: dict[str, Dir], gi: dict | None) -> str:
    mix = category_mix(d.t_ext)
    total = sum(mix.values()) or 1
    mix_s = ", ".join(f"{c} {100 * n / total:.0f}%" for c, n in mix.most_common(6))
    parent = dirs.get(d.parent) if d.parent else None
    sib = [dirs[c].name for c in parent.children][:30] if parent else []
    rel = os.path.relpath(d.path, d.root).replace("\\", "/")
    return "\n".join([
        f"path: {d.path}",
        f"relative to scan root: {rel}   (depth {d.depth})",
        f"parent folder: {parent.name if parent else '-'}; siblings: {', '.join(sib)}{' …' if parent and len(parent.children) > 30 else ''}",
        f"size: {d.t_files} files, {human(d.t_bytes)}; file modified dates {_date(d.oldest)} … {_date(d.t_newest)}",
        f"content mix (by extension): {mix_s}",
        f"top extensions: {ext_summary(d.t_ext, 10)}",
        f"project markers here: {', '.join(d.markers) or 'none'}; skipped heavy dirs: {', '.join(d.pruned) or 'none'}",
        f"descendant folders that look like project roots: {d.t_projecty}",
        git_block(d.path, gi),
    ])


def judge_context(d: Dir, dirs: dict[str, Dir], gits: dict[str, dict], cfg: Config) -> str:
    h, mimes = heads(d, cfg, cfg.ctx["judge_head_files"], cfg.ctx["judge_head_chars"])
    return "\n\n".join([
        "## Folder facts\n" + header(d, dirs, gits.get(d.path)),
        "## Subtree\n" + ascii_tree(d, dirs, cfg),
        "## Nested git repos\n" + (nested_git_summary(d, dirs, gits) or "none"),
        f"## File heads (first {cfg.ctx['head_lines']} lines; sniffed types: {dict(mimes)})\n" + (h or "(no readable text files)"),
    ])


def nested_git_summary(d: Dir, dirs: dict[str, Dir], gits: dict[str, dict], limit: int = 25) -> str:
    out = []
    stack = list(d.children)
    while stack and len(out) < limit:
        p = stack.pop()
        c = dirs.get(p)
        if not c:
            continue
        if c.git:
            gi = gits.get(p) or {}
            urls = ", ".join((gi.get("remotes") or {}).values()) or "no remote"
            out.append(f"- {os.path.relpath(p, d.path)}  → {urls}")
        stack.extend(c.children)
    return "\n".join(out)
