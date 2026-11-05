"""out/cards.html: one card per project, filterable by tag. Deliberately tiny."""
from __future__ import annotations

import json
from collections import Counter

from . import report


def write(con, cfg, dirs, gits) -> str:
    cls = {}
    if con.execute("SELECT name FROM sqlite_master WHERE name='classes'").fetchone():
        cls = {r[0]: json.loads(r[1]) for r in con.execute("SELECT path, data FROM classes")}
    cards = []
    for r in report.rows(con, cfg, dirs, gits):
        if r["verdict"] not in ("project", "collection"):
            continue
        c = cls.get(r["abs"], {})
        tags = c.get("tags") or [{"third-party-download": "third-party"}.get(r["kind"], "unclassified")]
        cards.append({
            "name": c.get("name") or r["path"].rsplit("/", 1)[-1], "path": r["path"], "abs": r["abs"],
            "syn": c.get("synopsis", ""), "note": c.get("notable", ""),
            "tags": tags, "extra": c.get("new_tags", []), "lang": c.get("lang", []),
            "kind": c.get("kind") or r["kind"], "mat": c.get("maturity", ""), "auth": c.get("authorship", ""),
            "keep": c.get("keep", ""), "last": r["last"], "commits": r["commits"], "size": r["size"],
            "hosts": r["hosts"], "flags": r["flags"], "area": "/".join(r["path"].split("/")[:2]),
            "coll": r["verdict"] == "collection",
        })
    tagc = Counter(t for c in cards for t in c["tags"])
    out = cfg.out_dir / "cards.html"
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    out.write_text(PAGE.replace("__DATA__", json.dumps(cards).replace("</", "<\\/"))
                   .replace("__TAGS__", json.dumps(tagc.most_common())), encoding="utf-8")
    return str(out)


PAGE = r"""<!doctype html><meta charset="utf-8"><title>inundated — projects</title>
<style>
body{font:14px/1.45 system-ui,Segoe UI,sans-serif;margin:0;background:#f4f4f2;color:#222}
header{position:sticky;top:0;background:#fff;border-bottom:1px solid #ddd;padding:10px 16px;z-index:1}
input,select{font:inherit;padding:3px 6px;margin:0 6px 6px 0}
#cloud span,.t{display:inline-block;padding:1px 7px;margin:2px;border-radius:9px;background:#e6eef7;cursor:pointer;font-size:12px}
#cloud span.on,.t.on{background:#2b6cb0;color:#fff}
.l{background:#eee;color:#555}.x{background:#fff;border:1px dashed #aaa}
main{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:12px;padding:14px}
.c{background:#fff;border-radius:8px;padding:10px 12px;box-shadow:0 1px 2px #0002}
.c h3{margin:0 0 2px;font-size:15px}.p{font:11px Consolas,monospace;color:#777;word-break:break-all}
.s{margin:6px 0}.n{color:#666;font-style:italic;font-size:12px}.m{font-size:12px;color:#555;margin-top:6px}
.b{font-size:11px;padding:0 5px;border-radius:4px;margin-left:4px;background:#eee}
.complete,.usable,.revive{background:#d7f5dd}.stub,.delete-candidate{background:#fde2e2}
.f{color:#b03030}
</style>
<header>
<input id="q" placeholder="search name, synopsis, path…" size="34">
<select id="area"><option value="">all folders</option></select>
<select id="mat"><option value="">any maturity</option><option>complete</option><option>usable</option><option>half-finished</option><option>sketch</option><option>stub</option></select>
<select id="keep"><option value="">any keep</option><option>revive</option><option>keep</option><option>archive</option><option>delete-candidate</option></select>
<select id="auth"><option value="">any authorship</option><option>mine</option><option>modified-fork</option><option>third-party</option><option>unclear</option></select>
<select id="sort"><option value="last">newest first</option><option value="name">name</option><option value="commits">most commits</option></select>
<b id="n"></b>
<div id="cloud"></div>
</header>
<main id="g"></main>
<script>
const D=__DATA__, T=__TAGS__, on=new Set(), $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
[...new Set(D.map(c=>c.area))].sort().forEach(a=>$("area").add(new Option(a)));
$("cloud").innerHTML=T.map(([t,n])=>`<span data-t="${esc(t)}">${esc(t)} ${n}</span>`).join("");
document.addEventListener("click",e=>{const t=e.target.dataset.t;if(!t)return;on.has(t)?on.delete(t):on.add(t);draw()});
function draw(){
 const q=$("q").value.toLowerCase(),f=k=>$(k).value;
 let r=D.filter(c=>[...on].every(t=>c.tags.includes(t)||c.lang.includes(t)||c.extra.includes(t))
  &&(!f("area")||c.area==f("area"))&&(!f("mat")||c.mat==f("mat"))&&(!f("keep")||c.keep==f("keep"))&&(!f("auth")||c.auth==f("auth"))
  &&(!q||(c.name+" "+c.syn+" "+c.path+" "+c.note).toLowerCase().includes(q)));
 const s=f("sort");r.sort((a,b)=>s=="name"?a.name.localeCompare(b.name):s=="commits"?b.commits-a.commits:(b.last>a.last?1:-1));
 $("n").textContent=r.length+" / "+D.length;
 document.querySelectorAll("#cloud span").forEach(x=>x.classList.toggle("on",on.has(x.dataset.t)));
 const chip=(t,cls)=>`<span class="t ${cls}${on.has(t)?" on":""}" data-t="${esc(t)}">${esc(t)}</span>`;
 $("g").innerHTML=r.map(c=>`<div class="c"><h3>${esc(c.name)}${c.coll?'<span class="b">collection</span>':""}${[c.mat,c.keep].filter(Boolean).map(b=>`<span class="b ${b}">${b}</span>`).join("")}</h3>
<div class="p" title="${esc(c.abs)}">${esc(c.path)}</div><div class="s">${esc(c.syn)}</div>${c.note?`<div class="n">${esc(c.note)}</div>`:""}
<div>${c.tags.map(t=>chip(t,"")).join("")}${c.extra.map(t=>chip(t,"x")).join("")}${c.lang.map(t=>chip(t,"l")).join("")}</div>
<div class="m">${esc(c.kind)} · ${esc(c.auth)} · ${c.last||"?"} · ${c.commits?c.commits+" commits · ":""}${c.size}${c.hosts?" · "+esc(c.hosts):""} <span class="f">${esc(c.flags)}</span></div></div>`).join("");
}
["q","area","mat","keep","auth","sort"].forEach(id=>$(id).oninput=draw);draw();
</script>"""
