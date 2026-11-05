"""Top-down project-boundary discovery: rules first, a judge model where rules are unsure.

Verdicts per folder:
  project    - one coherent codebase/work item; sub-folders are parts of it
  collection - itself one repo/project, but holds several independent sub-projects
  container  - just an organising folder of separate items
  non_code   - media, downloads, assets, data dumps; nothing to build or run
"""
from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import filetypes as ft
from .config import Config, key
from .context import judge_context
from .gitinfo import ownership, parse_remote
from .llm import Client, LLMError
from .walk import Dir, category_mix

VERDICTS = {"project", "collection", "container", "non_code"}
KEEP_WHOLE_KINDS = {"learning-tutorial", "third-party-download"}
KINDS = [
    "game", "game-prototype", "engine-experiment", "godot-addon", "browser-extension", "userscript",
    "cli-tool", "desktop-app", "web-app", "website", "library", "bot", "embedded-firmware", "fpga",
    "hardware-cad", "scripts", "learning-tutorial", "asset-pipeline", "config-dotfiles", "data",
    "media", "third-party-download", "unknown",
]

MARKER_KIND = {
    "godot": "game-prototype", "godot-addon": "godot-addon", "unity": "game-prototype",
    "unreal": "game-prototype", "unreal-plugin": "library", "browser-ext": "browser-extension",
    "platformio": "embedded-firmware", "arduino": "embedded-firmware", "stm32cube": "embedded-firmware",
    "keil": "embedded-firmware", "atmel-studio": "embedded-firmware", "vivado": "fpga", "quartus": "fpga",
    "kicad": "hardware-cad", "processing": "learning-tutorial",
}

JUDGE_SYSTEM = f"""You inspect folders on a developer's hard drive (a decade of personal projects,
experiments, tutorials and downloaded third-party code) and decide where project boundaries are.

Classify the FOLDER described by the user message as exactly one of:
- "project": a single coherent codebase or work item (even tiny, half-finished or a tutorial).
  Its sub-folders are parts of it (src/, assets/, docs/, a frontend/ and backend/ of one app...).
- "collection": the folder is itself one repo/project, but contains several INDEPENDENT sub-projects
  that should be catalogued separately (e.g. a monorepo of unrelated browser extensions).
- "container": just an organising folder (e.g. "Projects", "_Retired", "Experiments", "foo-master"
  wrapper) holding separate items; it has no real identity of its own.
- "non_code": media, documents, downloads, asset dumps or data with nothing to build or run.

Clues: git roots and manifests (package.json, project.godot, .sln, Cargo.toml, manifest.json...)
usually mark projects; sibling sub-folders each with their own manifests/git usually mean a
container or collection; a folder whose sub-folders share one build/manifest is one project.

Also give "kind", one of: {", ".join(KINDS)}.
Reply with JSON only: {{"verdict": "...", "kind": "...", "confidence": 0.0-1.0, "reason": "<= 25 words"}}"""


@dataclass
class Verdict:
    verdict: str
    confidence: float
    kind: str | None = None
    source: str = "heuristic"
    reasons: list[str] = field(default_factory=list)
    parent_item: str | None = None
    heuristic: str | None = None   # what the rules said, when overridden by the judge


def _hinted(name: str, hints: set[str]) -> bool:
    low = name.lower().strip()
    return low in hints or low.lstrip("_") in hints


# "foo-master", "bar-main", "baz-1.2.3", "qux_v2.0-beta", "portmedia-code-r234", "7z1512-src"
DOWNLOAD_RE = re.compile(
    r"([-_](master|main|develop|release|stable|trunk)$)|([-_ ]v?\d+(\.\d+){1,3}([-_.]?[a-z0-9]+)?$)"
    r"|([-_]r\d{2,}$)|(\d[-_.]?(src|source)$)", re.I)
EXAMPLES_RE = re.compile(r"^_?(examples?|samples?|demos?|tutorials?|exercises?)$", re.I)

# Build ecosystems: a root manifest plus sub-modules of the same ecosystem is ONE project
ECOSYSTEM = {
    "msvs": "vs", "msvc": "vs", "dotnet": "vs", "gradle": "jvm", "java": "jvm",
    "node": "js", "typescript": "js", "deno": "js", "rust": "rust", "go": "go",
    "python": "py", "python-reqs": "py", "cmake": "c", "make": "c", "qmake": "c", "cpp": "c",
    "meson": "c", "godot": "godot", "godot-addon": "godot", "godot-ext": "godot",
    "unity": "unity", "unreal": "unreal", "unreal-plugin": "unreal",
    "platformio": "embedded", "arduino": "embedded", "stm32cube": "embedded", "keil": "embedded",
}


def _kind_from(d: Dir) -> str | None:
    for m in d.markers:
        if m in MARKER_KIND:
            return MARKER_KIND[m]
    return None


def _eco(d: Dir) -> set[str]:
    return {ECOSYSTEM[m] for m in d.markers if m in ECOSYSTEM}


def _projecty_desc(d: Dir, dirs: dict[str, Dir], limit: int = 60) -> list[Dir]:
    """Nearest project-like descendants (does not look below one)."""
    out, stack = [], list(d.children)
    while stack and len(out) < limit:
        c = dirs.get(stack.pop())
        if not c:
            continue
        if c.projecty:
            out.append(c)
            continue
        stack.extend(c.children)
    return out


def _same_build(d: Dir, subs: list[Dir]) -> bool:
    eco = _eco(d)
    if not eco or not subs or any(s.git for s in subs):
        return False
    return all(_eco(s) and _eco(s) <= eco | {"c"} for s in subs)


def _code(d: Dir) -> int:
    return category_mix(d.t_ext).get("code", 0)


def heuristic(d: Dir, dirs: dict[str, Dir], cfg: Config, is_root: bool,
              gits: dict[str, dict] | None = None, sealed: bool = False) -> Verdict:
    if is_root:
        return Verdict("container", 1.0, source="root", reasons=["scan root"])
    kids = [dirs[c] for c in d.children if c in dirs]
    projecty_kids = [k for k in kids if k.projecty]
    mix = category_mix(d.t_ext)
    code = mix.get("code", 0)
    own_code = sum(n for e, n in d.ext.items() if e in ft.SOURCE_EXT)
    strong = d.strong
    kind = _kind_from(d)

    if d.t_files == 0:
        return Verdict("non_code", 0.95, reasons=["empty"])
    if sealed:
        if code == 0 and mix.get("config", 0) < 3 and not d.t_projecty and not strong:
            return Verdict("non_code", 0.85, "third-party-download", reasons=["inside sealed folder; no source"])
        return Verdict("project", 0.9, kind or "third-party-download", reasons=["inside sealed folder: kept whole"])
    if _hinted(d.name, cfg.sealed_hints) and not d.git and not strong:
        return Verdict("container", 0.95, reasons=[f"sealed container '{d.name}' (children kept whole)"])
    if _hinted(d.name, cfg.container_hints) and not d.git and not strong:
        return Verdict("container", 0.9, reasons=[f"container-ish name '{d.name}'"])
    gi = (gits or {}).get(d.path) or {}
    if d.git and gi.get("remotes"):
        if ownership(gi["remotes"], cfg.owner_handles, cfg.self_hosts) == "other":
            owners = sorted({f"{p['host']}/{p['owner']}" for p in map(parse_remote, gi["remotes"].values())})
            return Verdict("project", 0.92, kind, reasons=[f"third-party repo ({', '.join(owners)}) kept whole"])
    if not d.git and code and DOWNLOAD_RE.search(d.name):
        return Verdict("project", 0.85, kind or "third-party-download", reasons=[f"download-style name '{d.name}' kept whole"])

    if d.git:
        loose_kids = [k for k in projecty_kids if not k.git]
        if len(projecty_kids) >= 3 and len(projecty_kids) >= 0.5 * len(kids) and not _same_build(d, loose_kids):
            return Verdict("collection", 0.6, kind, reasons=[f"git root with {len(projecty_kids)} project-like children"])
        return Verdict("project", 0.95 if (strong or code) else 0.85, kind,
                       reasons=["git root"] + ([f"markers: {','.join(strong)}"] if strong else []))

    if strong:
        subs = _projecty_desc(d, dirs)
        if not subs:
            return Verdict("project", 0.92, kind, reasons=[f"markers: {','.join(strong)}"])
        if _same_build(d, subs):
            return Verdict("project", 0.9, kind, reasons=[f"{','.join(strong)} build with {len(subs)} sub-modules"])
        if len(projecty_kids) >= 3 or EXAMPLES_RE.match(d.name):
            return Verdict("collection", 0.6, kind, reasons=[f"markers {strong} + {len(subs)} independent-looking sub-projects"])
        return Verdict("project", 0.8, kind, reasons=[f"markers: {','.join(strong)}; {len(subs)} nested"])

    if code == 0 and mix.get("config", 0) < 3 and not d.t_projecty:
        top = ", ".join(c for c, _ in mix.most_common(3))
        return Verdict("non_code", 0.88, "media", reasons=[f"no source files ({top})"])
    if len(kids) == 1 and d.n_files <= 3:
        return Verdict("container", 0.88, reasons=["single-child wrapper folder"])

    if d.t_projecty:
        subs = _projecty_desc(d, dirs)
        if own_code <= 2:
            if len(subs) >= 2:
                return Verdict("container", 0.88, reasons=[f"{len(subs)} project-like folders inside, little loose code"])
            if subs and subs[0].t_files >= 0.6 * d.t_files:
                return Verdict("container", 0.85, reasons=[f"wrapper around '{subs[0].name}'"])
            return Verdict("project", 0.6, kind, reasons=[f"code plus one nested project '{subs[0].name if subs else '?'}'"])
        if len(subs) == 1 and subs[0].t_files < 0.5 * d.t_files:
            return Verdict("project", 0.82, kind, reasons=[f"own code + small nested '{subs[0].name}'"])
        if len(subs) >= 2:
            return Verdict("container", 0.6, reasons=[f"own code and {len(subs)} project-like folders"])
        return Verdict("project", 0.6, kind, reasons=["own code + one nested project"])

    # nothing project-like anywhere below: the only question is "one thing, or a bag of things?"
    code_kids = [k for k in kids if _code(k)]
    if d.name.lower().startswith(tuple(cfg.organiser_prefixes)) and len(code_kids) >= 2 and own_code <= 2:
        return Verdict("container", 0.82, reasons=[f"organiser-style name, {len(code_kids)} code folders"])
    if EXAMPLES_RE.match(d.name) and len(code_kids) >= 3:
        return Verdict("container", 0.75, "learning-tutorial", reasons=[f"examples folder with {len(code_kids)} code folders"])
    weak = [m for m in d.markers if m not in ft.STRONG_MARKERS]
    if len(code_kids) <= 3 or own_code >= 0.3 * code:
        why = f"weak markers: {','.join(weak)}" if weak else f"{code} source files, no sub-projects"
        return Verdict("project", 0.86, kind or ("scripts" if not weak else None), reasons=[why])
    return Verdict("project", 0.6, kind, reasons=[f"{len(code_kids)} code sub-folders without manifests (one thing or a bag of scripts?)"])


VERDICT_CRITERIA = {
    "project": "One coherent codebase or work item (even tiny, half-finished or a tutorial); its "
               "sub-folders are parts of it (src/, assets/, a frontend and backend of one app, build modules).",
    "container": "Just an organising folder (like 'Projects', '_Retired', a 'foo-master' wrapper) holding "
                 "separate unrelated items; no identity of its own.",
    "collection": "Itself one repo, but holding several independent sub-projects that should be "
                  "catalogued separately (e.g. a repo of unrelated browser extensions or examples).",
    "non_code": "Media, documents, downloads, asset dumps or data; nothing to build or run.",
}
KIND_CRITERIA = {
    "game": "a playable, fairly complete game", "game-prototype": "an unfinished game or game mechanic test",
    "engine-experiment": "rendering/shader/engine/tech experiment", "godot-addon": "Godot plugin or addon",
    "browser-extension": "Chrome/Firefox extension", "userscript": "Tampermonkey/Greasemonkey user script",
    "cli-tool": "command-line utility", "desktop-app": "GUI desktop application", "web-app": "web application with a backend or rich frontend",
    "website": "static or CMS website", "library": "reusable library/SDK/module", "bot": "chat/Discord/Twitch/trading/automation bot",
    "embedded-firmware": "microcontroller firmware (AVR/STM32/ESP/Arduino)", "fpga": "FPGA/HDL design",
    "hardware-cad": "PCB/CAD/mechanical design files", "scripts": "loose scripts or one-off automation",
    "learning-tutorial": "course work, tutorial follow-along, coding exercises", "asset-pipeline": "asset conversion/import/build tooling",
    "config-dotfiles": "configuration, dotfiles, docker/VM setup", "data": "datasets or scraped data",
    "media": "images/audio/video", "third-party-download": "someone else's software, downloaded or cloned unchanged",
    "unknown": "cannot tell",
}


def _clef_judge(client, d: Dir, dirs, gits, cfg) -> Verdict | None:
    state = "Describe-and-decide task: a folder on a developer's hard drive (a decade of personal " \
            "projects, experiments, tutorials and downloaded third-party code).\n\n" + judge_context(d, dirs, gits, cfg)
    questions = {
        "verdict": {"type": "choice", "instructions": "How should this FOLDER be catalogued?", "criteria": VERDICT_CRITERIA},
        "kind": {"type": "choice", "instructions": "What kind of thing is this folder (or mostly holds)?", "criteria": KIND_CRITERIA},
        "third_party": {"type": "noul", "instructions": "Was most of the code in this folder written by someone other than "
                        "the drive owner (downloaded, cloned, an SDK or sample)?"},
    }
    try:
        a = client.decide(state, questions)
    except LLMError as e:
        return Verdict("error", 0, reasons=[f"judge failed: {e}"[:300]], source="llm-error")
    v = a.get("verdict", {})
    if v.get("choice") not in VERDICTS:
        return None
    probs = sorted((v.get("probabilities") or {}).items(), key=lambda x: -x[1])
    k = a.get("kind", {})
    tp = a.get("third_party", {}).get("noul")
    why = ", ".join(f"{c} {p:.2f}" for c, p in probs[:3]) + f"; kind {k.get('choice')} {k.get('confidence', 0):.2f}"
    if tp is not None:
        why += f"; P(third-party)={tp:.2f}"
    return Verdict(v["choice"], float(v.get("confidence", 0.0)), k.get("choice") if k.get("choice") in KINDS else None,
                   source="clef", reasons=[why])


def _llm_judge(client: Client, d: Dir, dirs, gits, cfg) -> Verdict | None:
    if hasattr(client, "decide"):
        return _clef_judge(client, d, dirs, gits, cfg)
    ctx = judge_context(d, dirs, gits, cfg)
    try:
        r = client.chat_json(JUDGE_SYSTEM, ctx)
    except LLMError as e:
        return Verdict("error", 0, reasons=[f"judge failed: {e}"[:300]], source="llm-error")
    v = str(r.get("verdict", "")).lower().strip()
    if v not in VERDICTS:
        return None
    kind = str(r.get("kind") or "") or None
    try:
        conf = float(r.get("confidence", 0.6))
    except (TypeError, ValueError):
        conf = 0.6
    return Verdict(v, conf, kind if kind in KINDS else None, source="llm",
                   reasons=[str(r.get("reason", ""))[:300]])


def _overrides(con) -> dict[str, dict]:
    return {key(r["path"]): dict(r) for r in con.execute("SELECT * FROM overrides")}


def discover(cfg: Config, con, dirs: dict[str, Dir], gits: dict[str, dict],
             judge: str | None = None, client: Client | None = None, log=print,
             roots: list[str] | None = None) -> dict[str, Verdict]:
    judge = judge or cfg.judge
    if judge != "heuristic" and client is None:
        raise ValueError("judge mode needs a client")
    ovr = _overrides(con)
    roots = roots or cfg.roots
    root_keys = {key(r) for r in roots}
    results: dict[str, Verdict] = {}
    level = [r for r in roots if r in dirs]
    container_of: dict[str, str | None] = {r: None for r in level}
    sealed_kids: set[str] = set()
    pool = ThreadPoolExecutor(cfg.judge_workers)
    lvl_no = 0
    while level:
        verdicts: dict[str, Verdict] = {}
        ask: list[str] = []
        for p in level:
            d = dirs[p]
            o = ovr.get(key(p))
            if o:
                verdicts[p] = Verdict(o["verdict"], 1.0, o.get("kind"), source="override",
                                      reasons=[o.get("note") or "manual override"])
                continue
            h = heuristic(d, dirs, cfg, key(p) in root_keys, gits, sealed=p in sealed_kids)
            verdicts[p] = h
            if h.source != "root" and (judge == "llm-all" or (judge == "llm-ambiguous" and h.confidence < cfg.confident)):
                ask.append(p)
        if ask:
            log(f"  level {lvl_no}: {len(level)} folders, asking judge about {len(ask)}")
            futs = {p: pool.submit(_llm_judge, client, dirs[p], dirs, gits, cfg) for p in ask}
            for p, f in futs.items():
                v = f.result()
                if v is None:
                    continue
                h = verdicts[p]
                if v.verdict == "error":
                    h.reasons.append(v.reasons[0])
                    continue
                v.heuristic = f"{h.verdict}@{h.confidence:.2f}: {'; '.join(h.reasons)}"
                if not v.kind:
                    v.kind = h.kind
                if v.verdict in ("collection", "container") and v.kind in KEEP_WHOLE_KINDS and \
                        (v.verdict == "collection" or h.verdict == "project"):
                    # a bag of samples/tutorials is one catalogue entry, not 40
                    v.reasons.append(f"judge said {v.verdict}, kept whole ({v.kind})")
                    v.verdict = "project"
                verdicts[p] = v
        nxt: list[str] = []
        for p in level:
            v = verdicts[p]
            v.parent_item = container_of.get(p)
            results[p] = v
            d = dirs[p]
            if v.verdict in ("container", "collection"):
                seal = _hinted(d.name, cfg.sealed_hints)
                for c in d.children:
                    container_of[c] = p
                    nxt.append(c)
                    if seal:
                        sealed_kids.add(c)
            elif v.verdict == "project":
                _nested(d, dirs, results, p)
        level = nxt
        lvl_no += 1
    pool.shutdown()
    save(con, results, roots)
    return results


def _nested(d: Dir, dirs: dict[str, Dir], results: dict[str, Verdict], owner: str) -> None:
    """Record git repos living inside an accepted project (submodules, vendored clones)."""
    stack = [(c, owner) for c in d.children]
    while stack:
        p, parent = stack.pop()
        c = dirs.get(p)
        if not c:
            continue
        if c.git:
            sub = "submodule/worktree" if c.git == "file" else "nested repo"
            results[p] = Verdict("project", 0.9, _kind_from(c), source="nested", reasons=[sub], parent_item=parent)
            parent = p  # deeper repos belong to the nearest enclosing repo
        stack.extend((gc, parent) for gc in c.children)


def save(con, results: dict[str, Verdict], roots: list[str]) -> None:
    """Replace verdicts under these roots only; other roots' items stay."""
    from .roots import is_under_any
    with con:
        old = [p for (p,) in con.execute("SELECT path FROM items") if is_under_any(p, roots)]
        con.executemany("DELETE FROM items WHERE path=?", [(p,) for p in old])
        con.executemany(
            "INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?)",
            [(p, v.verdict, v.kind, v.confidence, v.source, " | ".join(v.reasons), v.parent_item,
              v.parent_item, v.heuristic) for p, v in results.items()],
        )
