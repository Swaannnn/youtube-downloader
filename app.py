import re
import json
import uuid
import shutil
import subprocess
import threading
import sys
from flask import Flask, request, jsonify, send_file
from pathlib import Path

BASE_DIR = Path(__file__).parent
app = Flask(__name__)

DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

jobs = {}

# ── Outils ──────────────────────────────────────────────────────────────────

def find_ytdlp():
    found = shutil.which("yt-dlp")
    if found:
        return [found]
    return [sys.executable, "-m", "yt_dlp"]

YTDLP = find_ytdlp()

def sanitize_filename(name: str) -> str:
    return re.sub(r'[^\w\s\-_\.]', '', name).strip()[:80]

# ── Job de téléchargement ────────────────────────────────────────────────────

PROGRESS_RE = re.compile(
    r"\[download\]\s+([\d\.]+)%"
    r"(?:.*?at\s+([\d\.]+\S+))?"
    r"(?:.*?ETA\s+(\S+))?"
)

def run_download(job_id: str, url: str):
    job = jobs[job_id]
    try:
        # Étape 1 : récupérer les métadonnées
        job["status"] = "fetching_info"
        info_result = subprocess.run(
            YTDLP + ["--dump-json", "--no-playlist", url],
            capture_output=True, text=True, timeout=30
        )
        if info_result.returncode != 0:
            raise RuntimeError("Infos introuvables : " + info_result.stderr[-200:])

        info = json.loads(info_result.stdout)
        title = sanitize_filename(info.get("title", "video"))
        job["title"] = info.get("title", "vidéo")
        out_file = DOWNLOAD_DIR / f"{job_id}_{title}.mp4"

        # Étape 2 : téléchargement
        job.update({"status": "downloading", "progress": 0, "dl_speed": "", "dl_eta": ""})

        proc = subprocess.Popen(
            YTDLP + [
                "--no-playlist",
                "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
                "--merge-output-format", "mp4",
                "--newline",
                "-o", str(out_file),
                url,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        for line in proc.stdout:
            job["last_line"] = line.strip()
            m = PROGRESS_RE.search(line)
            if m:
                job["progress"] = min(99, float(m.group(1)))
                job["dl_speed"] = m.group(2) or ""
                job["dl_eta"]   = m.group(3) or ""

        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError("Erreur yt-dlp : " + job.get("last_line", ""))

        # yt-dlp peut légèrement renommer le fichier
        if not out_file.exists():
            candidates = list(DOWNLOAD_DIR.glob(f"{job_id}*.mp4"))
            if not candidates:
                raise RuntimeError("Fichier introuvable après téléchargement.")
            out_file = candidates[0]

        job.update({"status": "done", "file": str(out_file), "filename": f"{title}.mp4"})

    except Exception as exc:
        job.update({"status": "error", "error": str(exc)})

# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_file(BASE_DIR / "index.html")


@app.route("/api/info", methods=["POST"])
def api_info():
    url = (request.json or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "URL manquante"}), 400
    try:
        result = subprocess.run(
            YTDLP + ["--dump-json", "--no-playlist", url],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return jsonify({"error": "yt-dlp : " + result.stderr[-300:]}), 400

        info = json.loads(result.stdout)
        dur = info.get("duration", 0)
        h, rem = divmod(int(dur), 3600)
        m, s   = divmod(rem, 60)
        return jsonify({
            "title":        info.get("title", ""),
            "channel":      info.get("uploader", ""),
            "duration_str": f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}",
            "thumbnail":    info.get("thumbnail", ""),
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/download", methods=["POST"])
def api_download():
    url = (request.json or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "URL manquante"}), 400

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {"status": "queued", "error": None, "file": None,
                    "progress": 0, "dl_speed": "", "dl_eta": ""}
    threading.Thread(target=run_download, args=(job_id, url), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def api_status(job_id):
    job = jobs.get(job_id)
    return (jsonify(job), 200) if job else (jsonify({"error": "Job introuvable"}), 404)


@app.route("/api/file/<job_id>")
def api_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "Fichier non prêt"}), 404
    return send_file(job["file"], as_attachment=True,
                     download_name=job.get("filename", "video.mp4"))


if __name__ == "__main__":
    app.run(debug=True, port=5000, threaded=True)
