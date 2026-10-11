#!/usr/bin/env python3
"""Agnes Storyboard — web backend (v4).

~1-minute AI video from 1-5 images + a scenario. Features:
  - duration 30s / 60s / 120s (3 / 5 / 10 shots)
  - French narration (male or female voice) + 3 background music choices
  - one image per shot (cycled if fewer images)
  - optional burned-in subtitles
  - MP4 + GIF outputs, permanent share link, auto title, gallery/history
  - per-shot regeneration, FIFO job queue, live log console

Run:  python3 server.py
"""
import base64
import json
import os
import queue
import re
import secrets
import subprocess
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
WORK = BASE_DIR / "jobs"
WORK.mkdir(exist_ok=True)
GALLERY = BASE_DIR / "gallery"
GALLERY.mkdir(exist_ok=True)
PORT = int(os.environ.get("APP_PORT", "8647"))
HOST = os.environ.get("APP_HOST", "0.0.0.0")

FFMPEG = "/root/.hermes/tools/ffmpeg-9.0.1-linux-x64/bin/ffmpeg"

AGNES_API = "https://apihub.agnes-ai.com"
AGNES_MODEL = "agnes-video-2.5-flash"
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-v4-pro"
MUSIC_VOLUME = 0.15

VOICES = {"homme": "fr-FR-HenriNeural", "femme": "fr-FR-DeniseNeural"}
MUSIC_TRACKS = {
    "ambiant": BASE_DIR / "music.mp3",
    "mysterieux": BASE_DIR / "music_ritual.mp3",
    "doux": BASE_DIR / "music_wish.mp3",
}
# duration key -> (num shots, seconds per shot)
DURATIONS = {"30": (3, "10"), "60": (5, "12"), "120": (10, "12")}

def _env_key(name):
    v = os.environ.get(name, "").strip()
    if v:
        return v
    env = Path("/root/.hermes/profiles/jorvas/.env")
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""

AGNES_KEY = _env_key("AGNES_API_KEY")
DEEPSEEK_KEY = _env_key("DEEPSEEK_API_KEY")

TOKEN_PATH = BASE_DIR / "token.txt"
if TOKEN_PATH.exists():
    TOKEN = TOKEN_PATH.read_text().strip()
else:
    TOKEN = secrets.token_hex(24)
    TOKEN_PATH.write_text(TOKEN + "\n")
    os.chmod(TOKEN_PATH, 0o600)

JOBS = {}
JOBS_LOCK = threading.Lock()

def set_job(job_id, **kw):
    with JOBS_LOCK:
        JOBS.setdefault(job_id, {}).update(kw)

def get_job(job_id):
    with JOBS_LOCK:
        return dict(JOBS.get(job_id, {}))

def log(job_id, msg):
    with JOBS_LOCK:
        entry = JOBS.setdefault(job_id, {})
        entry.setdefault("log", []).append({"t": time.time(), "m": msg})
        entry["log"] = entry["log"][-300:]

# ---- helpers ---------------------------------------------------------------
def http_json(url, payload=None, headers=None, method="POST"):
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode())

def deepseek_scenario(theme, n_shots):
    sys = ("You are a film director. Answer directly, no reasoning, no preamble. Output EXACTLY "
           f"{n_shots+1} lines. FIRST line: a short catchy French title (max 6 words, no punctuation). "
           f"Then {n_shots} lines: each one a vivid English visual prompt for a text-to-video "
           "model, keeping the SAME main character throughout. No numbering, no headings.")
    d = http_json(DEEPSEEK_URL,
                  payload={"model": DEEPSEEK_MODEL, "messages": [
                      {"role": "system", "content": sys},
                      {"role": "user", "content": theme},
                  ], "max_tokens": 4000},
                  headers={"Authorization": f"Bearer {DEEPSEEK_KEY}"})
    content = ((d.get("choices") or [{}])[0].get("message") or {}).get("content", "")
    lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
    title = (lines[0] if lines else theme)[:80]
    prompts = lines[1:1 + n_shots]
    while len(prompts) < n_shots:
        prompts.append(theme)
    return title, prompts[:n_shots]

def agnes_create(prompt, data_uris, aspect_ratio, seconds):
    payload = {
        "model": AGNES_MODEL, "prompt": prompt, "mode": "reference",
        "images": data_uris, "seconds": seconds, "size": "720P",
        "aspect_ratio": aspect_ratio, "n": 1,
    }
    try:
        d = http_json(AGNES_API + "/v1/videos", payload,
                      headers={"Authorization": f"Bearer {AGNES_KEY}"})
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Agnes HTTP {e.code}: {e.read().decode()[:300]}")
    vid = d.get("video_id") or d.get("id")
    if not vid:
        raise RuntimeError(f"Agnes no video_id: {json.dumps(d)[:300]}")
    return vid

def agnes_download(video_id, dest):
    for _ in range(60):
        try:
            d = http_json(f"{AGNES_API}/agnesapi?video_id={video_id}&model_name={AGNES_MODEL}",
                          headers={"Authorization": f"Bearer {AGNES_KEY}"}, method="GET")
        except Exception:
            time.sleep(8)
            continue
        st = str(d.get("status", "")).lower()
        if st in {"completed", "succeeded", "success", "done"}:
            md = d.get("metadata") or {}
            url = d.get("url") or md.get("url")
            if not url:
                raise RuntimeError(f"Agnes done but no url: {json.dumps(d)[:300]}")
            with urllib.request.urlopen(urllib.request.Request(url), timeout=180) as r:
                dest.write_bytes(r.read())
            return True
        if st in {"failed", "error", "cancelled"}:
            raise RuntimeError(f"Agnes status={st}: {json.dumps(d, ensure_ascii=False)[:400]}")
        time.sleep(12)
    raise RuntimeError("Agnes poll timed out")

def tts_voice(text, dest, voice):
    import asyncio
    import edge_tts
    async def go():
        await edge_tts.Communicate(text, voice).save(str(dest))
    asyncio.run(go())

def concat_takes(takes, out):
    lst = BASE_DIR / "_concat.txt"
    lst.write_text("".join(f"file '{t}'\n" for t in takes))
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "concat", "-safe", "0",
                    "-i", str(lst), "-c", "copy", str(out)], check=True)

def _dur(path):
    r = subprocess.run([FFMPEG, "-i", str(path)], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr or "")
    if m:
        return int(m.group(1))*3600 + int(m.group(2))*60 + float(m.group(3))
    return 61.0

def build_audio(video, voice, out, music_path, duration):
    inputs = ["-i", str(video), "-i", str(voice)]
    fc = "[1:a]adelay=1200|1200[v]"
    if music_path and Path(music_path).exists():
        inputs += ["-stream_loop", "-1", "-i", str(music_path)]
        fc += f";[2:a]volume={MUSIC_VOLUME}[m];[v][m]amix=inputs=2:duration=first:dropout_transition=0[a]"
    else:
        fc += ";[v]apad[a]"
    subprocess.run([FFMPEG, "-y", "-v", "error", *inputs, "-filter_complex", fc,
                    "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac",
                    "-b:a", "128k", "-t", str(duration), str(out)], check=True)

def make_srt(text, duration):
    phrases = [p.strip() for p in re.split(r"[.!?…\n]+", text) if p.strip()] or [text]
    n = len(phrases)
    step = duration / n
    def ts(t):
        t = max(0, t)
        return f"{int(t//3600):02d}:{int(t%3600//60):02d}:{int(t%60):02d},{int((t%1)*1000):03d}"
    parts = []
    for i, p in enumerate(phrases):
        parts.append(f"{i+1}\n{ts(i*step)} --> {ts((i+1)*step - 0.1)}\n{p}\n")
    return "\n".join(parts)

def burn_subtitles(video, srt, out):
    subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(video), "-vf",
                    f"subtitles={srt}:force_style='FontSize=13,Alignment=2,MarginV=30,Outline=1'",
                    "-c:a", "copy", str(out)], check=True)

def make_gif(video, out):
    subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(video),
                    "-vf", "fps=10,scale=360:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse",
                    "-loop", "0", str(out)], check=True)

# ---- pipeline --------------------------------------------------------------
def render_take(prompt, image_uri, aspect_ratio, seconds, dest):
    data_uris = [image_uri] if image_uri else []
    vid = agnes_create(prompt, data_uris, aspect_ratio, seconds)
    agnes_download(vid, dest)

def post_process(job_id, takes, settings):
    """Montage + voice + music + subtitles + GIF. Returns final mp4 path."""
    jobdir = WORK / job_id
    raw = jobdir / "raw.mp4"
    concat_takes(takes, raw)
    duration = _dur(raw)

    current = raw
    vtext = (settings.get("voice_text") or "").strip()
    if vtext:
        log(job_id, "Génération de la voix française…")
        voice_f = jobdir / "voice.mp3"
        tts_voice(vtext, voice_f, VOICES.get(settings.get("voice"), VOICES["homme"]))
        with_audio = jobdir / "with_audio.mp4"
        build_audio(raw, voice_f, with_audio, MUSIC_TRACKS.get(settings.get("music")), duration)
        current = with_audio
        log(job_id, f"Audio ✓ ({settings.get('voice')} + musique {settings.get('music') or 'off'})")

    if settings.get("subtitles") and vtext:
        log(job_id, "Incrustation des sous-titres…")
        srt = jobdir / "subs.srt"
        srt.write_text(make_srt(vtext, duration))
        final = jobdir / "final.mp4"
        burn_subtitles(current, srt, final)
        current = final

    return current

def save_to_gallery(job_id, mp4_path, title):
    gid = job_id
    mp4_out = GALLERY / f"{gid}.mp4"
    gif_out = GALLERY / f"{gid}.gif"
    try:
        mp4_out.write_bytes(mp4_path.read_bytes())
        log(job_id, "Génération du GIF…")
        make_gif(mp4_path, gif_out)
        meta = {"id": gid, "title": title, "date": time.time(),
                "mp4": f"/videos/{gid}.mp4", "gif": f"/videos/{gid}.gif"}
        (GALLERY / f"{gid}.json").write_text(json.dumps(meta))
        return meta
    except Exception as e:
        log(job_id, f"⚠️ GIF/export partiel : {e}")
        return None

def run_generation(job_id, settings):
    jobdir = WORK / job_id
    jobdir.mkdir(exist_ok=True)
    set_job(job_id, status="generating", progress=5)
    try:
        prompts = settings["prompts"]
        images = settings["images"]
        n_shots = len(prompts)
        seconds = settings["seconds"]
        log(job_id, f"{n_shots} plan(s) x {seconds}s · ratio {settings['aspect_ratio']} · voix {settings['voice']}")
        takes = []
        for i, p in enumerate(prompts):
            img = images[i % len(images)] if images else None
            log(job_id, f"Plan {i+1}/{n_shots} : envoi à Agnes…")
            dest = jobdir / f"take{i+1}.mp4"
            render_take(p, img, settings["aspect_ratio"], seconds, dest)
            takes.append(dest)
            log(job_id, f"Plan {i+1} : rendu ✓")
            set_job(job_id, progress=5 + int(70 * (i + 1) / n_shots))

        set_job(job_id, status="montage", progress=80)
        log(job_id, f"Montage ({n_shots} plans)…")
        final = post_process(job_id, takes, settings)
        log(job_id, "Montage ✓")

        set_job(job_id, status="export", progress=92)
        meta = save_to_gallery(job_id, final, settings.get("title") or "Video")
        log(job_id, "✅ Vidéo terminée")

        # keep state for regeneration
        set_job(job_id, status="done", progress=100, video=str(final),
                takes=[str(t) for t in takes], meta=meta)
    except Exception as e:
        log(job_id, f"❌ ERREUR : {e}")
        set_job(job_id, status="error", error=str(e))

def run_regenerate(job_id, plan_index):
    job = get_job(job_id)
    if not job.get("prompts") or not job.get("takes"):
        set_job(job_id, status="error", error="état du job introuvable pour régénération")
        return
    try:
        set_job(job_id, status="generating", progress=60)
        prompts = job["prompts"]
        images = job.get("images") or []
        takes = [Path(t) for t in job["takes"]]
        i = plan_index
        img = images[i % len(images)] if images else None
        log(job_id, f"↻ Régénération du plan {i+1}/{len(prompts)}…")
        dest = takes[i]
        render_take(prompts[i], img, job.get("aspect_ratio", "9:16"), job.get("seconds", "12"), dest)
        log(job_id, f"Plan {i+1} : nouveau rendu ✓")
        settings = {
            "voice_text": job.get("voice_text"), "voice": job.get("voice", "homme"),
            "music": job.get("music"), "subtitles": job.get("subtitles"),
            "aspect_ratio": job.get("aspect_ratio", "9:16"),
        }
        log(job_id, "Re-montage…")
        final = post_process(job_id, takes, settings)
        set_job(job_id, status="export", progress=92)
        meta = save_to_gallery(job_id, final, job.get("title") or "Video")
        set_job(job_id, status="done", progress=100, video=str(final), meta=meta)
        log(job_id, "✅ Vidéo mise à jour")
    except Exception as e:
        log(job_id, f"❌ ERREUR régénération : {e}")
        set_job(job_id, status="error", error=str(e))

# ---- FIFO queue ------------------------------------------------------------
JOB_QUEUE = queue.Queue()

def queue_worker():
    while True:
        fn = JOB_QUEUE.get()
        try:
            fn()
        except Exception as e:
            print("queue worker:", e)
        JOB_QUEUE.task_done()

threading.Thread(target=queue_worker, daemon=True).start()

# ---- HTTP handler ----------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Auth-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj).encode())

    def _authed(self):
        a = self.headers.get("Authorization", "")
        t = self.headers.get("X-Auth-Token", "")
        if a.startswith("Bearer "):
            t = a[7:]
        return t == TOKEN

    def _qs(self):
        if "?" not in self.path:
            return {}
        return dict(kv.split("=", 1) for kv in self.path.split("?", 1)[1].split("&") if "=" in kv)

    def do_OPTIONS(self):
        self._send(204, b"")

    def do_GET(self):
        path = self.path.split("?")[0]
        qs = self._qs()
        if path == "/api/status":
            self._json(200, get_job(qs.get("job_id", "")))
            return
        if path == "/api/gallery":
            out = []
            for f in sorted(GALLERY.glob("*.json"), reverse=True):
                try:
                    out.append(json.loads(f.read_text()))
                except Exception:
                    pass
            self._json(200, {"videos": out})
            return
        if path == "/api/video":
            if not self._authed():
                self._json(401, {"error": "unauthorized"})
                return
            job = get_job(qs.get("job_id", ""))
            vp = job.get("video")
            if vp and Path(vp).exists():
                self._send(200, Path(vp).read_bytes(), "video/mp4")
            else:
                self._json(404, {"error": "not ready"})
            return
        if path.startswith("/videos/"):
            # permanent share link (public, no token — anyone with the link can fetch)
            name = path.split("/")[-1]
            f = GALLERY / name
            if f.exists() and name.endswith((".mp4", ".gif")):
                ctype = "image/gif" if name.endswith(".gif") else "video/mp4"
                self._send(200, f.read_bytes(), ctype)
            else:
                self._json(404, {"error": "not found"})
            return
        if path in ("/", "/index.html"):
            idx = BASE_DIR / "index.html"
            if idx.exists():
                self._send(200, idx.read_bytes(), "text/html; charset=utf-8")
            else:
                self._json(404, {"error": "no index"})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        if not self._authed():
            self._json(401, {"error": "unauthorized"})
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            p = json.loads(self.rfile.read(n).decode() or "{}")
        except Exception:
            self._json(400, {"error": "bad request"})
            return

        if path == "/api/scenario":
            theme = (p.get("theme") or "").strip()
            if not theme:
                self._json(400, {"error": "theme required"})
                return
            dur = p.get("duration", "60")
            n_shots, _ = DURATIONS.get(dur, DURATIONS["60"])
            try:
                title, prompts = deepseek_scenario(theme, n_shots)
                self._json(200, {"title": title, "prompts": prompts})
            except Exception as e:
                self._json(502, {"error": str(e)})
            return

        if path == "/api/generate":
            images = (p.get("images") or [])[:5]
            prompts = [x.strip() for x in (p.get("prompts") or []) if x.strip()][:10]
            if not prompts:
                self._json(400, {"error": "prompts required"})
                return
            dur = p.get("duration", "60")
            if dur not in DURATIONS:
                dur = "60"
            n_shots, seconds = DURATIONS[dur]
            prompts = prompts[:n_shots]
            ar = p.get("aspect_ratio", "9:16")
            if ar not in ("9:16", "16:9", "1:1"):
                ar = "9:16"
            voice = p.get("voice", "homme")
            if voice not in VOICES:
                voice = "homme"
            music = p.get("music")  # None or a key in MUSIC_TRACKS
            if music and music not in MUSIC_TRACKS:
                music = None
            title = (p.get("title") or "").strip() or "Video"
            settings = {
                "images": images, "prompts": prompts, "title": title,
                "aspect_ratio": ar, "seconds": seconds, "voice": voice,
                "music": music, "subtitles": bool(p.get("subtitles")),
                "voice_text": (p.get("voice_text") or "").strip(),
            }
            job_id = secrets.token_hex(8)
            set_job(job_id, status="queued", progress=0, **{
                k: settings[k] for k in ("prompts", "images", "title", "aspect_ratio",
                                         "seconds", "voice", "music", "subtitles", "voice_text")
            })
            JOB_QUEUE.put(lambda jid=job_id, s=settings: run_generation(jid, s))
            self._json(200, {"job_id": job_id, "queued": JOB_QUEUE.qsize()})
            return

        if path == "/api/regenerate":
            job_id = p.get("job_id", "")
            idx = int(p.get("plan_index", 0))
            job = get_job(job_id)
            if not job:
                self._json(404, {"error": "job not found"})
                return
            JOB_QUEUE.put(lambda: run_regenerate(job_id, idx))
            self._json(200, {"job_id": job_id, "queued": JOB_QUEUE.qsize()})
            return

        self._json(404, {"error": "not found"})

def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Agnes Storyboard v4 on http://{HOST}:{PORT}  "
          f"(agnes={'set' if AGNES_KEY else 'MISSING'}, deepseek={'set' if DEEPSEEK_KEY else 'MISSING'}, "
          f"music={sum(1 for m in MUSIC_TRACKS.values() if m.exists())}/3)")
    srv.serve_forever()

if __name__ == "__main__":
    main()
