import os
import threading
import unicodedata
import re
from flask import Flask, request, render_template, send_file, jsonify
import yt_dlp

app = Flask(__name__)
REPO_ROOT = os.path.abspath(os.path.dirname(__file__))
# Creates a 'downloads' folder right inside your project directory
DOWNLOADS_DIR = os.path.join(REPO_ROOT, "downloads") 

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

progress_lock = threading.Lock()
last_video_filename = None

def safe_filename(filename):
    value = unicodedata.normalize('NFKD', filename).encode('ascii', 'ignore').decode('ascii')
    value = str(re.sub(r'[^A-Za-z0-9_.-]', '_', value))
    return value[:100]  # Limit length for filesystem safety

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

def download_video(url, quality, download_subs, mode='video'):
    global last_video_filename
    try:
        # Client spoofing options designed to emulate systems that do not use web cookies
        probe_opts = {
            'quiet': True,
            'no_warnings': True,
            'extractor_args': {
                'youtube': {
                    'player_client': ['android_tv_embedded', 'web_safari']
                }
            }
        }

        base_opts = {
            'progress_hooks': [download_hook],
            'quiet': False,
            'no_warnings': False,
            'progress_with_newline': False,
            'ignoreerrors': True,
            'remote_components': 'ejs:github',
            'javascript_runtimes': ['node'],
            'extractor_args': {
                'youtube': {
                    'player_client': ['android_tv_embedded', 'web_safari']
                }
            }
        }

        # Set output format based on mode
        if mode == 'audio':
            base_opts['outtmpl'] = os.path.join(DOWNLOADS_DIR, '%(title)s.%(ext)s')
            base_opts['format'] = 'bestaudio/best'
            base_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]
            download_subs = False  # No subtitles for audio
        else:
            base_opts['outtmpl'] = os.path.join(DOWNLOADS_DIR, '%(title)s.%(ext)s')
            base_opts['merge_output_format'] = 'mp4'

        # Record requested quality in progress
        with progress_lock:
            progress['quality_requested'] = quality
            progress['mode'] = mode

        os.makedirs(DOWNLOADS_DIR, exist_ok=True)

        # For audio mode, skip quality selection (not applicable)
        if mode == 'audio':
            with progress_lock:
                progress['quality_used'] = 'Best Available'
                progress['status'] = 'Extracting best audio...'

            with yt_dlp.YoutubeDL(base_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                actual_filename = ydl.prepare_filename(info)
                audio_filename = os.path.splitext(actual_filename)[0] + '.mp3'

                if os.path.exists(audio_filename):
                    last_video_filename = audio_filename
                    with progress_lock:
                        progress['filename'] = os.path.basename(last_video_filename)
                else:
                    # Fallback search
                    title = info.get('title', 'audio')
                    safe_title = safe_filename(title)
                    files = [f for f in os.listdir(DOWNLOADS_DIR) if f.startswith(safe_title) and f.endswith('.mp3')]
                    if files:
                        last_video_filename = os.path.join(DOWNLOADS_DIR, files[0])
                        with progress_lock:
                            progress['filename'] = os.path.basename(last_video_filename)
                    else:
                        last_video_filename = None
        else:
            # Video mode with quality fallback
            requested_int = None
            try:
                requested_int = int(quality) if quality is not None else None
            except Exception:
                requested_int = None

            # Probe available formats
            with yt_dlp.YoutubeDL(probe_opts) as ydl_probe:
                info = ydl_probe.extract_info(url, download=False)

            formats = info.get('formats', []) if info else []
            available_heights = sorted({f.get('height') for f in formats if f.get('height')}, reverse=True)

            chosen_height = None
            if requested_int is not None and available_heights:
                for h in available_heights:
                    if h <= requested_int:
                        chosen_height = h
                        break

            # If nothing <= requested was found, fallback to highest available
            if chosen_height is None and available_heights:
                chosen_height = available_heights[0]

            # Build final format selector
            if chosen_height:
                format_selector = f"bestvideo[height<={chosen_height}]+bestaudio/best[height<={chosen_height}]"
            else:
                format_selector = 'bestvideo+bestaudio/best'

            # Build final ydl options
            ydl_opts = base_opts.copy()
            ydl_opts['format'] = format_selector

            if download_subs:
                ydl_opts.update({
                    'writesubtitles': True,
                    'writeautomaticsub': True,
                    'subtitleslangs': ['en.*'],
                    'embedsubtitles': True,
                })

            # Update progress with selection
            with progress_lock:
                progress['quality_used'] = str(chosen_height) if chosen_height else 'best'
                progress['status'] = f"Selected quality: {progress['quality_used']}p (requested: {quality})"

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)

                # Directly retrieve the true path yt-dlp generated to prevent filename mismatch 404s
                actual_filename = ydl.prepare_filename(info)
                video_filename = os.path.splitext(actual_filename)[0] + '.mp4'

                if os.path.exists(video_filename):
                    last_video_filename = video_filename
                    with progress_lock:
                        progress['filename'] = os.path.basename(last_video_filename)
                else:
                    # Secondary structural string checking pattern if path resolves slightly differently
                    title = info.get('title', 'video')
                    safe_title = safe_filename(title)
                    files = [f for f in os.listdir(DOWNLOADS_DIR) if f.startswith(safe_title) and f.endswith('.mp4')]
                    if files:
                        last_video_filename = os.path.join(DOWNLOADS_DIR, files[0])
                        with progress_lock:
                            progress['filename'] = os.path.basename(last_video_filename)
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
        print("POST request received")  # Debug
        print("Form data:", request.form)

        url = request.form.get("url")
        quality = request.form.get("quality")
        download_subs = request.form.get("subs") is not None
        mode = request.form.get("mode", "video")

        print("URL:", url)
        print("Quality:", quality)
        print("Download Subs:", download_subs)
        print("Mode:", mode)

        # Reset progress tracker context
        with progress_lock:
            progress['status'] = 'Starting...'
            progress['percent'] = 0
            progress['done'] = False
            progress['error'] = ''
            progress['quality_requested'] = quality
            progress['quality_used'] = None
            progress['filename'] = None
            progress['mode'] = mode

        # Fire worker download thread - execution parameters are now fully clean
        threading.Thread(target=download_video, args=(url, quality, download_subs, mode), daemon=True).start()

        return render_template("progress.html")

    return render_template("index.html")

@app.route("/progress")
def progress_status():
    with progress_lock:
        return jsonify(progress)

@app.route("/download/<path:filename>")
def download_file(filename):
    # Securely point to the file directly inside your downloads directory
    target_file = os.path.join(DOWNLOADS_DIR, filename)
    
    if os.path.exists(target_file):
        return send_file(target_file, as_attachment=True)
    else:
        return f"File '{filename}' not found on the server.", 404

if __name__ == "__main__":
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    
    # Only try to open Termux browser if running locally inside Termux
    if os.path.exists('/data/data/com.termux'):
        os.system("termux-open-url http://127.0.0.1:5000/")
    
    # Dynamically bind to the cloud provider's port, or default to 5000 locally
    port = int(os.environ.get("PORT", 5000))
    
    # debug=True can cause threading/hook loops on some production platforms
    app.run(host="0.0.0.0", port=port, debug=False)
