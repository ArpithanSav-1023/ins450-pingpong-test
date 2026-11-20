"""Selection list for the sender dashboard: choose which sentences / groups / the radar stream the sender sends.

    .venv/bin/python tools/selector.py          # http://127.0.0.1:9104   (started by scripts/start_stack.sh, embedded in the SENDER dashboard)
Writes data/selection.json; the running sender (traffic/pp.py role A) picks a change up within about a second. Stdlib only, localhost only.
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from traffic import selection  # noqa: E402

SC = json.load(open(os.path.join(ROOT, "scenarios", "pp9_025mbps.json")))
CATALOG = selection.catalog([g["name"] for g in SC["groups"]])
ADDR = {g["name"]: f"{g['addr']}:{g['port']}" for g in SC["groups"]}
ADDR["RADAR"] = "239.6.7.8:6678"

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Sender selection</title>
<style>
:root{--bg:#fff;--fg:#1f2328;--mut:#6b7280;--line:#d8dee4;--acc:#2563eb;--hd:#f3f4f6}
@media (prefers-color-scheme:dark){:root{--bg:#111217;--fg:#e6e8eb;--mut:#9aa3ad;--line:#2c3038;--acc:#6ea8fe;--hd:#1b1d24}}
body{margin:0;padding:8px 12px;background:var(--bg);color:var(--fg);font:13px system-ui,sans-serif}
.bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;position:sticky;top:0;background:var(--bg);padding:4px 0 8px;border-bottom:1px solid var(--line)}
button,input[type=search]{font:inherit;color:var(--fg);background:var(--hd);border:1px solid var(--line);border-radius:6px;padding:4px 10px}
button{cursor:pointer}button:hover{border-color:var(--acc)}
.cnt{margin-left:auto;color:var(--mut)}
.grp{border:1px solid var(--line);border-radius:6px;margin:8px 0;overflow:hidden}
.gh{background:var(--hd);padding:5px 10px;font-weight:600;display:flex;gap:8px;align-items:center}
.gh small{color:var(--mut);font-weight:400}
.it{padding:3px 10px 3px 30px;display:flex;gap:8px;align-items:center;border-top:1px solid var(--line)}
label{cursor:pointer;display:flex;gap:8px;align-items:center;width:100%}
.off{color:var(--mut)}
.msg{color:var(--mut);font-size:12px;margin:6px 0}
</style></head><body>
<div class="bar"><button onclick="setAll(true)">Send everything</button><button onclick="setAll(false)">Send nothing</button>
<input id="q" type="search" placeholder="filter (e.g. NAVD, GPGGA, SI0001)" oninput="draw()"><span class="cnt" id="cnt"></span></div>
<div class="msg">Ticked = sent. A change reaches a running sender within about a second and stays for the next runs. Sentences left out are not counted as lost.</div>
<div id="list"></div>
<script>
let cat=[],off=new Set();
async function load(){const r=await (await fetch('api/catalog')).json();cat=r.catalog;off=new Set(r.disabled);draw()}
async function save(){await fetch('api/selection',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({disabled:[...off]})});draw()}
function setAll(on){off=on?new Set():new Set(cat.map(c=>c.key));save()}
function setGroup(g,on){cat.filter(c=>c.group===g).forEach(c=>on?off.delete(c.key):off.add(c.key));save()}
function setOne(k,on){on?off.delete(k):off.add(k);save()}
function draw(){
 const q=document.getElementById('q').value.toLowerCase(),by={};
 cat.forEach(c=>{if(!q||(c.key+' '+c.label+' '+c.addr).toLowerCase().includes(q))(by[c.group]=by[c.group]||[]).push(c)});
 let h='';
 for(const g in by){const all=cat.filter(c=>c.group===g),n=all.filter(c=>!off.has(c.key)).length;
  h+=`<div class="grp"><div class="gh"><label><input type="checkbox" ${n===all.length?'checked':''} ${n>0&&n<all.length?'data-p=1':''} onchange="setGroup('${g}',this.checked)">${g} <small>${all[0].addr} · ${n}/${all.length} sent</small></label></div>`;
  by[g].forEach(c=>{h+=`<div class="it ${off.has(c.key)?'off':''}"><label><input type="checkbox" ${off.has(c.key)?'':'checked'} onchange="setOne('${c.key}',this.checked)">${c.label}</label></div>`});
  h+='</div>'}
 document.getElementById('list').innerHTML=h||'<div class="msg">nothing matches</div>';
 document.querySelectorAll('input[data-p]').forEach(e=>e.indeterminate=true);
 document.getElementById('cnt').textContent=(cat.length-off.size)+' of '+cat.length+' sent';
}
load();
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/api/catalog"):
            s = selection.Selector()
            s.refresh()
            self._send(200, json.dumps({"catalog": [{"key": k, "group": g, "label": l, "addr": ADDR.get(g, "")} for k, g, l in CATALOG],
                                        "disabled": sorted(s.disabled)}), "application/json")
        elif self.path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        if self.path != "/api/selection":
            return self._send(404, "not found", "text/plain")
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            known = {k for k, _, _ in CATALOG}
            selection.save([k for k in body.get("disabled", []) if k in known])
            self._send(200, "{}", "application/json")
        except (ValueError, TypeError):
            self._send(400, "bad request", "text/plain")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9104
    print(f"[selector] http://127.0.0.1:{port}  ({len(CATALOG)} entries) -> {selection.PATH}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
