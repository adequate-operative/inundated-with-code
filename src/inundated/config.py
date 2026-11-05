from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def norm(p: str | os.PathLike) -> str:
    return os.path.normpath(os.path.abspath(os.path.expanduser(os.fspath(p))))


def key(p: str) -> str:
    """Case-folded path for lookups (Windows filesystems are case-insensitive)."""
    return os.path.normcase(norm(p))


@dataclass
class Backend:
    name: str
    type: str = "openai"
    base_url: str = ""
    model: str = ""
    api_key_env: str = ""
    json_mode: bool = True
    temperature: float = 0.0
    max_tokens: int = 600
    timeout: float = 90.0
    extra_headers: dict = field(default_factory=dict)
    extra_body: dict = field(default_factory=dict)

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) if self.api_key_env else None


@dataclass
class Config:
    home: Path
    db_path: Path
    out_dir: Path
    roots: list[str]
    exclude_names: set[str]
    exclude_paths: set[str]
    prune_always: set[str]
    prune: set[str]
    git_workers: int
    owner_handles: set[str]
    owner_emails: set[str]
    owner_names: set[str]
    self_hosts: set[str]
    judge: str
    confident: float
    judge_backend: str
    judge_workers: int
    classify_backend: str
    container_hints: set[str]
    sealed_hints: set[str]
    organiser_prefixes: list[str]
    at_risk_hosts: set[str]
    ctx: dict
    backends: dict[str, Backend]


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if v and k not in os.environ:
            os.environ[k] = v


class ConfigNotFound(RuntimeError):
    pass


# Where to look, in order. Your personal workspace (config, .env, db, out/) normally lives in a
# gitignored ./local/ folder, which can be its own git repo.
SEARCH = ("local/inundated.toml", "inundated.toml")


def find(path: str | None = None) -> Path:
    if path:
        p = Path(path)
        return (p / "inundated.toml" if p.is_dir() else p).resolve()
    if os.environ.get("INUNDATED_CONFIG"):
        return Path(os.environ["INUNDATED_CONFIG"]).resolve()
    for cand in SEARCH:
        if Path(cand).is_file():
            return Path(cand).resolve()
    raise ConfigNotFound("no inundated.toml found (looked in ./local/ and ./). "
                         "Create a workspace with:  inundated init local")


def load(path: str | None = None) -> Config:
    cfg_path = find(path)
    if not cfg_path.is_file():
        raise ConfigNotFound(f"config not found: {cfg_path}")
    home = cfg_path.parent
    _load_dotenv(home / ".env")
    raw = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    scan, owner, disc, ctx = (raw.get(k, {}) for k in ("scan", "owner", "discover", "context"))
    if not scan.get("roots"):
        raise ConfigNotFound(f"{cfg_path}: set [scan] roots to the folder(s) you want scanned")
    low = lambda xs: {x.lower() for x in xs}
    backends = {name: Backend(name=name, **vals) for name, vals in raw.get("backends", {}).items()}
    return Config(
        home=home,
        db_path=home / raw.get("db", "inundated.db"),
        out_dir=home / raw.get("out", "out"),
        roots=[norm(r) for r in scan["roots"]],
        exclude_names=low(scan.get("exclude_names", [])),
        exclude_paths={key(os.path.expanduser(p)) for p in scan.get("exclude_paths", [])},
        prune_always=low(scan.get("prune_always", ["node_modules", ".git"])) | {"node_modules", ".git"},
        prune=low(scan.get("prune", [])),
        git_workers=int(scan.get("git_workers", 8)),
        owner_handles=low(owner.get("handles", [])),
        owner_emails=low(owner.get("emails", [])),
        owner_names=low(owner.get("names", [])),
        self_hosts=low(owner.get("self_hosts", [])),
        judge=disc.get("judge", "llm-ambiguous"),
        confident=float(disc.get("confident", 0.8)),
        judge_backend=disc.get("backend", "judge"),
        judge_workers=int(disc.get("workers", 4)),
        classify_backend=raw.get("classify", {}).get("backend", "gemma"),
        container_hints=low(disc.get("container_hints", [])),
        sealed_hints=low(disc.get("sealed_hints", [])),
        organiser_prefixes=[p.lower() for p in disc.get("organiser_prefixes", ["_"])],
        at_risk_hosts=low(raw.get("report", {}).get("at_risk_hosts", [])),
        ctx={
            "tree_depth": 3, "tree_max_lines": 140, "head_lines": 20,
            "judge_head_files": 8, "judge_head_chars": 9000,
            "evidence_head_files": 16, "evidence_head_chars": 16000,
            **ctx,
        },
        backends=backends,
    )
