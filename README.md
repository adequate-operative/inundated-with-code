# inundated

Scans sprawling code folders, works out where each project starts and ends, flags git risk
(unpushed commits, dirty trees, repos stranded on hosts you're leaving), and uses cheap LLMs to
name, summarise and tag every project into a filterable card catalogue.

## Quick start

You need **Python 3.12+**, [**uv**](https://docs.astral.sh/uv/), **git** on your PATH, and **one API
key** for an OpenAI-compatible model provider. The defaults use [Neuralwatt](https://portal.neuralwatt.com)
(`gemma-4-31b`), so you need a `NEURALWATT_API_KEY`. See [Models](#models) to use another provider,
a local model, or no model at all.

```bash
git clone https://github.com/adequate-operative/inundated-with-code
cd inundated-with-code
uv sync
uv run inundated init --root "D:/Code" --handle your-github-username
```

`init` creates a `local/` folder holding your personal workspace. Put your key in **`local/.env`**:

```ini
NEURALWATT_API_KEY=sk-...
```

Then do the whole thing:

```bash
uv run inundated run --open
```

That runs scan → discover → evidence → classify → report and opens `local/out/cards.html`, with
one card per project and tags you can click to filter. A first run checks your key with one tiny
call before it starts.

- **Try it cheaply first:** `uv run inundated run --limit 20` classifies only 20 projects.
- **No key / nothing leaves your machine:** `uv run inundated run --no-llm` gives a rules-only
  inventory (boundaries, git risk, sizes, dates) with no synopses or tags.
- **Re-running is cheap:** model answers are cached, so only new or changed projects cost anything.
  Add `--skip-scan` to reuse the last filesystem scan.

`--root` can be given several times. `--handle` is your username on GitHub/Bitbucket/GitLab. It is
how inundated tells your repos from third-party clones. Edit both later in `local/inundated.toml`.

## Multiple roots

Scan several folders at once by listing them in `[scan] roots` or repeating `--root`:

```bash
uv run inundated run --root "D:/Code" --root "E:/OldDrive/src"
```

Or add them in **separate runs**. Everything goes into the same database and one combined
catalogue:

```bash
uv run inundated run --root "D:/Code"
uv run inundated run --root "E:/OldDrive/src"     # adds to what's there; D:/Code is kept
uv run inundated roots                            # list what's in the database
uv run inundated forget "E:/OldDrive/src"         # remove a root again
```

Scanning a path always means "make the catalogue's picture of this path fresh":

- **Same root again:** its data is replaced, and other roots are untouched.
- **A folder inside a root you already scanned** (`--root "D:/Code/Projects"` after `D:/Code`):
  only that subtree is rescanned. It stays part of `D:/Code`, the folder totals above it are
  updated, and discovery reruns from `D:/Code`, which is fast because model answers are cached.
  Use this to refresh the corner you've been working in without rescanning the whole drive.
- **A parent of a root you already scanned** (`D:/Code` after `D:/Code/Projects`): the parent
  absorbs it, so you end up with one root and no duplicates. Overrides and cached model answers are
  stored by path and carry over.

One thing is refused: naming a folder *and* its parent **in the same command**
(`--root "D:/Code" --root "D:/Code/Projects"`). The second is already part of the first, so it's
almost certainly a mistake.

## What you get

`local/out/cards.html` has a card per project with a name, a 2–3 sentence synopsis, tags (`game-dev`,
`win32`, `browser-ext`, `music-production`, `yarr`, `toy`, `experiment`, … plus `lang:*`), maturity,
authorship, a keep/archive hint, last activity, git hosts and warnings (`uncommitted`, `unpushed`,
`no-remote`, `only-on:<host>`). You can search and filter by tag, folder, maturity, keep and
authorship. `out/report.html` (sortable table) and `out/report.md` list every folder verdict.

## Where things live

```
inundated-with-code/        this repo: the tool
└── local/                  gitignored, yours (optionally its own repo: init --git)
    ├── inundated.toml      roots, your handles/hosts, rules, model backends
    ├── .env                API keys
    ├── inundated.db        scan, verdicts, cached model answers, classifications
    └── out/                cards.html, report.html, report.md
```

Run commands from the repo folder; `./local/inundated.toml` is found automatically. To use a
different workspace, pass `-c PATH` or set `INUNDATED_CONFIG`.

## Models

Backends are defined in `local/inundated.toml`. `[discover] backend` picks the one that settles unsure
project boundaries, and `[classify] backend` picks the one that writes synopses and tags.

- **Any OpenAI-compatible API** (`type = "openai"`): set `base_url`, `model` and `api_key_env`
  (the variable name to read from `.env`). The template has Neuralwatt `gemma-4-31b`,
  `deepseek-v4.1-flash` and `qwen3.6-35b-fast` ready.
- **Local, no key:** point the `local` backend at Ollama (`http://localhost:11434/v1`), then set
  both `backend = "local"`.
- **Decision models** (`type = "systemone"`, e.g. Neuralwatt `clef-flash`, preview access only) can
  act as the boundary judge and return real probabilities instead of generated text.

Cost: a full run over ~1,000 projects was about 4.2M input tokens and 150k output tokens, roughly
$0.70 on `gemma-4-31b`.

What is sent: folder trees, the first 20 lines of READMEs/manifests/source files, git remote URLs
and recent commit subjects. Secret-looking files (`.env`, keys, credentials) are never read, and
token/password patterns are redacted from what is.

## Individual steps

`run` is these in order. Use them directly to redo one stage or to tune the rules.

| command | does |
|---|---|
| `inundated scan [--root PATH ...]` | walk the roots (never enters `node_modules`, venvs, build caches) and collect read-only git facts |
| `inundated discover [--judge heuristic\|llm-ambiguous\|llm-all] [--root PATH]` | decide project boundaries: rules first, the model only where unsure (default). Covers every root in the database unless `--root` is given |
| `inundated evidence [--dump]` | build the per-project text the model reads (`--dump` writes them to `out/evidence/`) |
| `inundated classify [--limit N] [--only TEXT]` | synopsis, tags and labels per project |
| `inundated report [--open]` | write the cards, table and markdown |

Tuning:

```bash
uv run inundated context "D:/Code/some/folder" --system   # exactly what the model sees for a folder
uv run inundated override "D:/Code/some/folder" container --note "just a folder"   # pin a verdict
uv run inundated ping --backend gemma                      # test a backend
```

Rule settings such as folders kept whole (`sealed_hints`), organising-folder names and prefixes,
skipped folders and hosts you're leaving are in `local/inundated.toml`, with comments.
