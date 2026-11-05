"""Plain inventory report (markdown + a sortable/filterable single-file HTML table)."""
from __future__ import annotations

import html
import json
import os
import time
from collections import Counter

from .config import Config
from .gitinfo import parse_remote
from .walk import Dir, ext_summary, human


def _owned(host_owner: list[dict], cfg: Config) -> str:
    if not host_owner:
        return ""
    for p in host_owner:
        if (p.get("owner") or "").lower() in cfg.owner_handles or (p.get("user") or "").lower() in cfg.owner_handles:
            return "mine"
    return "other"


def rows(con, cfg: Config, dirs: dict[str, Dir], gits: dict[str, dict]) -> list[dict]:
    out = []
    for r in con.execute("SELECT * FROM items ORDER BY path"):
        d = dirs.get(r["path"])
        if not d or r["source"] == "root":
            continue
        gi = gits.get(r["path"]) or {}
        rem = gi.get("remotes") or {}
        parsed = [parse_remote(u) for u in rem.values()]
        hosts = sorted({p["host"] or "?" for p in parsed})
        flags = []
        if d.git and not gi.get("ok"):
            flags.append("git-broken")
        if gi.get("dirty"):
            flags.append(f"uncommitted:{gi['dirty']}")
        if gi.get("unpushed"):
            flags.append(f"unpushed:{gi['unpushed']}")
        if d.git and gi.get("ok") and not rem:
            flags.append("no-remote")
        risky = [h for h in hosts if h in cfg.at_risk_hosts]
        if risky and len(risky) == len(hosts):
            flags.append(f"only-on:{risky[0]}")
        if r["source"] == "nested":
            flags.append("nested")
        if r["confidence"] is not None and r["confidence"] < cfg.confident and r["source"] == "heuristic":
            flags.append("unsure")
        last = (gi.get("last_commit") or "")[:10] or (time.strftime("%Y-%m-%d", time.localtime(d.t_newest)) if d.t_newest else "")
        out.append({
            "path": os.path.relpath(d.path, d.root).replace("\\", "/"),
            "abs": d.path,
            "verdict": r["verdict"], "kind": r["kind"] or "", "conf": round(r["confidence"] or 0, 2),
            "source": r["source"], "reason": r["reason"] or "",
            "files": d.t_files, "bytes": d.t_bytes, "size": human(d.t_bytes),
            "ext": ext_summary(d.t_ext, 4), "markers": ",".join(d.markers),
            "last": last, "first": (gi.get("first_commit") or "")[:10],
            "commits": gi.get("commit_count") or 0,
            "remotes": " ".join(rem.values()), "hosts": ",".join(hosts),
            "owner": _owned(parsed, cfg), "flags": " ".join(flags),
        })
    return out


def write(con, cfg: Config, dirs, gits) -> tuple[str, str]:
    data = rows(con, cfg, dirs, gits)
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    md_path, html_path = cfg.out_dir / "report.md", cfg.out_dir / "report.html"
    by_v = Counter(r["verdict"] for r in data)
    by_k = Counter(r["kind"] or "?" for r in data if r["verdict"] in ("project", "collection"))
    by_h = Counter(h for r in data for h in (r["hosts"].split(",") if r["hosts"] else ["(none)"]) if r["verdict"] != "container")
    flags = Counter(f.split(":")[0] for r in data for f in r["flags"].split())
    years = Counter(r["last"][:4] for r in data if r["verdict"] in ("project", "collection") and r["last"])

    def tbl(c: Counter) -> str:
        return "\n".join(f"| {k} | {v} |" for k, v in c.most_common())

    md = [f"# inundated inventory\n\nGenerated {time.strftime('%Y-%m-%d %H:%M')} — roots: {', '.join(cfg.roots)}\n",
          "## Verdicts\n| verdict | n |\n|---|---|\n" + tbl(by_v),
          "## Kinds (projects)\n| kind | n |\n|---|---|\n" + tbl(by_k),
          "## Remote hosts\n| host | n |\n|---|---|\n" + tbl(by_h),
          "## Flags\n| flag | n |\n|---|---|\n" + tbl(flags),
          "## Last activity year\n| year | n |\n|---|---|\n" + "\n".join(f"| {y} | {n} |" for y, n in sorted(years.items())),
          "## Items\n| path | verdict | kind | conf/src | files | size | last | hosts | flags |\n|---|---|---|---|---|---|---|---|---|"]
    for r in data:
        md.append(f"| {r['path']} | {r['verdict']} | {r['kind']} | {r['conf']} {r['source']} | {r['files']} | {r['size']} | {r['last']} | {r['hosts']} | {r['flags']} |")
    md_path.write_text("\n\n".join(md[:6]) + "\n\n" + "\n".join(md[6:]) + "\n", encoding="utf-8")
    html_path.write_text(_HTML.replace("__DATA__", json.dumps(data).replace("</", "<\\/"))
                         .replace("__TITLE__", html.escape(f"inundated — {len(data)} items")), encoding="utf-8")
    return str(md_path), str(html_path)


_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><title>__TITLE__</title>
<style>
body{font:13px/1.4 system-ui,Segoe UI,sans-serif;margin:16px;background:#fafafa;color:#222}
input,select{font:inherit;padding:4px 6px;margin-right:6px}
table{border-collapse:collapse;width:100%;margin-top:10px;background:#fff}
th,td{border-bottom:1px solid #e3e3e3;padding:4px 6px;text-align:left;vertical-align:top}
th{position:sticky;top:0;background:#f0f0f0;cursor:pointer;user-select:none}
td.p{font-family:Consolas,monospace;font-size:12px}
.f{font-size:11px;color:#a33}.r{font-size:11px;color:#666;max-width:420px}
.v-container{color:#888}.v-non_code{color:#888;font-style:italic}.v-collection{color:#063}
#stats{color:#555}
</style></head><body>
<h2>__TITLE__</h2>
<div>
<input id="q" placeholder="search path / reason / remote…" size="40">
<select id="fv"><option value="">all verdicts</option><option>project</option><option>collection</option><option>container</option><option>non_code</option></select>
<select id="fk"><option value="">all kinds</option></select>
<select id="fh"><option value="">all hosts</option></select>
<select id="ff"><option value="">any flag</option></select>
<span id="stats"></span>
</div>
<table><thead><tr>
<th data-k="path">path</th><th data-k="verdict">verdict</th><th data-k="kind">kind</th><th data-k="conf">conf</th>
<th data-k="files">files</th><th data-k="bytes">size</th><th data-k="last">last</th><th data-k="commits">commits</th>
<th data-k="hosts">hosts</th><th data-k="flags">flags</th><th data-k="reason">why</th></tr></thead><tbody id="tb"></tbody></table>
<script>
const D=__DATA__;let sk="path",sd=1;
const $=id=>document.getElementById(id);
function opts(sel,vals){[...new Set(vals)].filter(Boolean).sort().forEach(v=>{const o=document.createElement("option");o.textContent=v;sel.appendChild(o)})}
opts($("fk"),D.map(r=>r.kind));opts($("fh"),D.flatMap(r=>r.hosts.split(",")));opts($("ff"),D.flatMap(r=>r.flags.split(" ").map(f=>f.split(":")[0])));
function esc(s){return String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
function draw(){const q=$("q").value.toLowerCase(),v=$("fv").value,k=$("fk").value,h=$("fh").value,f=$("ff").value;
 const rs=D.filter(r=>(!v||r.verdict==v)&&(!k||r.kind==k)&&(!h||r.hosts.split(",").includes(h))&&(!f||r.flags.split(" ").some(x=>x.split(":")[0]==f))
  &&(!q||(r.path+" "+r.reason+" "+r.remotes+" "+r.markers+" "+r.ext).toLowerCase().includes(q)));
 rs.sort((a,b)=>(a[sk]>b[sk]?1:a[sk]<b[sk]?-1:0)*sd);
 $("stats").textContent=rs.length+" shown";
 $("tb").innerHTML=rs.map(r=>`<tr class="v-${r.verdict}"><td class="p" title="${esc(r.abs)}">${esc(r.path)}<div class="r">${esc(r.ext)}${r.markers?" · "+esc(r.markers):""}</div></td><td>${r.verdict}</td><td>${esc(r.kind)}</td><td>${r.conf} <small>${r.source}</small></td><td>${r.files}</td><td>${r.size}</td><td>${r.last}</td><td>${r.commits||""}</td><td>${esc(r.hosts)}<div class="r">${esc(r.owner)}</div></td><td class="f">${esc(r.flags)}</td><td class="r">${esc(r.reason)}</td></tr>`).join("")}
document.querySelectorAll("th").forEach(th=>th.onclick=()=>{const k=th.dataset.k;sd=sk==k?-sd:1;sk=k;draw()});
["q","fv","fk","fh","ff"].forEach(id=>$(id).oninput=draw);draw();
</script></body></html>"""
