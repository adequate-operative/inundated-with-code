"""Phase 2: per-project synopsis + tags + labels from the evidence pack (one cheap call each)."""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from .discover import KINDS
from .llm import LLMError, _lock
from .walk import Dir

# Controlled vocabulary. The model must pick from here (it may suggest up to 2 extras).
TAGS = {
    # area
    "game-dev": "games, game mechanics, game engines", "graphics": "rendering, shaders, 2D/3D graphics, procedural generation",
    "system-programming": "low-level, OS, memory, drivers, compilers, native code", "win32": "Windows API, Win32 GUI, Windows-specific tooling",
    "reverse-engineering": "disassembly, memory hacking, cheat engines, protocol/file-format RE", "modding": "mods for existing games/apps",
    "embedded": "microcontrollers, firmware, Arduino/STM32/ESP/AVR/RISC-V", "electronics": "PCB, keyboards, circuits, hardware hacking",
    "fpga": "HDL, FPGA designs", "devops": "docker, CI, deployment, VMs, infrastructure, build systems",
    "self-hosting": "home-lab, NAS, media servers, router firmware", "networking": "sockets, packets, protocols, proxies, VPN",
    "web-frontend": "browser UI, HTML/CSS/JS frontends", "web-backend": "servers, APIs, databases",
    "website": "a site for someone/something", "browser-ext": "browser extensions", "userscript": "Tampermonkey/Greasemonkey scripts",
    "scraping": "web scraping, crawling, data harvesting", "bot": "chat/Discord/Twitch/IRC/automation bots",
    "automation": "macros, workflow/desktop automation, scripting chores", "desktop-util": "small desktop utility or GUI tool",
    "cli-tool": "command-line utility", "library": "reusable library/package", "music-production": "DAW, MIDI, synths, audio plugins",
    "audio": "audio processing/playback/visualisation", "video": "video download/processing/streaming", "image": "image processing/conversion",
    "3d-assets": "Blender, meshes, asset pipelines, asset ripping", "ai-ml": "machine learning, neural nets, LLM tooling",
    "data-viz": "charts, visualisation, notebooks", "crypto": "cryptocurrency, mining, blockchain", "finance": "trading, prices, money tracking",
    "security": "CTFs, pentesting, exploits, password tools", "yarr": "piracy-adjacent: downloaders, torrent/seedbox, paywall/DRM bypass, cracks",
    "emulation-retro": "emulators, ROM hacking, retro platforms", "mobile": "Android/iOS apps", "productivity": "notes, tabs, time tracking, personal organisation",
    "social-media": "Reddit/Instagram/Twitter/YouTube/Twitch tooling", "math": "maths, geometry, algorithms visualised",
    # nature / status
    "toy": "a tiny fun or silly thing", "experiment": "trying out an idea, API or library", "prototype": "early version of something meant to grow",
    "learning": "tutorial follow-along, course work, exercises, learning a language", "puzzles": "Advent of Code, code golf, kata, challenges",
    "polished": "fairly complete and usable", "third-party": "mostly someone else's code (download, clone, sample, SDK)",
    "fork": "someone else's project with own changes", "abandoned-wip": "clearly unfinished and left",
}
MATURITY = ["stub", "sketch", "half-finished", "usable", "complete"]
AUTHORSHIP = ["mine", "third-party", "modified-fork", "unclear"]
KEEP = ["revive", "keep", "archive", "delete-candidate"]

LANG_EXT = {
    ".py": "python", ".go": "go", ".rs": "rust", ".js": "javascript", ".mjs": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".c": "c", ".h": "c", ".cpp": "c++", ".cc": "c++", ".hpp": "c++",
    ".cs": "csharp", ".gd": "gdscript", ".lua": "lua", ".java": "java", ".kt": "kotlin", ".php": "php",
    ".rb": "ruby", ".ps1": "powershell", ".sh": "shell", ".bat": "batch", ".ahk": "autohotkey", ".elm": "elm",
    ".v": "verilog", ".vhd": "vhdl", ".sv": "verilog", ".ino": "arduino", ".pde": "processing", ".dart": "dart",
    ".swift": "swift", ".zig": "zig", ".hs": "haskell", ".ex": "elixir", ".gleam": "gleam", ".asm": "asm",
    ".glsl": "glsl", ".gdshader": "glsl", ".hlsl": "hlsl", ".ipynb": "jupyter", ".html": "html",
}

SYSTEM = f"""You catalogue projects found on one developer's hard drive: a decade of personal tools,
experiments, games, tutorials and downloaded third-party code. Given an evidence pack (facts, file
tree, git info, first lines of files), describe the project for its owner, who has forgotten it.

Reply with JSON only:
{{
 "name": "short human name (better than a cryptic folder name)",
 "synopsis": "2-3 plain sentences: what it is, what it does/was for, how far it got",
 "tags": ["3-6 tags chosen ONLY from the vocabulary below, most specific first"],
 "new_tags": ["0-2 extra lowercase-hyphenated tags only if the vocabulary truly lacks one"],
 "kind": one of {json.dumps(KINDS)},
 "maturity": one of {json.dumps(MATURITY)},
 "authorship": one of {json.dumps(AUTHORSHIP)},
 "keep": one of {json.dumps(KEEP)},
 "notable": "one short phrase worth remembering, or empty"
}}

Tag vocabulary:
""" + "\n".join(f"- {t}: {d}" for t, d in TAGS.items()) + """

Hints: git remotes owned by someone else, -master/-main folder names, vendored SDKs and samples mean
third-party. The owner's handle is in the facts when remotes are theirs. Be concrete and brief."""


def lang_tags(d: Dir, k: int = 2) -> list[str]:
    c: Counter = Counter()
    for e, n in d.t_ext.items():
        if e in LANG_EXT:
            c[LANG_EXT[e]] += n
    tot = sum(c.values()) or 1
    return [f"lang:{l}" for l, n in c.most_common(k) if n / tot >= 0.15]


def _clean_tags(xs) -> list[str]:
    out = []
    for t in xs or []:
        t = re.sub(r"[^a-z0-9+-]+", "-", str(t).lower()).strip("-")
        if t and t not in out:
            out.append(t)
    return out


def classify_one(client, path: str, evidence: str, verdict: str, kind_hint: str | None) -> dict:
    user = (f"Catalogue entry type: {verdict}" + (f" (rules guessed kind: {kind_hint})" if kind_hint else "")
            + "\n\n" + evidence[:20000])
    r = client.chat_json(SYSTEM, user)
    tags = [t for t in _clean_tags(r.get("tags")) if t in TAGS]
    extra = [t for t in _clean_tags(r.get("new_tags")) if t not in TAGS][:2]
    pick = lambda v, opts, dflt: v if v in opts else dflt
    return {
        "name": str(r.get("name") or os.path.basename(path))[:80],
        "synopsis": str(r.get("synopsis") or "")[:700],
        "tags": tags, "new_tags": extra,
        "kind": pick(r.get("kind"), KINDS, kind_hint or "unknown"),
        "maturity": pick(r.get("maturity"), MATURITY, "sketch"),
        "authorship": pick(r.get("authorship"), AUTHORSHIP, "unclear"),
        "keep": pick(r.get("keep"), KEEP, "keep"),
        "notable": str(r.get("notable") or "")[:200],
    }


KIND_FALLBACK_TAG = {
    "game": "game-dev", "game-prototype": "game-dev", "engine-experiment": "graphics", "godot-addon": "game-dev",
    "browser-extension": "browser-ext", "userscript": "userscript", "cli-tool": "cli-tool", "desktop-app": "desktop-util",
    "web-app": "web-backend", "website": "website", "library": "library", "bot": "bot", "embedded-firmware": "embedded",
    "fpga": "fpga", "hardware-cad": "electronics", "scripts": "automation", "learning-tutorial": "learning",
    "asset-pipeline": "3d-assets", "config-dotfiles": "devops", "data": "data-viz", "media": "image",
    "third-party-download": "third-party",
}


def run(con, cfg, dirs: dict[str, Dir], client, limit: int | None = None, only: str | None = None,
        workers: int = 8, log=print) -> Counter:
    con.execute("CREATE TABLE IF NOT EXISTS classes (path TEXT PRIMARY KEY, model TEXT, data TEXT, error TEXT)")
    q = ("SELECT e.path, e.text, i.verdict, i.kind FROM evidence e JOIN items i USING(path) "
         "WHERE i.verdict IN ('project','collection')")
    rows = con.execute(q).fetchall()
    if only:
        rows = [r for r in rows if only.lower() in r["path"].lower()]
    if limit:
        rows = rows[:limit]
    stats: Counter = Counter()

    def work(r):
        try:
            return r["path"], classify_one(client, r["path"], r["text"], r["verdict"], r["kind"]), None
        except (LLMError, ValueError, TypeError) as e:
            return r["path"], None, str(e)[:300]

    with ThreadPoolExecutor(workers) as ex:
        for i, (path, data, err) in enumerate(ex.map(work, rows), 1):
            d = dirs.get(path)
            if data is None:
                data = {"name": os.path.basename(path), "synopsis": "", "tags": [], "new_tags": [],
                        "kind": "unknown", "maturity": "sketch", "authorship": "unclear", "keep": "keep", "notable": ""}
                stats["errors"] += 1
            # guarantee at least one meaningful tag, plus language tags from file extensions
            if not data["tags"]:
                data["tags"] = [KIND_FALLBACK_TAG.get(data["kind"], "experiment")]
            data["lang"] = lang_tags(d) if d else []
            with _lock, con:  # workers write the llm cache on the same connection
                con.execute("INSERT OR REPLACE INTO classes VALUES (?,?,?,?)",
                            (path, getattr(client.b, "model", "?"), json.dumps(data), err))
            stats["done"] += 1
            if i % 25 == 0:
                log(f"  … classified {i}/{len(rows)}  ({client.tokens_in:,} tok in / {client.tokens_out:,} out, "
                    f"{client.cache_hits} cached)")
    return stats
