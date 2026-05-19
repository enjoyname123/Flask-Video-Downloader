import os
import threading
import unicodedata
import re
from flask import Flask, request, render_template, send_file, jsonify
import yt_dlp

app = Flask(__name__)
REPO_ROOT = os.path.abspath(os.path.dirname(__file__))
DOWNLOADS_DIR = os.path.join(REPO_ROOT, "downloads") 
COOKIES_PATH = os.path.join(REPO_ROOT, 'cookies.txt')
FALLBACK_COOKIES_PATH = os.path.join(REPO_ROOT, 'fallback_cookies.txt')

progress = {
    'status': 'Waiting...',
    'percent': 0,
    'done': False,
    'error': '',
    'quality_requested': None,
    'quality_used': None,
    'filename': None,
    'mode': 'video'
}

def get_effective_cookies_path(form_path=None):
    if os.path.exists(COOKIES_PATH):
        print(f"--> Using Primary Repository Cookies: {COOKIES_PATH}")
        return COOKIES_PATH
        
    if os.path.exists(FALLBACK_COOKIES_PATH):
        print(f"--> Using Fallback Repository Cookies: {FALLBACK_COOKIES_PATH}")
        return FALLBACK_COOKIES_PATH

    env_path = os.environ.get('YT_DLP_COOKIES_PATH')
    if env_path:
        env_path = os.path.expanduser(env_path)
        if os.path.exists(env_path):
            return env_path

    if form_path and str(form_path).strip():
        form_path = os.path.expanduser(form_path)
        if os.path.exists(form_path):
            return form_path

    return None

progress_lock = threading.Lock()
last_video_filename = None

def safe_filename(filename):
    value = unicodedata.normalize('NFKD', filename).encode('ascii', 'ignore').decode('ascii')
    value = str(re.sub(r'[^A-Za-z0-9_.-]', '_', value))
    return value[:100]

def download_hook(d):
    with progress_lock:
        try:
            if d['status'] == 'downloading':
                percent = 0
                percent_str = d.get('_percent_str') or d.get('percent') or '0.0%'
                if isinstance(percent_str, str):
                    try:
                        percent = float(percent_str.replace('%', '').strip())
                    except ValueError:
                        percent = 0
                elif isinstance(percent_str, (int, float)):
                    percent = float(percent_str)

                total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate')
                downloaded_bytes = d.get('downloaded_bytes')
                if percent == 0 and downloaded_bytes and total_bytes:
                    try:
                        percent = float(downloaded_bytes) / float(total_bytes) * 100
                    except Exception:
                        percent = 0

                progress['percent'] = max(0, min(100, int(percent)))
                speed = d.get('speed')
                eta = d.get('eta')
                parts = [f'Downloading... {progress["percent"]}%']
                if speed:
                    parts.append(f'{speed/1024:.1f} KB/s')
                if eta is not None:
                    parts.append(f'ETA {int(eta)}s')
                progress['status'] = ' · '.join(parts)
                progress['done'] = False
            elif d['status'] == 'finished':
                progress['percent'] = 100
                progress['status'] = 'Processing file...'
                progress['done'] = False
        except Exception as e:
            progress['status'] = f"Error in hook: {e}"
            progress['done'] = True

def download_video(url, quality, download_subs, mode='video', cookies_path=None):
    global last_video_filename
    try:
        effective_cookies = get_effective_cookies_path(cookies_path)

        # Standard, clean options relying completely on cookies for auth
        ydl_opts = {
            'progress_hooks': [download_hook],
            'quiet': False,
            'no_warnings': False,
            'progress_with_newline': False,
            'ignoreerrors': False,
            'cache_dir': os.path.join(REPO_ROOT, ".yt-dlp-cache"),
        }

        if effective_cookies:
            ydl_opts['cookiefile'] = effective_cookies

        if mode == 'audio':
            ydl_opts['outtmpl'] = os.path.join(DOWNLOADS_DIR, '%(title)s.%(ext)s')
            ydl_opts['format'] = 'bestaudio/best'
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]
            download_subs = False  
        else:
            ydl_opts['outtmpl'] = os.path.join(DOWNLOADS_DIR, '%(title)s.%(ext)s')
            ydl_opts['merge_output_format'] = 'mp4'
            
            # Format handling based on resolution request
            if quality and quality.isdigit():
                ydl_opts['format'] = f"bestvideo[height<={quality}]+bestaudio/best[height<={quality}]"
            else:
                ydl_opts['format'] = 'bestvideo+bestaudio/best'

            if download_subs:
                ydl_opts.update({
                    'writesubtitles': True,
                    'writeautomaticsub': True,
                    'subtitleslangs': ['en.*'],
                    'embedsubtitles': True,
                })

        with progress_lock:
            progress['quality_requested'] = quality
            progress['quality_used'] = quality if quality else 'best'
            progress['mode'] = mode
            progress['status'] = 'Extracting and downloading...'

        os.makedirs(DOWNLOADS_DIR, exist_ok=True)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            actual_filename = ydl.prepare_filename(info)
            
            ext = 'mp3' if mode == 'audio' else 'mp4'
            final_filename = os.path.splitext(actual_filename)[0] + f'.{ext}'

            if os.path.exists(final_filename):
                last_video_filename = final_filename
            else:
                title = info.get('title', 'file')
                safe_title = safe_filename(title)
                files = [f for f in os.listdir(DOWNLOADS_DIR) if f.startswith(safe_title) and f.endswith(f'.{ext}')]
                if files:
                    last_video_filename = os.path.join(DOWNLOADS_DIR, files[0])
                else:
                    last_video_filename = None

        with progress_lock:
            progress['status'] = 'Completed!'
            progress['percent'] = 100
            progress['done'] = True
            progress['error'] = ''
            if last_video_filename:
                progress['filename'] = os.path.basename(last_video_filename)
    except Exception as e:
        with progress_lock:
            progress['status'] = "Error"
            progress['done'] = True
            progress['error'] = str(e)
            

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        url = request.form.get("url")
        quality = request.form.get("quality")
        download_subs = request.form.get("subs") is not None
        mode = request.form.get("mode", "video")
        cookies_path = request.form.get("cookies")

        with progress_lock:
            progress['status'] = 'Starting...'
            progress['percent'] = 0
            progress['done'] = False
            progress['error'] = ''
            progress['quality_requested'] = quality
            progress['quality_used'] = None
            progress['filename'] = None
            progress['mode'] = mode

        threading.Thread(target=download_video, args=(url, quality, download_subs, mode, cookies_path), daemon=True).start()
        return render_template("progress.html")

    return render_template("index.html")

@app.route("/progress")
def progress_status():
    with progress_lock:
        return jsonify(progress)

@app.route("/download/<path:filename>")
def download_file(filename):
    target_file = os.path.join(DOWNLOADS_DIR, filename)
    if os.path.exists(target_file):
        return send_file(target_file, as_attachment=True)
    else:
        return f"File '{filename}' not found on the server.", 404

if __name__ == "__main__":
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    if os.path.exists('/data/data/com.termux'):
        os.system("termux-open-url http://127.0.0.1:5000/")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
