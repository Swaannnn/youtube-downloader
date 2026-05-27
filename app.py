import re
import json
import uuid
import shutil
import zipfile
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

# ── Outils ───────────────────────────────────────────────────────────────────

def find_ytdlp():
    found = shutil.which("yt-dlp")
    if found:
        return [found]
    return [sys.executable, "-m", "yt_dlp"]

YTDLP = find_ytdlp()

def sanitize_filename(name: str) -> str:
    return re.sub(r'[^\w\s\-_\.]', '', name).strip()[:80]

# ── Regex progression yt-dlp
PROGRESS_RE = re.compile(
    r"\[download\]\s+([\d\.]+)%"
    r"(?:.*?at\s+([\d\.]+\S+))?"
    r"(?:.*?ETA\s+(\S+))?"
)

# ── Job vidéo unique ──────────────────────────────────────────────────────────

def run_download(job_id: str, url: str, fmt: str):
    job = jobs[job_id]
    try:
        job["status"] = "fetching_info"
        info_result = subprocess.run(
            YTDLP + ["--dump-json", "--no-playlist", url],
            capture_output=True, text=True, timeout=30
        )
        if info_result.returncode != 0:
            raise RuntimeError("Infos introuvables : " + info_result.stderr[-200:])

        info  = json.loads(info_result.stdout)
        title = sanitize_filename(info.get("title", "video"))
        job["title"] = info.get("title", "vidéo")

        job.update({"status": "downloading", "progress": 0, "dl_speed": "", "dl_eta": ""})

        if fmt == "mp3":
            out_file = DOWNLOAD_DIR / f"{job_id}_{title}.mp3"
            dl_cmd = YTDLP + [
                "--no-playlist", "-f", "bestaudio/best",
                "-x", "--audio-format", "mp3", "--audio-quality", "0",
                "--newline", "-o", str(out_file), url,
            ]
            job["filename"] = f"{title}.mp3"
        else:
            out_file = DOWNLOAD_DIR / f"{job_id}_{title}.mp4"
            dl_cmd = YTDLP + [
                "--no-playlist",
                "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
                "--merge-output-format", "mp4",
                "--newline", "-o", str(out_file), url,
            ]
            job["filename"] = f"{title}.mp4"

        proc = subprocess.Popen(
            dl_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
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

        ext = "mp3" if fmt == "mp3" else "mp4"
        if not out_file.exists():
            candidates = list(DOWNLOAD_DIR.glob(f"{job_id}*.{ext}"))
            if not candidates:
                raise RuntimeError("Fichier introuvable après téléchargement.")
            out_file = candidates[0]

        job.update({"status": "done", "file": str(out_file)})

    except Exception as exc:
        job.update({"status": "error", "error": str(exc)})


# ── Job playlist (téléchargement séquentiel) ──────────────────────────────────

def run_playlist_download(job_id: str, fmt: str, video_indices: list):
    job = jobs[job_id]
    ext = "mp3" if fmt == "mp3" else "mp4"

    try:
        total     = len(video_indices)
        completed = 0
        files     = []

        for idx, vi in enumerate(video_indices):
            # Vérifie l'annulation entre chaque vidéo
            if job.get("cancel_requested"):
                break

            video = job["videos"][vi]
            video["status"]  = "downloading"
            video["progress"] = 0
            job["current_index"] = idx
            job["current_title"] = video["title"]
            job["status"]        = "downloading"

            title    = sanitize_filename(video["title"])
            out_file = DOWNLOAD_DIR / f"{job_id}_{vi}_{title}.{ext}"

            if fmt == "mp3":
                dl_cmd = YTDLP + [
                    "--no-playlist", "-f", "bestaudio/best",
                    "-x", "--audio-format", "mp3", "--audio-quality", "0",
                    "--newline", "-o", str(out_file), video["url"],
                ]
            else:
                dl_cmd = YTDLP + [
                    "--no-playlist",
                    "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
                    "--merge-output-format", "mp4",
                    "--newline", "-o", str(out_file), video["url"],
                ]

            proc = subprocess.Popen(
                dl_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            # Stocke le process pour pouvoir l'annuler
            job["_current_proc"] = proc

            for line in proc.stdout:
                job["last_line"] = line.strip()
                m = PROGRESS_RE.search(line)
                if m:
                    p = min(99, float(m.group(1)))
                    video["progress"] = p
                    job["dl_speed"]   = m.group(2) or ""
                    job["dl_eta"]     = m.group(3) or ""
                    job["progress"]   = round((completed + p / 100) / total * 100, 1)
                # Annulation en cours de téléchargement
                if job.get("cancel_requested"):
                    proc.terminate()
                    break

            proc.wait()
            job["_current_proc"] = None

            if job.get("cancel_requested"):
                video["status"] = "cancelled"
                break

            if proc.returncode != 0:
                video["status"] = "error"
                video["error"]  = job.get("last_line", "")
            else:
                if not out_file.exists():
                    candidates = list(DOWNLOAD_DIR.glob(f"{job_id}_{vi}_*.{ext}"))
                    if candidates:
                        out_file = candidates[0]

                video["status"]   = "done"
                video["progress"] = 100
                video["file"]     = str(out_file)
                video["filename"] = f"{title}.{ext}"
                files.append(str(out_file))
                completed += 1

            # ← FIX : mise à jour en temps réel du compteur
            job["completed"] = completed
            job["progress"]  = round(completed / total * 100, 1)

        job["completed"] = completed
        job["files"]     = files
        job["total"]     = total

        if job.get("cancel_requested"):
            job["status"] = "cancelled"
        else:
            job["status"] = "done"

    except Exception as exc:
        job.update({"status": "error", "error": str(exc)})


# ── Routes ────────────────────────────────────────────────────────────────────

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
        dur  = info.get("duration", 0)
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


@app.route("/api/playlist-info", methods=["POST"])
def api_playlist_info():
    url = (request.json or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "URL manquante"}), 400
    try:
        result = subprocess.run(
            YTDLP + ["--flat-playlist", "--dump-json", "--yes-playlist", url],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            return jsonify({"error": "yt-dlp : " + result.stderr[-300:]}), 400

        videos = []
        playlist_title = "Playlist"
        for line in result.stdout.strip().splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                if not playlist_title or playlist_title == "Playlist":
                    playlist_title = item.get("playlist_title") or item.get("playlist") or "Playlist"
                dur = item.get("duration") or 0
                h, rem = divmod(int(dur), 3600)
                m, s   = divmod(rem, 60)
                dur_str = f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
                video_url = item.get("url") or item.get("webpage_url") or ""
                if not video_url.startswith("http"):
                    video_url = "https://www.youtube.com/watch?v=" + video_url
                videos.append({
                    "title":     item.get("title", "Sans titre"),
                    "url":       video_url,
                    "thumbnail": item.get("thumbnail", ""),
                    "duration":  dur_str,
                    "uploader":  item.get("uploader", item.get("channel", "")),
                })
            except Exception:
                continue

        if not videos:
            return jsonify({"error": "Aucune vidéo trouvée dans la playlist."}), 400

        return jsonify({
            "playlist_title": playlist_title,
            "videos": videos,
            "count": len(videos),
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/download", methods=["POST"])
def api_download():
    body = request.json or {}
    url  = body.get("url", "").strip()
    fmt  = body.get("format", "mp4").strip().lower()
    if fmt not in ("mp4", "mp3"):
        fmt = "mp4"
    if not url:
        return jsonify({"error": "URL manquante"}), 400

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {
        "status": "queued", "error": None, "file": None,
        "progress": 0, "dl_speed": "", "dl_eta": "", "filename": ""
    }
    threading.Thread(target=run_download, args=(job_id, url, fmt), daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/playlist-download", methods=["POST"])
def api_playlist_download():
    body   = request.json or {}
    fmt    = body.get("format", "mp4").strip().lower()
    videos = body.get("videos", [])
    if fmt not in ("mp4", "mp3"):
        fmt = "mp4"
    if not videos:
        return jsonify({"error": "Aucune vidéo sélectionnée"}), 400

    job_id = str(uuid.uuid4())[:8]
    video_list = [
        {
            "title":     v.get("title", ""),
            "url":       v.get("url", ""),
            "thumbnail": v.get("thumbnail", ""),
            "duration":  v.get("duration", ""),
            "status":    "queued",
            "progress":  0,
            "file":      None,
            "filename":  None,
            "error":     None,
        }
        for v in videos
    ]
    jobs[job_id] = {
        "status":           "queued",
        "type":             "playlist",
        "videos":           video_list,
        "progress":         0,
        "current_index":    -1,
        "current_title":    "",
        "dl_speed":         "",
        "dl_eta":           "",
        "completed":        0,
        "total":            len(video_list),
        "files":            [],
        "error":            None,
        "cancel_requested": False,
        "_current_proc":    None,
    }
    indices = list(range(len(video_list)))
    threading.Thread(
        target=run_playlist_download, args=(job_id, fmt, indices), daemon=True
    ).start()
    return jsonify({"job_id": job_id})


@app.route("/api/cancel/<job_id>", methods=["POST"])
def api_cancel(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job introuvable"}), 404
    job["cancel_requested"] = True
    proc = job.get("_current_proc")
    if proc:
        try:
            proc.terminate()
        except Exception:
            pass
    return jsonify({"ok": True})


@app.route("/api/status/<job_id>")
def api_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job introuvable"}), 404
    # On ne sérialise pas le process subprocess
    safe = {k: v for k, v in job.items() if not k.startswith("_")}
    return jsonify(safe), 200


@app.route("/api/file/<job_id>")
def api_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "Fichier non prêt"}), 404
    return send_file(job["file"], as_attachment=True,
                     download_name=job.get("filename", "video.mp4"))


@app.route("/api/file/<job_id>/all")
def api_file_playlist_all(job_id):
    """Crée un ZIP de toutes les vidéos terminées et le renvoie."""
    job = jobs.get(job_id)
    if not job or job.get("type") != "playlist":
        return jsonify({"error": "Job introuvable"}), 404

    done_videos = [v for v in job.get("videos", []) if v["status"] == "done" and v["file"]]
    if not done_videos:
        return jsonify({"error": "Aucun fichier disponible"}), 404

    zip_path = DOWNLOAD_DIR / f"{job_id}_playlist.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
        for v in done_videos:
            p = Path(v["file"])
            if p.exists():
                zf.write(p, v.get("filename") or p.name)

    return send_file(str(zip_path), as_attachment=True,
                     download_name="playlist.zip")


if __name__ == "__main__":
    app.run(debug=True, port=5000, threaded=True)
