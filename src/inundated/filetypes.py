"""Extension tables, project markers, content sniffing and secret scrubbing."""
from __future__ import annotations

import mimetypes
import os
import re

SOURCE_EXT = {
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".inl", ".m", ".mm",
    ".py", ".pyw", ".pyx", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue", ".svelte",
    ".gd", ".gdshader", ".cs", ".fs", ".vb", ".rs", ".go", ".java", ".kt", ".kts", ".scala",
    ".lua", ".rb", ".php", ".pl", ".pm", ".r", ".jl", ".dart", ".swift", ".zig", ".nim",
    ".hs", ".ml", ".ex", ".exs", ".erl", ".clj", ".lisp", ".el", ".scm", ".rkt",
    ".sh", ".bash", ".zsh", ".ps1", ".psm1", ".bat", ".cmd", ".ahk", ".au3", ".vbs",
    ".html", ".htm", ".css", ".scss", ".sass", ".less",
    ".glsl", ".hlsl", ".shader", ".compute", ".frag", ".vert", ".wgsl", ".cg",
    ".v", ".sv", ".vh", ".vhd", ".vhdl", ".asm", ".s", ".ino", ".pde",
    ".sql", ".uc", ".usc", ".sol", ".tcl", ".awk", ".coffee", ".elm", ".purs",
}
DOC_EXT = {".md", ".markdown", ".txt", ".rst", ".org", ".adoc", ".tex", ".nfo"}
CONFIG_EXT = {
    ".json", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".conf", ".xml", ".properties",
    ".gradle", ".cmake", ".mk", ".csproj", ".vcxproj", ".sln", ".godot", ".uproject",
    ".sublime-project", ".editorconfig", ".gitignore", ".gitmodules", ".tres", ".tscn",
}
TEXT_NAMES = {"readme", "makefile", "dockerfile", "license", "licence", "changelog", "todo",
              "notes", "procfile", "gemfile", "rakefile", "vagrantfile", "jenkinsfile"}

CATEGORY_EXT = {
    "image": {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tga", ".tif", ".tiff", ".psd", ".webp",
              ".ico", ".svg", ".exr", ".hdr", ".dds", ".kra", ".xcf", ".aseprite", ".ase"},
    "audio": {".wav", ".mp3", ".ogg", ".flac", ".aif", ".aiff", ".m4a", ".mid", ".midi", ".opus"},
    "video": {".mp4", ".mkv", ".avi", ".mov", ".webm", ".wmv", ".flv", ".m4v"},
    "3d":    {".fbx", ".obj", ".blend", ".blend1", ".gltf", ".glb", ".dae", ".3ds", ".max", ".stl",
              ".ply", ".uasset", ".umap", ".mesh", ".vox"},
    "archive": {".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".pck", ".pak", ".jar"},
    "binary": {".exe", ".dll", ".so", ".dylib", ".lib", ".a", ".o", ".pdb", ".bin", ".hex",
               ".elf", ".class", ".pyc", ".wasm", ".msi", ".apk", ".bit", ".ilk", ".exp"},
    "data":  {".csv", ".tsv", ".db", ".sqlite", ".sqlite3", ".parquet", ".npy", ".npz", ".h5",
              ".pkl", ".log", ".dat"},
    "doc_binary": {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt"},
    "cad":   {".dwg", ".dxf", ".step", ".stp", ".iges", ".f3d", ".sldprt", ".kicad_pcb", ".kicad_sch",
              ".brd", ".sch", ".gbr"},
    "font":  {".ttf", ".otf", ".woff", ".woff2", ".fnt"},
}
_EXT_CATEGORY = {e: c for c, es in CATEGORY_EXT.items() for e in es}


def category(ext: str) -> str:
    if ext in SOURCE_EXT:
        return "code"
    if ext in DOC_EXT:
        return "docs"
    if ext in CONFIG_EXT:
        return "config"
    if ext in _EXT_CATEGORY:
        return _EXT_CATEGORY[ext]
    mt, _ = mimetypes.guess_type("x" + ext)
    if mt:
        major = mt.split("/")[0]
        return {"text": "docs", "image": "image", "audio": "audio", "video": "video"}.get(major, "other")
    return "other"


# --- Project markers -----------------------------------------------------------
# filename (lowercase) -> marker. "strong" markers define a project root on their own.
NAME_MARKERS = {
    "project.godot": "godot", "plugin.cfg": "godot-addon", "package.json": "node",
    "cargo.toml": "rust", "go.mod": "go", "pyproject.toml": "python", "setup.py": "python",
    "setup.cfg": "python", "pipfile": "python", "requirements.txt": "python-reqs",
    "cmakelists.txt": "cmake", "makefile": "make", "platformio.ini": "platformio",
    "pom.xml": "java", "build.gradle": "gradle", "build.gradle.kts": "gradle",
    "composer.json": "php", "gemfile": "ruby", "dockerfile": "docker", "docker-compose.yml": "docker",
    "deno.json": "deno", "tsconfig.json": "typescript", "pubspec.yaml": "flutter",
    "mix.exs": "elixir", "stack.yaml": "haskell", "build.zig": "zig", "meson.build": "meson",
    "manifest.json": "manifest", "index.html": "web", ".gitmodules": "submodules",
    "scrapy.cfg": "python", "manage.py": "python", "vagrantfile": "vagrant", "vcpkg.json": "cpp",
    "conanfile.txt": "cpp", "conanfile.py": "cpp", "build.sbt": "scala", "pyvenv.cfg": "python-venv",
    "readme.md": "readme", "readme.txt": "readme", "readme": "readme", "readme.rst": "readme",
}
EXT_MARKERS = {
    ".sln": "msvs", ".csproj": "dotnet", ".vcxproj": "msvc", ".vcproj": "msvc", ".fsproj": "dotnet",
    ".uproject": "unreal", ".uplugin": "unreal-plugin", ".xpr": "vivado", ".qpf": "quartus",
    ".ino": "arduino", ".pde": "processing", ".kicad_pro": "kicad", ".pro": "qmake",
    ".sublime-project": "sublime", ".gdextension": "godot-ext", ".xcodeproj": "xcode",
    ".code-workspace": "vscode", ".blend": "blender", ".dwg": "autocad", ".ioc": "stm32cube",
    ".uvprojx": "keil", ".cproject": "eclipse-cdt", ".atsln": "atmel-studio",
}
STRONG_MARKERS = {
    "godot", "godot-addon", "node", "rust", "go", "python", "platformio", "java", "gradle", "php",
    "ruby", "deno", "flutter", "elixir", "haskell", "zig", "meson", "browser-ext", "msvs", "dotnet",
    "msvc", "unreal", "unreal-plugin", "vivado", "quartus", "arduino", "processing", "kicad",
    "unity", "xcode", "stm32cube", "keil", "atmel-studio", "godot-ext", "qmake", "vagrant", "cpp", "scala",
}


def markers_for(dirpath: str, file_names: list[str], dir_names: list[str]) -> list[str]:
    found: set[str] = set()
    lower_dirs = {d.lower() for d in dir_names}
    for fn in file_names:
        low = fn.lower()
        m = NAME_MARKERS.get(low)
        if m == "manifest":
            m = _manifest_kind(os.path.join(dirpath, fn))
        if m:
            found.add(m)
        ext = os.path.splitext(low)[1]
        if ext in EXT_MARKERS:
            found.add(EXT_MARKERS[ext])
    if "assets" in lower_dirs and "projectsettings" in lower_dirs:
        found.add("unity")
    if any(d.endswith(".xcodeproj") for d in lower_dirs):
        found.add("xcode")
    return sorted(found)


def _manifest_kind(path: str) -> str | None:
    try:
        if os.path.getsize(path) > 256_000:
            return None
        with open(path, "rb") as f:
            data = f.read(256_000)
    except OSError:
        return None
    return "browser-ext" if b"manifest_version" in data else None


# --- Sniffing ------------------------------------------------------------------
SECRET_NAME = re.compile(
    r"(^\.env($|\.)|\.pem$|\.key$|\.pfx$|\.p12$|\.keystore$|\.jks$|^id_(rsa|dsa|ecdsa|ed25519)|"
    r"credential|secret|\.kdbx$|(^|[_.-])tokens?\.(json|txt|ya?ml)$|passw)", re.I)
SKIP_HEAD_NAME = re.compile(
    r"(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|cargo\.lock|poetry\.lock|composer\.lock|"
    r"gemfile\.lock|\.min\.(js|css)$|\.map$|\.import$|\.uid$)", re.I)


def is_secretish(name: str) -> bool:
    return bool(SECRET_NAME.search(name))


def sniff(path: str) -> tuple[str, bool]:
    """Return (mime, is_text) from file content, not just the extension."""
    try:
        with open(path, "rb") as f:
            chunk = f.read(8192)
    except OSError:
        return ("unreadable", False)
    if not chunk:
        return ("inode/x-empty", True)
    if chunk.startswith((b"\xff\xfe", b"\xfe\xff")):
        return ("text/plain; charset=utf-16", True)
    if b"\x00" not in chunk:
        try:
            chunk.decode("utf-8")
            return (mimetypes.guess_type(path)[0] or "text/plain", True)
        except UnicodeDecodeError as e:
            if e.start > len(chunk) - 4:  # cut mid multibyte char
                return (mimetypes.guess_type(path)[0] or "text/plain", True)
            printable = sum(32 <= b < 127 or b in (9, 10, 13) for b in chunk)
            if printable / len(chunk) > 0.9:
                return ("text/plain; charset=latin-1", True)
    try:
        import puremagic
        m = puremagic.from_string(chunk, mime=True)
        return (m or "application/octet-stream", False)
    except Exception:
        return ("application/octet-stream", False)


def head(path: str, n: int = 20, line_max: int = 200) -> str:
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= n:
                    break
                line = line.rstrip("\r\n")
                out.append(line if len(line) <= line_max else line[:line_max] + " …")
    except OSError:
        return ""
    return scrub("\n".join(out))


_SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S), "[REDACTED PRIVATE KEY]"),
    (re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED]"),
    (re.compile(r"\b(ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}\b"), "[REDACTED]"),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"), "[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "[REDACTED]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "[REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), "[REDACTED JWT]"),
    (re.compile(r"(://[^/\s:@]+:)[^@\s/]+@"), r"\1[REDACTED]@"),
    (re.compile(r"(?i)\b((?:api|access|secret|auth|client)[_-]?(?:key|token|secret)|password|passwd|pwd|token|oauth)"
                r"(\s*[:=]\s*[\"']?|\"\s*:\s*\")([^\s\"',;]{6,})"), r"\1\2[REDACTED]"),
]


def scrub(text: str) -> str:
    for pat, repl in _SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text
