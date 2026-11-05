# inundated

Make sense of years of accumulated code projects. inundated walks your code folders, figures out
where each project starts and ends, collects git facts (remotes, unpushed work, dirty trees), and
asks a cheap LLM for a name, synopsis and tags per project. The result is a filterable card view
of everything you have.

```
scan ──► discover ──► evidence ──► classify ──► report (cards.html)
```

- **scan**: one filesystem pass (never enters `node_modules`, virtualenvs or build caches) plus
  read-only git facts.
- **discover**: top-down project-boundary detection. Rules decide most folders. A judge model
  is asked only where the rules are unsure. It sees an ASCII subtree, git remotes, and the first
  lines of source/doc files after MIME sniffing and secret scrubbing.
- **classify**: one short-context call per project returns a synopsis, tags from a controlled
  vocabulary, kind, maturity, authorship and a keep signal. Responses are cached, so re-runs only
  pay for projects that changed.
- **report**: `out/cards.html` (project cards, click tags to filter), `out/report.html` (table),
  `out/report.md`.

## Setup

```bash
uv sync
uv run inundated init local --root "D:/Code" --git    # personal workspace, its own git repo
# edit local/inundated.toml ([owner] handles, self_hosts, ...) and put keys in local/.env
```

`local/` is gitignored by this repo. It holds your config, `.env`, the SQLite database and the
reports, and `--git` makes it a separate repository, so your data can be versioned and backed up
privately. The CLI finds `./local/inundated.toml` automatically. You can also point elsewhere with
`-c PATH` or `INUNDATED_CONFIG`. Paths in the config are relative to the config file.

## Use

```bash
uv run inundated scan                          # walk roots + git facts
uv run inundated discover --judge heuristic    # rules only, no API calls
uv run inundated context "<folder>" --system   # print exactly what the judge model would see
uv run inundated ping --backend gemma          # test a model endpoint
uv run inundated discover                      # rules + judge model for unsure folders
uv run inundated override "<folder>" container --note "just a folder"
uv run inundated evidence [--dump]             # per-project evidence packs
uv run inundated classify                      # synopsis + tags per project
uv run inundated report                        # out/cards.html, out/report.html, out/report.md
```

## Models

Any OpenAI-compatible endpoint works (`type = "openai"`): Neuralwatt, DeepSeek, Ollama, LM Studio
and so on. Decision models in the Clef/Jev style (`type = "systemone"`, e.g. `clef-flash`) can act as
the boundary judge. They return calibrated probabilities instead of generated text. A full
classification of ~1,000 projects is about 3.5M input tokens, under $1 on small open models.

Text sent to a model: file trees, manifest/README/source *heads* (20 lines), git remotes and
recent commit subjects. Files that look like secrets are never read, and token/password/key
patterns are redacted. Use a local backend if nothing should leave your machine.
