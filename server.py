#!/usr/bin/env python3
"""Agnes Storyboard — web backend (v2).

Generates a ~1-minute AI video from 1-5 uploaded images + a scenario:
  1. scenario (5 shots) written by DeepSeek (editable by the user via /api/scenario)
  2. each shot rendered via Agnes (image-to-video, reference mode)
  3. montage (ffmpeg concat)
  4. French narration (edge-tts) + optional background music
  5. optional burned-in subtitles
Returns the finished MP4 for download.

Run:  python3 server.py
"""
import base64
import json
import os
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
MUSIC = BASE_DIR / "music.mp3"
PORT = int(os.environ.get("APP_PORT", "8647"))
HOST = os.environ.get("APP_HOST", "0.0.0.0")

FFMPEG = "/root/.hermes/tools/ffmpeg-9.0.1-linux-x64/bin/ffmpeg"

AGNES_API = "https://apihub.agnes-ai.com"
AGNES_MODEL = "agnes-video-2.5-flash"
DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
DEEPSEEK_MODEL = "deepseek-v4-pro"
TTS_VOICE = "fr-FR-HenriNeural"
MUSIC_VOLUME = 0.15

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

# ---- helpers ---------------------------------------------------------------
def http_json(url, payload=None, headers=None, method="POST"):
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode())

def deepseek_scenario(theme):
    sys = ("You are a film director. Write a 5-shot storyboard for a 60-second video "
           "(5 shots x 12 seconds each). Output EXACTLY 5 lines, one per shot, each a "
           "vivid English visual prompt for a text-to-video model, keeping the SAME main "
           "character throughout. No numbering, no headings, no extra text.")
    d = http_json(DEEPSEEK_URL,
                  payload={"model": DEEPSEEK_MODEL, "messages": [
                      {"role": "system", "content": sys},
                      {"role": "user", "content": theme},
                  ], "max_tokens": 800},
                  headers={"Authorization": f"Bearer {DEEPSEEK_KEY}"})
    content = ((d.get("choices") or [{}])[0].get("message") or {}).get("content", "")
    prompts = [ln.strip() for ln in content.splitlines() if ln.strip()]
    return prompts[:5] or [theme]

def agnes_create(prompt, data_uris, aspect_ratio):
    payload = {
        "model": AGNES_MODEL, "prompt": prompt, "mode": "reference",
        "images": data_uris, "seconds": "12", "size": "720P",
        "aspect_ratio": aspect_ratio, "n": 1,
    }
    d = http_json(AGNES_API + "/v1/videos", payload,
                  headers={"Authorization": f"Bearer {AGNES_KEY}"})
    return d.get("video_id") or d.get("id")

def agnes_download(video_id, dest):
    for _ in range(50):
        d = http_json(f"{AGNES_API}/agnesapi?video_id={video_id}&model_name={AGNES_MODEL}",
                      headers={"Authorization": f"Bearer {AGNES_KEY}"}, method="GET")
        st = str(d.get("status", "")).lower()
        if st in {"completed", "succeeded", "success", "done"}:
            md = d.get("metadata") or {}
            url = d.get("url") or md.get("url")
            with urllib.request.urlopen(urllib.request.Request(url), timeout=180) as r:
                dest.write_bytes(r.read())
            return True
        if st in {"failed", "error", "cancelled"}:
            return False
        time.sleep(12)
    return False

def tts_voice(text, dest):
    import asyncio
    import edge_tts
    async def go():
        await edge_tts.Communicate(text, TTS_VOICE).save(str(dest))
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

def build_audio(video, voice, out, with_music, duration):
    """Mux video + narration (delayed 1.2s) [+ music loop] into `out`."""
    inputs = ["-i", str(video), "-i", str(voice)]
    fc = "[1:a]adelay=1200|1200[v]"
    if with_music and MUSIC.exists():
        inputs += ["-stream_loop", "-1", "-i", str(MUSIC)]
        fc += f";[2:a]volume={MUSIC_VOLUME}[m];[v][m]amix=inputs=2:duration=first:dropout_transition=0[a]"
        amap = "[a]"
    else:
        fc += ";[v]apad[a]"
        amap = "[a]"
    subprocess.run([FFMPEG, "-y", "-v", "error", *inputs, "-filter_complex", fc,
                    "-map", "0:v", "-map", amap, "-c:v", "copy", "-c:a", "aac",
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

# ---- pipeline (background thread) ------------------------------------------
def run_job(job_id, images, prompts, aspect_ratio, voice_text, with_music, with_subs):
    jobdir = WORK / job_id
    jobdir.mkdir(exist_ok=True)
    try:
        data_uris = [f"data:image/jpeg;base64,{b64}" for b64 in images]
        set_job(job_id, status="generating", progress=10)
        takes = []
        for i, p in enumerate(prompts):
            vid = agnes_create(p, data_uris, aspect_ratio)
            if not vid:
                raise RuntimeError(f"Agnes refused shot {i+1}")
            dest = jobdir / f"take{i+1}.mp4"
            if not agnes_download(vid, dest):
                raise RuntimeError(f"shot {i+1} failed")
            takes.append(dest)
            set_job(job_id, progress=10 + int(70 * (i + 1) / len(prompts)))

        set_job(job_id, status="montage", progress=85)
        raw = jobdir / "raw.mp4"
        concat_takes(takes, raw)
        duration = _dur(raw)

        current = raw
        if voice_text and voice_text.strip():
            set_job(job_id, status="voice", progress=90)
            voice = jobdir / "voice.mp3"
            tts_voice(voice_text.strip(), voice)
            with_audio = jobdir / "with_audio.mp4"
            build_audio(raw, voice, with_audio, with_music, duration)
            current = with_audio

        if with_subs and voice_text and voice_text.strip():
            set_job(job_id, status="subtitles", progress=95)
            srt = jobdir / "subs.srt"
            srt.write_text(make_srt(voice_text.strip(), duration))
            final = jobdir / "final.mp4"
            burn_subtitles(current, srt, final)
            current = final

        set_job(job_id, status="done", progress=100, video=str(current))
    except Exception as e:
        set_job(job_id, status="error", error=str(e))

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
            try:
                prompts = deepseek_scenario(theme)
                self._json(200, {"prompts": prompts})
            except Exception as e:
                self._json(502, {"error": str(e)})
            return

        if path == "/api/generate":
            images = p.get("images") or []
            prompts = p.get("prompts") or []
            voice = (p.get("voice_text") or "").strip()
            ar = (p.get("aspect_ratio") or "9:16")
            if ar not in ("9:16", "16:9", "1:1"):
                ar = "9:16"
            if not images or len(prompts) < 1:
                self._json(400, {"error": "images and prompts required"})
                return
            images = images[:5]
            prompts = [x.strip() for x in prompts if x.strip()][:5]
            job_id = secrets.token_hex(8)
            set_job(job_id, status="queued", progress=0)
            threading.Thread(target=run_job, args=(
                job_id, images, prompts, ar, voice,
                bool(p.get("music")), bool(p.get("subtitles")),
            ), daemon=True).start()
            self._json(200, {"job_id": job_id})
            return

        self._json(404, {"error": "not found"})

def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Agnes Storyboard v2 on http://{HOST}:{PORT}  (agnes={'set' if AGNES_KEY else 'MISSING'}, deepseek={'set' if DEEPSEEK_KEY else 'MISSING'}, music={'ok' if MUSIC.exists() else 'MISSING'})")
    srv.serve_forever()

if __name__ == "__main__":
    main()
