import os
import sys
import uuid
import glob
import json
import re
import subprocess
import threading
import platform
import time
import shutil
import tempfile
from datetime import datetime
from flask import Flask, request, jsonify, send_file, render_template, Response

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # 2MB max payload

DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(__file__))
if not os.path.exists(DATA_DIR):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception:
        DATA_DIR = os.path.dirname(__file__)

HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
COOKIE_FILE = os.path.join(DATA_DIR, "cookies.txt")
jobs = {}
history_lock = threading.Lock()


def is_safe_timestamp(t_val):
    if not t_val or not isinstance(t_val, str):
        return False
    s = t_val.strip()
    return bool(re.match(r'^\d+(\.\d+)?$', s) or re.match(r'^\d{1,2}:\d{2}(:\d{2}(\.\d+)?)?$', s))


def is_valid_media_url(url):
    if not url or not isinstance(url, str):
        return False
    u = url.strip()
    return u.startswith("http://") or u.startswith("https://")


def is_safe_system_path(path):
    if not path or not isinstance(path, str):
        return False
    try:
        norm = os.path.abspath(os.path.normpath(path.strip()))
    except Exception:
        return False

    norm_lower = norm.lower()
    sys_name = platform.system().lower()
    if "windows" in sys_name:
        win_dir = os.environ.get("WINDIR", "C:\\Windows").lower()
        prog_files = os.environ.get("ProgramFiles", "C:\\Program Files").lower()
        prog_files_x86 = os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)").lower()
        if norm_lower == win_dir or norm_lower.startswith(win_dir + "\\"):
            return False
        if norm_lower == prog_files or norm_lower.startswith(prog_files + "\\"):
            return False
        if norm_lower == prog_files_x86 or norm_lower.startswith(prog_files_x86 + "\\"):
            return False
        parts = [p.lower() for p in norm.split(os.sep)]
        if any(seg in parts for seg in ("system volume information", "$recycle.bin", "recovery")):
            return False
    else:
        for forbidden in ("/etc", "/boot", "/dev", "/proc", "/sys", "/bin", "/sbin", "/usr/bin", "/usr/sbin"):
            if norm_lower == forbidden or norm_lower.startswith(forbidden + "/"):
                return False
    return True


@app.before_request
def csrf_protect():
    if request.method in ("POST", "DELETE", "PUT"):
        origin = request.headers.get("Origin") or request.headers.get("Referer")
        if origin:
            import urllib.parse
            parsed = urllib.parse.urlparse(origin)
            req_host = request.host.split(":")[0].lower()
            orig_host = parsed.hostname.lower() if parsed.hostname else ""
            if orig_host and orig_host not in (req_host, "localhost", "127.0.0.1", "::1"):
                return jsonify({"error": "Cross-origin request forbidden"}), 403


def load_history():
    with history_lock:
        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return []
        return []


def save_history(history_list):
    with history_lock:
        try:
            temp_file = f"{HISTORY_FILE}.tmp_{uuid.uuid4().hex[:6]}"
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(history_list, f, indent=2, ensure_ascii=False)
            os.replace(temp_file, HISTORY_FILE)
        except Exception:
            try:
                if 'temp_file' in locals() and os.path.exists(temp_file):
                    os.remove(temp_file)
            except Exception:
                pass


def add_history_entry(entry):
    with history_lock:
        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                    history = json.load(f)
            except Exception:
                history = []
        else:
            history = []
        # Prepend new item
        history.insert(0, entry)
        # Retain latest 200 items
        history = history[:200]
        try:
            temp_file = f"{HISTORY_FILE}.tmp_{uuid.uuid4().hex[:6]}"
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(history, f, indent=2, ensure_ascii=False)
            os.replace(temp_file, HISTORY_FILE)
        except Exception:
            try:
                if 'temp_file' in locals() and os.path.exists(temp_file):
                    os.remove(temp_file)
            except Exception:
                pass



def is_android():
    """True when running under Termux / on an Android userland (platform.system() reports 'Linux' there)."""
    if sys.platform == "win32" or sys.platform == "darwin":
        return False
    prefix = os.environ.get("PREFIX", "")
    return (
        "com.termux" in prefix
        or "com.termux" in os.path.expanduser("~")
        or bool(os.environ.get("TERMUX_VERSION"))
        or (os.environ.get("ANDROID_ROOT") is not None and os.path.isdir("/system"))
    )


ANDROID_SHARED_ROOT = "/storage/emulated/0"


def android_media_scan(path):
    """Tell Android's media scanner about a new file so it shows up in the file manager's
    Videos / Audio / Images sections and in Gallery. Files written from Termux are otherwise
    invisible to MediaStore. Uses Termux's built-in `am` (no extra app). Silent best-effort."""
    if not path or not is_android() or not shutil.which("am"):
        return
    try:
        from urllib.parse import quote
        uri = "file://" + quote(os.path.abspath(path))
        subprocess.run(
            ["am", "broadcast", "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE", "-d", uri],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass


def get_android_download_dir():
    """Best writable Downloads/ReClip location on Android (shared storage if Termux has access to it)."""
    home = os.path.expanduser("~")
    for base in ("/storage/emulated/0/Download", os.path.join(home, "storage", "downloads")):
        if os.path.isdir(base) and os.access(base, os.W_OK):
            return os.path.join(base, "ReClip")
    return os.path.join(home, "Downloads", "ReClip")


def get_system_default_folder():
    home = os.path.expanduser("~")
    if is_android():
        return get_android_download_dir()
    return os.path.join(home, "Downloads", "ReClip")


def get_effective_download_dir(output_folder=None):
    if output_folder and str(output_folder).strip():
        try:
            # Expand "~" and ignore paths that aren't absolute on this server's OS
            # (e.g. a Windows "C:\\..." placeholder sent to a Linux/Android server).
            chosen = os.path.expanduser(str(output_folder).strip())
            # Android: the UI hides the "/storage/emulated/0/" prefix, so a path like
            # "Download/ReClip" (typed or displayed) is completed here automatically.
            if is_android() and chosen and not os.path.isabs(chosen) and not re.match(r"^[A-Za-z]:[\\/]", chosen):
                chosen = os.path.join(ANDROID_SHARED_ROOT, chosen.strip("/\\"))
            if os.path.isabs(chosen):
                chosen = os.path.abspath(chosen)
                if is_safe_system_path(chosen):
                    return chosen
        except Exception:
            pass
    return get_system_default_folder()


def remove_file_with_retries(path, max_attempts=5, delay=0.2):
    for attempt in range(max_attempts):
        try:
            if os.path.isfile(path) or os.path.islink(path):
                os.remove(path)
                return True
            elif os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
                return True
        except (PermissionError, OSError):
            time.sleep(delay)
    return False


def cleanup_job_temp_files(job_id, target_dir, keep_file=None, base_folder=None):
    if not target_dir or not os.path.exists(target_dir):
        return
    prefix = f"{job_id}"
    keep_abs = os.path.abspath(keep_file) if keep_file else None

    try:
        if os.path.isdir(target_dir):
            for entry in os.listdir(target_dir):
                if entry.startswith(prefix):
                    full_path = os.path.join(target_dir, entry)
                    if keep_abs and os.path.abspath(full_path) == keep_abs:
                        continue
                    remove_file_with_retries(full_path)
    except Exception as e:
        print(f"Error during cleanup for job {job_id}: {e}")

    # If a channel subfolder was created and is now empty (e.g. cancelled / error), remove it
    if base_folder and target_dir != base_folder:
        try:
            abs_target = os.path.abspath(target_dir)
            abs_base = os.path.abspath(base_folder)
            if abs_target.startswith(abs_base) and abs_target != abs_base:
                if os.path.isdir(abs_target) and not os.listdir(abs_target):
                    os.rmdir(abs_target)
        except Exception:
            pass





def detect_firefox_profile():
    try:
        if sys.platform == "win32":
            appdata = os.environ.get("APPDATA", "")
            ff_profiles = os.path.join(appdata, "Mozilla", "Firefox", "Profiles")
            return os.path.isdir(ff_profiles)
        elif sys.platform == "darwin":
            ff_profiles = os.path.expanduser("~/Library/Application Support/Firefox/Profiles")
            return os.path.isdir(ff_profiles)
        else:
            ff_profiles = os.path.expanduser("~/.mozilla/firefox")
            return os.path.isdir(ff_profiles)
    except Exception:
        return False


def get_ytdlp_bot_bypass_args(cookies_source="auto", use_client_spoof=True):
    """Generate yt-dlp arguments for Method 1 (client spoofing) and Method 2 (cookies)."""
    args = ["--js-runtimes", "node"]

    cookie_args = []
    # Method 2: Cookies
    # Priority 1: cookies.txt file if present in project root
    if os.path.exists(COOKIE_FILE) and os.path.getsize(COOKIE_FILE) > 0:
        cookie_args.extend(["--cookies", COOKIE_FILE])
    # Priority 2: Browser cookies
    elif cookies_source and str(cookies_source).lower() not in ("none", "disabled", "false"):
        c_src = str(cookies_source).lower()
        if c_src == "auto":
            if detect_firefox_profile():
                cookie_args.extend(["--cookies-from-browser", "firefox"])
        elif c_src in ("firefox", "chrome", "edge", "brave", "opera", "vivaldi", "safari"):
            cookie_args.extend(["--cookies-from-browser", c_src])

    # If cookies are active, prioritize them WITHOUT player_client=android.
    # YouTube has enabled SABR streaming on its Android client, which strips all formats
    # higher than 360p (format 18). Active cookies authenticate the standard web client
    # directly and unlock all formats (1080p, 1440p, 4K, 720p, 480p, etc.).
    if cookie_args:
        args.extend(cookie_args)
        return args

    # Method 1: Client spoofing fallback when no cookies are available
    # On Android/Termux the android client is what caps YouTube at 360p (SABR), so use
    # yt-dlp's default clients there (needs nodejs + yt-dlp-ejs, see the Setup page).
    if use_client_spoof and not is_android():
        args.extend(["--extractor-args", "youtube:player_client=android,web"])

    return args


# ---------------------------------------------------------------------------
# Automatic YouTube access handling (age-restricted / "confirm you're not a bot")
# Nothing here needs any UI: the first attempt uses the normal arguments; if YouTube
# blocks it, other ways in are tried automatically and the one that works is
# remembered for that URL so the size check / download use it too.
# ---------------------------------------------------------------------------
_BYPASS_BY_URL = {}
_BLOCK_MARKERS = (
    "not a bot", "sign in", "confirm your age", "age-restricted", "age restricted",
    "inappropriate for some", "login required", "429", "too many requests",
)


def is_blocked_error(err_text):
    e = (err_text or "").lower()
    return any(m in e for m in _BLOCK_MARKERS)


def is_age_error(err_text):
    e = (err_text or "").lower()
    return any(m in e for m in ("confirm your age", "age-restricted", "age restricted", "inappropriate for some"))


def installed_browsers():
    """Browsers that have a profile on this machine (empty on servers, Docker and Termux)."""
    home = os.path.expanduser("~")
    local = os.environ.get("LOCALAPPDATA", "")
    roaming = os.environ.get("APPDATA", "")
    if sys.platform == "win32":
        paths = {
            "firefox": os.path.join(roaming, "Mozilla", "Firefox", "Profiles"),
            "chrome": os.path.join(local, "Google", "Chrome", "User Data"),
            "edge": os.path.join(local, "Microsoft", "Edge", "User Data"),
            "brave": os.path.join(local, "BraveSoftware", "Brave-Browser", "User Data"),
            "opera": os.path.join(roaming, "Opera Software", "Opera Stable"),
            "vivaldi": os.path.join(local, "Vivaldi", "User Data"),
        }
    elif sys.platform == "darwin":
        sup = os.path.join(home, "Library", "Application Support")
        paths = {
            "firefox": os.path.join(sup, "Firefox", "Profiles"),
            "chrome": os.path.join(sup, "Google", "Chrome"),
            "edge": os.path.join(sup, "Microsoft Edge"),
            "brave": os.path.join(sup, "BraveSoftware", "Brave-Browser"),
            "opera": os.path.join(sup, "com.operasoftware.Opera"),
            "vivaldi": os.path.join(sup, "Vivaldi"),
            "safari": os.path.join(home, "Library", "Cookies"),
        }
    else:
        cfg = os.path.join(home, ".config")
        paths = {
            "firefox": os.path.join(home, ".mozilla", "firefox"),
            "chrome": os.path.join(cfg, "google-chrome"),
            "chromium": os.path.join(cfg, "chromium"),
            "edge": os.path.join(cfg, "microsoft-edge"),
            "brave": os.path.join(cfg, "BraveSoftware", "Brave-Browser"),
            "opera": os.path.join(cfg, "opera"),
            "vivaldi": os.path.join(cfg, "vivaldi"),
        }
    return [name for name, path in paths.items() if path and os.path.isdir(path)]


def get_bypass_args_for(url, cookies_source="auto", use_client_spoof=True):
    """Arguments for this URL: whatever worked last time, else the normal defaults."""
    learned = _BYPASS_BY_URL.get((url or "").strip())
    if learned:
        return list(learned)
    return get_ytdlp_bot_bypass_args(cookies_source, use_client_spoof)


def _remember_bypass(url, args):
    if len(_BYPASS_BY_URL) > 200:
        _BYPASS_BY_URL.pop(next(iter(_BYPASS_BY_URL)))
    _BYPASS_BY_URL[(url or "").strip()] = list(args)


def _fallback_attempts(tried_args):
    """Other ways to get past a YouTube block, most reliable first."""
    attempts = []
    base = ["--js-runtimes", "node"]
    if os.path.exists(COOKIE_FILE) and os.path.getsize(COOKIE_FILE) > 0:
        attempts.append(base + ["--cookies", COOKIE_FILE])
    for browser in installed_browsers():
        attempts.append(base + ["--cookies-from-browser", browser])
    # No sign-in needed for many age-gated videos when using these player clients
    attempts.append(base + ["--extractor-args", "youtube:player_client=tv,web_embedded"])
    attempts.append(base + ["--extractor-args", "youtube:player_client=mweb"])
    attempts.append(base + ["--extractor-args", "youtube:player_client=android,web"])
    return [a for a in attempts if a != tried_args]


def run_ytdlp_with_fallbacks(prefix, tail, url, cookies_source="auto", use_client_spoof=True, timeout=60):
    """Run yt-dlp; if YouTube blocks it, quietly retry other ways. Returns the CompletedProcess."""
    first_args = get_bypass_args_for(url, cookies_source, use_client_spoof)
    result = subprocess.run(prefix + first_args + tail, capture_output=True, text=True,
                            timeout=timeout, encoding="utf-8", errors="replace")
    if result.returncode == 0 or not is_blocked_error(result.stderr):
        return result

    deadline = time.time() + 120
    for args in _fallback_attempts(first_args):
        if time.time() > deadline:
            break
        try:
            res = subprocess.run(prefix + args + tail, capture_output=True, text=True,
                                 timeout=timeout, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            continue
        if res.returncode == 0 and res.stdout.strip():
            _remember_bypass(url, args)
            return res
    return result


def friendly_block_message(err_text):
    last = (err_text or "").strip().split("\n")[-1] if (err_text or "").strip() else "Download failed"
    if is_age_error(err_text):
        return ("This video is age-restricted and needs a signed-in YouTube session. "
                "Sign in to YouTube in Firefox or Chrome on this device, or save a cookies.txt file at "
                f"{COOKIE_FILE} and try again.")
    if is_blocked_error(err_text):
        return ("YouTube is asking to confirm you're not a bot. Sign in to YouTube in Firefox or Chrome on this "
                f"device, or save a cookies.txt file at {COOKIE_FILE}, then try again. ({last})")
    return last



def parse_ytdlp_json(stdout):
    """Parse yt-dlp JSON output (-j or -J). Skip warnings and handle both formats."""
    cleaned = (stdout or "").strip()
    if not cleaned:
        raise ValueError("yt-dlp returned empty output")

    # Try direct parse first
    try:
        return json.loads(cleaned)
    except Exception:
        pass

    # Try substring from first '{' to last '}' (handles multi-line JSON with prefixed warnings)
    brace_start = cleaned.find("{")
    brace_end = cleaned.rfind("}")
    if brace_start != -1 and brace_end > brace_start:
        try:
            return json.loads(cleaned[brace_start:brace_end + 1])
        except Exception:
            pass

    # Try line by line (handles -j single-line output with interleaved warning lines)
    for line in cleaned.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            return json.loads(line)
        except Exception:
            continue

    raise ValueError("yt-dlp returned no valid data")


def estimate_format_size(f, duration=0):
    if not f:
        return 0
    if f.get("filesize"):
        return f["filesize"]
    if f.get("filesize_approx"):
        return f["filesize_approx"]
    if f.get("url"):
        clen_match = re.search(r"clen[/=](\d+)|clen%3D(\d+)", f.get("url", ""))
        if clen_match:
            clen_val = clen_match.group(1) or clen_match.group(2)
            if clen_val:
                try:
                    return int(clen_val)
                except ValueError:
                    pass
    # Bitrate estimation: tbr, vbr, or abr in kbps (1 kbit/s = 1000 bits/s = 125 bytes/s)
    br = f.get("tbr") or f.get("vbr") or f.get("abr") or 0
    if br and duration:
        return int(br * 1000 / 8 * duration)
    return 0


def format_size(bytes_val):
    if not bytes_val or bytes_val <= 0:
        return "--"
    mb = bytes_val / (1024 * 1024)
    if mb >= 1024:
        return f"{mb / 1024:.2f} GB"
    return f"{mb:.2f} MB"


def probe_video_height(file_path):
    if not file_path or not os.path.exists(file_path):
        return None
    try:
        abs_path = os.path.abspath(file_path)
        cmd = [
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=height",
            "-of", "json",
            "--",
            abs_path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if res.returncode == 0 and res.stdout:
            data = json.loads(res.stdout)
            streams = data.get("streams", [])
            if streams and streams[0].get("height"):
                return int(streams[0]["height"])
    except Exception:
        pass
    return None


def probe_media_duration(file_path):
    if not file_path or not os.path.exists(file_path):
        return None
    try:
        abs_path = os.path.abspath(file_path)
        cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json",
            "--",
            abs_path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if res.returncode == 0 and res.stdout:
            data = json.loads(res.stdout)
            dur = data.get("format", {}).get("duration")
            if dur:
                return float(dur)
    except Exception:
        pass
    return None


def run_download(job_id, url, format_choice, format_id, output_folder=None, create_channel_folder=True, channel="", trim_start="", trim_end="", embed_metadata=True, embed_subs=False, sub_lang="en", quality="", trim_method="full", cookies_source="auto", use_client_spoof=True):
    job = jobs[job_id]

    base_folder = get_effective_download_dir(output_folder)
    target_dir = base_folder

    if create_channel_folder and channel and channel.strip():
        safe_channel = "".join(c for c in channel.strip() if c not in r'\/:*?"<>|').strip() or "Unknown Channel"
        target_dir = os.path.join(target_dir, safe_channel)

    os.makedirs(target_dir, exist_ok=True)
    job["saved_dir"] = target_dir
    out_template = os.path.join(target_dir, f"{job_id}.%(ext)s")

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--newline",
        "--geo-bypass",
        "--retries", "10",
        "--fragment-retries", "10",
    ] + get_bypass_args_for(url, cookies_source, use_client_spoof) + [
        "-o", out_template
    ]

    def parse_time_str(t_str):
        try:
            s = str(t_str).strip()
            if not s:
                return 0.0
            parts = s.split(":")
            if len(parts) == 3:
                return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
            elif len(parts) == 2:
                return float(parts[0]) * 60 + float(parts[1])
            return float(parts[0])
        except Exception:
            return 0.0

    slice_start_sec = parse_time_str(trim_start) if trim_start else 0.0
    slice_end_sec = parse_time_str(trim_end) if trim_end else 0.0
    slice_duration = max(0.0, slice_end_sec - slice_start_sec) if (slice_end_sec > slice_start_sec) else 0.0

    # Direct slice download without full video download
    is_direct_slice = (trim_method == "direct" and (trim_start or trim_end))

    target_height = 1080
    if quality:
        m = re.search(r"(\d+)p", quality, re.IGNORECASE)
        if m:
            target_height = int(m.group(1))

    if format_choice == "audio":
        cmd += ["-x", "--audio-format", "mp3"]
        if embed_metadata:
            cmd += ["--embed-metadata", "--embed-thumbnail", "--convert-thumbnails", "jpg"]
            clean_artist = (channel or "").replace(":", " - ").strip()
            clean_title = (job.get("title") or "").replace(":", " - ").strip()
            if clean_artist:
                cmd += [
                    "--parse-metadata", f"{clean_artist}:%(artist)s",
                    "--parse-metadata", f"{clean_artist}:%(uploader)s",
                ]
            else:
                cmd += [
                    "--parse-metadata", "%(uploader)s:%(artist)s",
                ]
            if clean_title:
                cmd += [
                    "--parse-metadata", f"{clean_title}:%(title)s",
                ]
    elif is_direct_slice:
        # Prefer seekable segmented m3u8 formats matching requested resolution to prevent hanging and preserve HD quality
        fmt_spec = f"bestvideo[protocol^=m3u8][height<={target_height}]+bestaudio/bestvideo[height<={target_height}]+bestaudio/best"
        cmd += ["-f", fmt_spec, "--merge-output-format", "mp4"]
        if embed_subs:
            lang_pattern = f"{sub_lang}.*" if sub_lang and sub_lang != "all" else "all"
            cmd += ["--embed-subs", "--write-auto-subs", "--sub-langs", lang_pattern]
    elif format_id:
        cmd += ["-f", f"{format_id}+bestaudio/best", "--merge-output-format", "mp4"]
        if embed_subs:
            lang_pattern = f"{sub_lang}.*" if sub_lang and sub_lang != "all" else "all"
            cmd += ["--embed-subs", "--write-auto-subs", "--sub-langs", lang_pattern]
    else:
        cmd += ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"]
        if embed_subs:
            lang_pattern = f"{sub_lang}.*" if sub_lang and sub_lang != "all" else "all"
            cmd += ["--embed-subs", "--write-auto-subs", "--sub-langs", lang_pattern]

    if is_direct_slice:
        s_val = trim_start.strip() if (trim_start and is_safe_timestamp(trim_start)) else "0"
        e_val = trim_end.strip() if (trim_end and is_safe_timestamp(trim_end)) else "inf"
        cmd += [
            "--force-keyframes-at-cuts",
            "--download-sections", f"*{s_val}-{e_val}"
        ]

    cmd.extend(["--", url])

    # --- Progress model -------------------------------------------------
    # Overall percent is monotonic. Download phase maps to 0-88 (video ~85% / audio ~15%
    # when two streams are merged), then post-processing steps move it 90 -> 98.
    _DL_MAX = 88.0
    _NON_MEDIA_EXTS = {".vtt", ".srt", ".ass", ".ttml", ".srv1", ".srv2", ".srv3", ".json3",
                       ".lrc", ".jpg", ".jpeg", ".png", ".webp", ".json", ".description"}
    prog = {"pct": 0.0, "frac": 0.0, "seen": 0, "streams": 2 if format_choice != "audio" else 1,
            "media": True, "post": False}

    def _overall():
        weights = [0.85, 0.15] if prog["streams"] >= 2 else [1.0]
        idx = min(max(prog["seen"] - 1, 0), len(weights) - 1)
        return (sum(weights[:idx]) + weights[idx] * prog["frac"]) * _DL_MAX

    def _set_progress(pct, stage, speed="", eta="", size=""):
        pct = max(prog["pct"], min(98.0, round(pct, 1)))
        prog["pct"] = pct
        job["progress"] = {
            "percent": pct,
            "speed": speed,
            "eta": eta,
            "total_size": size,
            "stage": stage,
        }

    job["progress"] = {
        "percent": 0.0,
        "speed": "",
        "eta": "",
        "stage": "Direct slicing..." if is_direct_slice else "Starting...",
    }

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
        )
        job["proc"] = proc
        job["saved_dir"] = target_dir
        last_lines = []
        for raw_line in iter(proc.stdout.readline, ""):
            if job.get("cancelled"):
                break
            line = raw_line.strip()
            if not line:
                continue
            last_lines.append(line)
            if len(last_lines) > 20:
                last_lines.pop(0)

            # ---- Unified, monotonic progress (never goes backwards) ----
            # New media stream (video / audio) starting
            if "[download] Destination:" in line:
                dest = line.split("Destination:", 1)[1].strip()
                dext = os.path.splitext(dest)[1].lower()
                if dext in _NON_MEDIA_EXTS:
                    prog["media"] = False  # subtitles / thumbnails: ignore their 0-100%
                else:
                    prog["media"] = True
                    prog["seen"] += 1
                    prog["frac"] = 0.0
                    if prog["seen"] == 1:
                        # "<job>.f137.mp4" style name => separate video+audio streams will be merged
                        prog["streams"] = 2 if re.search(r"\.f[\w-]+\.[A-Za-z0-9]+$", dest) else 1
                    _set_progress(_overall(), "Direct slicing..." if is_direct_slice else "Downloading")
            # Direct slice (FFmpeg) progress output
            elif is_direct_slice and not prog["post"] and prog["media"] and ("frame=" in line or "size=" in line or "time=" in line):
                time_match = re.search(r"time=([\d:.]+)", line)
                speed_match = re.search(r"speed=\s*([\d\.]+[xX]?)", line)
                size_match = re.search(r"size=\s*([\d\.]+[A-Za-z]+)", line)

                speed = (speed_match.group(1) + ("" if "x" in speed_match.group(1).lower() else "x")) if speed_match else ""
                size = size_match.group(1) if size_match else ""

                if time_match and slice_duration > 0:
                    frac = min(1.0, parse_time_str(time_match.group(1)) / slice_duration)
                else:
                    frac = min(0.95, prog["frac"] + 0.02)
                prog["frac"] = max(prog["frac"], frac)
                _set_progress(_overall(), "Direct slicing...", speed=speed, size=size)
            # Parse progress: [download]  45.2% of ~ 125.40MiB at 5.20MiB/s ETA 00:13
            elif "[download]" in line:
                if prog["post"] or not prog["media"]:
                    continue
                pct_match = re.search(r"([\d\.]+)%", line)
                if not pct_match:
                    continue
                speed_match = re.search(r"at\s+([\d\.]+[A-Za-z]+/s)", line)
                eta_match = re.search(r"ETA\s+([\d:]+)", line)
                size_match = re.search(r"of\s+~?\s*([\d\.]+[A-Za-z]+)", line)

                frac = min(1.0, float(pct_match.group(1)) / 100.0)
                prog["frac"] = max(prog["frac"], frac)
                _set_progress(
                    _overall(),
                    "Direct slicing..." if is_direct_slice else "Downloading",
                    speed=speed_match.group(1) if speed_match else "",
                    eta=eta_match.group(1) if eta_match else "",
                    size=size_match.group(1) if size_match else "",
                )
            elif "[ExtractAudio]" in line:
                prog["post"] = True
                _set_progress(90.0, "Extracting audio...")
            elif "[Merger]" in line:
                prog["post"] = True
                _set_progress(92.0, "Merging tracks...")
            elif line.startswith("[Fixup") or "[VideoRemuxer]" in line or "[VideoConvertor]" in line:
                prog["post"] = True
                _set_progress(93.0, "Merging tracks...")
            elif "[EmbedSubtitle]" in line:
                prog["post"] = True
                _set_progress(94.0, "Embedding subtitles...")
            elif "[Metadata]" in line or "[ThumbnailsConvertor]" in line or "[EmbedThumbnail]" in line:
                prog["post"] = True
                _set_progress(96.0, "Embedding metadata...")

        proc.wait(timeout=300)
        if job.get("cancelled"):
            job["status"] = "cancelled"
            cleanup_job_temp_files(job_id, target_dir, base_folder=base_folder)
            return

        if proc.returncode != 0:
            if job.get("cancelled"):
                job["status"] = "cancelled"
            else:
                job["status"] = "error"
                _err_blob = "\n".join(last_lines) if last_lines else ""
                if is_blocked_error(_err_blob):
                    job["error"] = friendly_block_message(_err_blob)
                else:
                    job["error"] = last_lines[-1] if last_lines else "Download process failed"
            cleanup_job_temp_files(job_id, target_dir, base_folder=base_folder)
            return

        files = glob.glob(os.path.join(target_dir, f"{job_id}.*"))
        if not files:
            files = glob.glob(os.path.join(target_dir, f"{job_id}*.*"))
            files = [f for f in files if not f.endswith(".part") and not f.endswith(".ytdl")]
            if files:
                first_f = files[0]
                ext = os.path.splitext(first_f)[1]
                normalized_path = os.path.join(target_dir, f"{job_id}{ext}")
                if first_f != normalized_path:
                    try:
                        os.rename(first_f, normalized_path)
                        files = [normalized_path]
                    except Exception:
                        pass
        if not files:
            job["status"] = "error"
            job["error"] = "Download completed but no file was found"
            cleanup_job_temp_files(job_id, target_dir, base_folder=base_folder)
            return

        if format_choice == "audio":
            exact = os.path.join(target_dir, f"{job_id}.mp3")
            if os.path.exists(exact):
                chosen = exact
            else:
                target = [f for f in files if f.endswith(".mp3")]
                chosen = target[0] if target else files[0]
        else:
            exact = os.path.join(target_dir, f"{job_id}.mp4")
            if os.path.exists(exact):
                chosen = exact
            else:
                target = [f for f in files if f.endswith(".mp4") and not re.search(r'\.f\d+\.', f)]
                chosen = target[0] if target else ([f for f in files if f.endswith(".mp4")] or files)[0]

        for f in files:
            if f != chosen:
                remove_file_with_retries(f)

        # Post-download section trimming via FFmpeg (lossless, instant stream copy)
        if trim_method != "direct" and (trim_start or trim_end):
            _set_progress(97.0, "Trimming clip...")
            file_ext = os.path.splitext(chosen)[1]
            trimmed_chosen = os.path.join(target_dir, f"{job_id}_trimmed{file_ext}")
            trim_cmd = ["ffmpeg", "-y"]
            if trim_start and is_safe_timestamp(trim_start):
                trim_cmd += ["-ss", str(trim_start).strip()]
            if trim_end and is_safe_timestamp(trim_end):
                trim_cmd += ["-to", str(trim_end).strip()]
            trim_cmd += ["-i", chosen, "-c", "copy", trimmed_chosen]

            try:
                trim_res = subprocess.run(trim_cmd, capture_output=True, text=True, timeout=120)
                if trim_res.returncode == 0 and os.path.exists(trimmed_chosen) and os.path.getsize(trimmed_chosen) > 0:
                    remove_file_with_retries(chosen)
                    chosen = trimmed_chosen
                else:
                    # Fallback to re-encode if stream copy fails
                    trim_cmd_fallback = ["ffmpeg", "-y"]
                    if trim_start and is_safe_timestamp(trim_start):
                        trim_cmd_fallback += ["-ss", str(trim_start).strip()]
                    if trim_end and is_safe_timestamp(trim_end):
                        trim_cmd_fallback += ["-to", str(trim_end).strip()]
                    trim_cmd_fallback += ["-i", chosen, trimmed_chosen]
                    trim_res_fb = subprocess.run(trim_cmd_fallback, capture_output=True, text=True, timeout=180)
                    if trim_res_fb.returncode == 0 and os.path.exists(trimmed_chosen) and os.path.getsize(trimmed_chosen) > 0:
                        remove_file_with_retries(chosen)
                        chosen = trimmed_chosen
            except subprocess.TimeoutExpired:
                print(f"Trimming timed out for job {job_id}")
            except Exception as e:
                print(f"Trimming failed for job {job_id}: {e}")

        job["status"] = "done"
        job["file"] = chosen
        job["saved_dir"] = target_dir
        ext = os.path.splitext(chosen)[1]
        title = job.get("title", "").strip()

        # Sanitize title for filename
        if title:
            safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()[:100].strip()
            final_filename = f"{safe_title}{ext}" if safe_title else os.path.basename(chosen)
        else:
            final_filename = os.path.basename(chosen)

        # Rename to clean title in destination directory
        final_path = os.path.join(target_dir, final_filename)
        if final_path != chosen:
            if os.path.exists(final_path):
                base_name, file_ext = os.path.splitext(final_filename)
                counter = 1
                while os.path.exists(os.path.join(target_dir, f"{base_name} ({counter}){file_ext}")):
                    counter += 1
                final_filename = f"{base_name} ({counter}){file_ext}"
                final_path = os.path.join(target_dir, final_filename)
            
            renamed = False
            for _ in range(5):
                try:
                    os.replace(chosen, final_path)
                    job["file"] = final_path
                    renamed = True
                    break
                except OSError:
                    time.sleep(0.2)
            if not renamed:
                final_path = chosen
                final_filename = os.path.basename(chosen)
                job["file"] = chosen
        else:
            job["file"] = final_path

        job["filename"] = final_filename

        # Remove any lingering temporary files (.part, .ytdl, etc.) for this job
        cleanup_job_temp_files(job_id, target_dir, keep_file=final_path, base_folder=base_folder)

        # Android: make the file visible in the file manager's Videos/Audio/Images sections
        android_media_scan(job["file"])

        # Record into history
        file_size_bytes = 0
        try:
            file_size_bytes = os.path.getsize(job["file"])
        except Exception:
            pass

        final_duration = probe_media_duration(job["file"]) or job.get("duration")

        # Determine actual video / audio quality
        resolved_quality = quality or job.get("quality") or ""
        if format_choice == "audio":
            resolved_quality = "320kbps"
        else:
            actual_h = probe_video_height(final_path)
            if actual_h:
                resolved_quality = f"{actual_h}p"
            elif not resolved_quality:
                if format_id and isinstance(format_id, str) and format_id.endswith("p"):
                    resolved_quality = format_id

        add_history_entry({
            "id": job_id,
            "title": title or final_filename,
            "uploader": channel or job.get("uploader") or "Unknown Channel",
            "url": url,
            "thumbnail": job.get("thumbnail") or "",
            "duration": final_duration,
            "format": format_choice,
            "format_id": format_id,
            "quality": resolved_quality,
            "filename": final_filename,
            "file_path": job["file"],
            "saved_dir": target_dir,
            "filesize": format_size(file_size_bytes),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
    except subprocess.TimeoutExpired:
        job["status"] = "error"
        job["error"] = "Download timed out (5 min limit)"
        cleanup_job_temp_files(job_id, target_dir, base_folder=base_folder)
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        cleanup_job_temp_files(job_id, target_dir, base_folder=base_folder)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.json or {}
    url = data.get("url", "").strip()
    if not is_valid_media_url(url):
        return jsonify({"error": "Invalid or unsupported URL scheme"}), 400

    cookies_source = data.get("cookies_source", "auto")
    use_client_spoof = data.get("use_client_spoof", True)

    try:
        result = run_ytdlp_with_fallbacks(
            ["yt-dlp", "--no-playlist", "--geo-bypass"], ["-j", "--", url],
            url, cookies_source, use_client_spoof, timeout=60)

        if result.returncode != 0:
            return jsonify({"error": friendly_block_message(result.stderr)}), 400

        info = parse_ytdlp_json(result.stdout)
        duration = info.get("duration") or 0

        # Calculate best audio size
        audio_formats = [
            f for f in info.get("formats", [])
            if f.get("vcodec") == "none" and f.get("acodec") != "none"
        ]
        best_audio_size = max([estimate_format_size(f, duration) for f in audio_formats], default=0)

        # Build quality options — keep best format per resolution
        def format_preference_key(f):
            # 1. Prefer native HTTPS over m3u8/HLS streaming playlists (m3u8 often has fake inflated bitrate headers)
            is_m3u8 = "m3u8" in f.get("protocol", "")
            # 2. Prefer formats with real known filesize
            has_real_size = 1 if (f.get("filesize") or f.get("filesize_approx")) else 0
            # 3. Prefer mp4/h264 compatible codecs for best MP4 container compatibility
            vcodec = (f.get("vcodec") or "").lower()
            is_avc = 1 if ("avc" in vcodec or "h264" in vcodec) else 0
            # 4. Bitrate (tbr)
            tbr = f.get("tbr") or f.get("vbr") or 0
            return (not is_m3u8, has_real_size, is_avc, tbr)

        best_by_height = {}
        for f in info.get("formats", []):
            height = f.get("height")
            if height and f.get("vcodec", "none") != "none":
                if height not in best_by_height or format_preference_key(f) > format_preference_key(best_by_height[height]):
                    best_by_height[height] = f

        formats = []
        for height, f in best_by_height.items():
            v_size = estimate_format_size(f, duration)
            tot_size = v_size if f.get("acodec") != "none" else (v_size + best_audio_size)
            formats.append({
                "id": f["format_id"],
                "label": f"{height}p",
                "height": height,
                "filesize": format_size(tot_size),
                "filesize_bytes": tot_size,
            })
        formats.sort(key=lambda x: x["height"], reverse=True)

        # Build subtitle options
        subtitles = []
        seen_langs = set()

        raw_subs = info.get("subtitles") or {}
        raw_auto = info.get("automatic_captions") or {}
        video_lang = (info.get("language") or "").lower().split("-")[0]

        has_manual_subs = any(code != "live_chat" for code in raw_subs.keys())

        # 1. Process creator-uploaded subtitles
        for code, formats_list in raw_subs.items():
            if code == "live_chat":
                continue
            name = code
            if formats_list and isinstance(formats_list, list) and formats_list[0].get("name"):
                name = formats_list[0]["name"]
            name = re.sub(r'\s*\((?:auto-generated|auto)\)\s*', '', name, flags=re.IGNORECASE).strip()
            is_orig = False
            if video_lang and (code.lower() == video_lang or code.lower().startswith(video_lang + "-")):
                is_orig = True
            subtitles.append({
                "code": code,
                "name": name,
                "is_auto": False,
                "is_available": True,
                "is_original": is_orig,
            })
            seen_langs.add(code)

        # 2. Process automatic captions
        for code, formats_list in (raw_auto or {}).items():
            if code in seen_langs:
                continue
            base_code = code.replace("-orig", "")
            if code.endswith("-orig") and base_code in (raw_auto or {}):
                continue

            name = code
            if formats_list and isinstance(formats_list, list) and formats_list[0].get("name"):
                name = formats_list[0]["name"]
            name = re.sub(r'\s*\((?:auto-generated|auto)\)\s*', '', name, flags=re.IGNORECASE).strip()

            is_orig_speech = not any("tlang=" in (f.get("url") or "") for f in (formats_list or []))
            if video_lang and (code.lower() == video_lang or code.lower().startswith(video_lang + "-")):
                is_orig_speech = True

            is_avail = (not has_manual_subs and is_orig_speech)

            subtitles.append({
                "code": code,
                "name": name,
                "is_auto": True,
                "is_available": is_avail,
                "is_original": is_orig_speech,
            })
            seen_langs.add(code)

        # Ensure exactly one subtitle is marked as is_original
        orig_subs = [s for s in subtitles if s["is_original"]]
        if not orig_subs and subtitles:
            avail_sub = next((s for s in subtitles if s["is_available"]), subtitles[0])
            avail_sub["is_original"] = True
            avail_sub["is_available"] = True
        elif len(orig_subs) > 1:
            first_found = False
            for s in subtitles:
                if s["is_original"]:
                    if not first_found:
                        first_found = True
                    else:
                        s["is_original"] = False

        def sub_sort_key(s):
            orig_p = 0 if s.get("is_original") else 1
            avail_p = 0 if s.get("is_available") else 1
            auto_p = 1 if s.get("is_auto") else 0
            c = s["code"].lower()
            lang_p = 0 if (video_lang and c.startswith(video_lang)) else (1 if c.startswith("en") else 2)
            return (orig_p, avail_p, auto_p, lang_p, s.get("name", ""))

        subtitles.sort(key=sub_sort_key)

        return jsonify({
            "title": info.get("title", ""),
            "thumbnail": info.get("thumbnail", ""),
            "duration": duration,
            "uploader": info.get("uploader", ""),
            "formats": formats,
            "audio_filesize": format_size(best_audio_size),
            "audio_filesize_bytes": best_audio_size,
            "subtitles": subtitles,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching video info"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/playlist", methods=["POST"])
def get_playlist_info():
    data = request.json or {}
    url = data.get("url", "").strip()
    if not is_valid_media_url(url):
        return jsonify({"error": "Invalid or unsupported URL scheme"}), 400

    cookies_source = data.get("cookies_source", "auto")
    use_client_spoof = data.get("use_client_spoof", True)

    cmd = ["yt-dlp", "--flat-playlist", "--geo-bypass"] + get_ytdlp_bot_bypass_args(cookies_source, use_client_spoof) + ["-J", "--", url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            return jsonify({"error": result.stderr.strip().split("\n")[-1]}), 400

        info = parse_ytdlp_json(result.stdout)
        entries = info.get("entries", [])
        urls = [entry.get("url") for entry in entries if entry.get("url")]
        return jsonify({"urls": urls})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching playlist info"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/filesize", methods=["POST"])
def get_filesize():
    data = request.json or {}
    url = data.get("url", "").strip()
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    cookies_source = data.get("cookies_source", "auto")
    use_client_spoof = data.get("use_client_spoof", True)

    if not is_valid_media_url(url):
        return jsonify({"error": "Invalid or unsupported URL scheme"}), 400

    cmd = ["yt-dlp", "--no-playlist", "--geo-bypass"] + get_bypass_args_for(url, cookies_source, use_client_spoof) + ["-j", "--skip-download"]
    if format_choice == "audio":
        cmd += ["-x", "--audio-format", "mp3"]
    elif format_id:
        cmd += ["-f", f"{format_id}+bestaudio/best"]
    else:
        cmd += ["-f", "bestvideo+bestaudio/best"]

    cmd.extend(["--", url])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            return jsonify({"error": "Size unavailable"}), 400

        info = parse_ytdlp_json(result.stdout)
        duration = info.get("duration") or 0

        audio_formats = [
            f for f in info.get("formats", [])
            if f.get("vcodec") == "none" and f.get("acodec") != "none"
        ]
        best_audio_size = max([estimate_format_size(f, duration) for f in audio_formats], default=0)

        total_size = 0
        if format_choice == "audio":
            total_size = int(best_audio_size * 1.05) if best_audio_size else estimate_format_size(info, duration)
        else:
            req_formats = info.get("requested_formats", [])
            if req_formats:
                total_size = sum(estimate_format_size(f, duration) for f in req_formats)

            if not total_size and format_id and "formats" in info:
                target_v = next((f for f in info["formats"] if str(f.get("format_id")) == str(format_id)), None)
                if target_v:
                    v_size = estimate_format_size(target_v, duration)
                    total_size = v_size if target_v.get("acodec") != "none" else (v_size + best_audio_size)

            if not total_size:
                total_size = estimate_format_size(info, duration)

        return jsonify({
            "size_bytes": total_size,
            "formatted": format_size(total_size)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json or {}
    url = data.get("url", "").strip()
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    title = data.get("title", "")
    thumbnail = data.get("thumbnail", "")
    output_folder = data.get("output_folder", "").strip()
    create_channel_folder = bool(data.get("create_channel_folder", True))
    channel = data.get("channel", "").strip()
    trim_start = data.get("trim_start", "").strip()
    trim_end = data.get("trim_end", "").strip()
    embed_metadata = bool(data.get("embed_metadata", True))

    embed_subs = bool(data.get("embed_subs", False))
    sub_lang = data.get("sub_lang", "en").strip() or "en"
    quality = (data.get("quality") or data.get("quality_label") or "").strip()
    trim_method = (data.get("trim_method") or "full").strip().lower()
    cookies_source = data.get("cookies_source", "auto")
    use_client_spoof = data.get("use_client_spoof", True)

    if not is_valid_media_url(url):
        return jsonify({"error": "Invalid or unsupported URL scheme"}), 400

    if trim_start and not is_safe_timestamp(trim_start):
        return jsonify({"error": "Invalid trim start time format. Please use mm:ss or hh:mm:ss"}), 400
    if trim_end and not is_safe_timestamp(trim_end):
        return jsonify({"error": "Invalid trim end time format. Please use mm:ss or hh:mm:ss"}), 400

    def parse_time_to_seconds(t_str):
        try:
            s = str(t_str).strip()
            if not s:
                return None
            parts = [float(p) for p in s.split(":")]
            if len(parts) == 1:
                return parts[0]
            elif len(parts) == 2:
                return parts[0] * 60 + parts[1]
            elif len(parts) == 3:
                return parts[0] * 3600 + parts[1] * 60 + parts[2]
        except Exception:
            return None
        return None

    if trim_start and trim_end:
        s_sec = parse_time_to_seconds(trim_start)
        e_sec = parse_time_to_seconds(trim_end)
        if s_sec is not None and e_sec is not None and s_sec >= e_sec:
            return jsonify({"error": "Trim start time must be before end time"}), 400

    duration = data.get("duration")
    job_id = uuid.uuid4().hex[:10]

    base_folder = get_effective_download_dir(output_folder)
    target_dir = base_folder
    if create_channel_folder and channel:
        safe_channel = "".join(c for c in channel if c not in r'\/:*?"<>|').strip() or "Unknown Channel"
        target_dir = os.path.join(target_dir, safe_channel)

    # Bound jobs dictionary to prevent unbounded memory growth
    if len(jobs) > 100:
        prunable = [jid for jid, j in jobs.items() if j.get("status") in ("done", "error", "cancelled")]
        for jid in prunable[:max(1, len(jobs) - 80)]:
            jobs.pop(jid, None)

    jobs[job_id] = {
        "status": "downloading",
        "url": url,
        "title": title,
        "thumbnail": thumbnail,
        "uploader": channel,
        "duration": duration,
        "quality": quality,
        "trim_method": trim_method,
        "saved_dir": target_dir,
    }

    thread = threading.Thread(
        target=run_download,
        args=(job_id, url, format_choice, format_id, output_folder, create_channel_folder, channel, trim_start, trim_end, embed_metadata, embed_subs, sub_lang, quality, trim_method, cookies_source, use_client_spoof)
    )
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


def translate_subtitle_file(src_path, dst_path, target_lang):
    try:
        import urllib.request, urllib.parse
        with open(src_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

        cue_indices = []
        texts_to_translate = []
        for idx, line in enumerate(lines):
            s = line.strip()
            if not s or s.isdigit() or "-->" in s or s.startswith("WEBVTT") or s.startswith("NOTE") or s.startswith("Kind:") or s.startswith("Language:"):
                continue
            cue_indices.append(idx)
            texts_to_translate.append(s)

        if not texts_to_translate:
            return False

        clean_lang = re.sub(r'[^a-zA-Z0-9_\-]', '', target_lang.split("-")[0].lower()) or "en"
        translated_texts = []
        chunk_size = 40
        delimiter = "\n<<<SEP>>>\n"
        for i in range(0, len(texts_to_translate), chunk_size):
            chunk = texts_to_translate[i:i+chunk_size]
            joined = delimiter.join(chunk)
            url = "https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=" + urllib.parse.quote(clean_lang) + "&dt=t&q=" + urllib.parse.quote(joined)
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                trans_block = "".join(c[0] for c in data[0] if c and c[0])
                parts = trans_block.split("<<<SEP>>>")
                if len(parts) == len(chunk):
                    translated_texts.extend([p.strip() for p in parts])
                else:
                    for t in chunk:
                        url_single = "https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=" + urllib.parse.quote(clean_lang) + "&dt=t&q=" + urllib.parse.quote(t)
                        req_s = urllib.request.Request(url_single, headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(req_s, timeout=8) as resp_s:
                            d_s = json.loads(resp_s.read().decode("utf-8"))
                            translated_texts.append("".join(c[0] for c in d_s[0] if c and c[0]).strip())

        for idx, trans in zip(cue_indices, translated_texts):
            lines[idx] = trans + "\n"

        with open(dst_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        return True
    except Exception as e:
        print(f"Subtitle translation fallback failed: {e}")
        return False


@app.route("/api/download-subtitles", methods=["GET"])
def download_subtitles():
    url = request.args.get("url", "").strip()
    if not is_valid_media_url(url):
        return jsonify({"error": "Invalid or unsupported URL scheme"}), 400

    lang = re.sub(r'[^a-zA-Z0-9_\-]', '', request.args.get("lang", "en").strip() or "en")
    fmt = request.args.get("format", "srt").strip().lower()
    title = request.args.get("title", "subtitles").strip()

    if fmt not in ("srt", "vtt"):
        fmt = "srt"

    temp_dir = tempfile.mkdtemp(prefix="reclip_subs_")
    try:
        out_pattern = os.path.join(temp_dir, "sub.%(ext)s")

        cmd = [
            "yt-dlp",
            "--no-playlist",
            "--geo-bypass",
            "--extractor-args", "youtube:player_client=android,web",
            "--write-sub",
            "--write-auto-subs",
            "--sub-langs", lang,
            "--sub-format", fmt,
            "--convert-subs", fmt,
            "--skip-download",
            "-o", out_pattern,
            "--",
            url
        ]

        subprocess.run(cmd, capture_output=True, text=True, timeout=35)
        sub_files = glob.glob(os.path.join(temp_dir, f"*.{fmt}"))
        if not sub_files:
            sub_files = glob.glob(os.path.join(temp_dir, "*.*"))
            sub_files = [f for f in sub_files if not f.endswith(".json")]

        # If language not found directly or rate-limited, fall back to downloading original subtitle and translating
        if not sub_files:
            orig_pattern = os.path.join(temp_dir, "orig.%(ext)s")
            cmd_orig = [
                "yt-dlp",
                "--no-playlist",
                "--geo-bypass",
                "--extractor-args", "youtube:player_client=android,web",
                "--write-sub",
                "--write-auto-subs",
                "--sub-langs", "en.*,.*-orig",
                "--sub-format", fmt,
                "--convert-subs", fmt,
                "--skip-download",
                "-o", orig_pattern,
                "--",
                url
            ]
            subprocess.run(cmd_orig, capture_output=True, text=True, timeout=35)
            orig_files = glob.glob(os.path.join(temp_dir, f"orig*.{fmt}"))
            if not orig_files:
                orig_files = glob.glob(os.path.join(temp_dir, "orig*.*"))
                orig_files = [f for f in orig_files if not f.endswith(".json")]

            if orig_files:
                translated_file = os.path.join(temp_dir, f"translated.{lang}.{fmt}")
                if translate_subtitle_file(orig_files[0], translated_file, lang):
                    sub_files = [translated_file]
                else:
                    sub_files = [orig_files[0]]

        if not sub_files:
            return jsonify({"error": f"No {lang} subtitles found for this video"}), 404

        target_file = sub_files[0]
        ext = os.path.splitext(target_file)[1].lstrip(".") or fmt

        # Strip CRLF, control characters, and illegal path characters
        safe_title = "".join(c for c in title if c not in r'\/:*?"<>|' and 32 <= ord(c) <= 126).strip() or "subtitles"
        download_filename = f"{safe_title}.{lang}.{ext}"

        with open(target_file, "rb") as f:
            sub_content = f.read()

        return Response(
            sub_content,
            mimetype="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{download_filename}"'}
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@app.route("/api/download-thumbnail", methods=["GET"])
def download_thumbnail():
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if not (url.startswith("http://") or url.startswith("https://")):
        return jsonify({"error": "Invalid URL"}), 400
    try:
        import urllib.request
        from flask import Response
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            content = resp.read()
            content_type = resp.headers.get("Content-Type", "image/jpeg")
            return Response(
                content,
                mimetype=content_type,
                headers={"Content-Disposition": 'attachment; filename="thumbnail.jpg"'}
            )
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/status/<job_id>")
def check_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "error": job.get("error"),
        "filename": job.get("filename"),
        "file": job.get("file"),
        "saved_dir": job.get("saved_dir"),
        "progress": job.get("progress"),
    })


@app.route("/api/cancel/<job_id>", methods=["POST"])
def cancel_job(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    job["cancelled"] = True
    job["status"] = "cancelled"
    proc = job.get("proc")
    if proc:
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
            else:
                proc.terminate()
        except Exception:
            pass

    time.sleep(0.3)
    target_dir = job.get("saved_dir") or get_system_default_folder()
    cleanup_job_temp_files(job_id, target_dir)

    return jsonify({"status": "cancelled"})


@app.route("/api/file/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404
    file_path = job.get("file")
    if not file_path or not os.path.exists(file_path):
        return jsonify({"error": "File not found on disk"}), 404
    return send_file(file_path, as_attachment=True, download_name=job.get("filename") or os.path.basename(file_path))


@app.route("/api/system-info", methods=["GET"])
def system_info():
    sys_name = platform.system().lower()
    home = os.path.expanduser("~")

    server_os = "windows"
    if is_android():
        server_os = "android"
    elif "darwin" in sys_name:
        server_os = "mac"
    elif "linux" in sys_name:
        server_os = "linux"

    defaults = {
        "windows": os.path.join(home, "Downloads", "ReClip") if "windows" in sys_name else "C:\\Users\\Downloads\\ReClip",
        "mac": os.path.join(home, "Downloads", "ReClip") if "darwin" in sys_name else "~/Downloads/ReClip",
        "linux": os.path.join(home, "Downloads", "ReClip") if (server_os == "linux") else "~/Downloads/ReClip",
        "android": get_android_download_dir() if server_os == "android" else "/storage/emulated/0/Download/ReClip",
        "ios": "Files/Downloads/ReClip",
    }

    return jsonify({
        "server_os": server_os,
        "default_folder": get_system_default_folder(),
        "platform_defaults": defaults,
    })


_DRIVES_CACHE = {"t": 0.0, "data": []}


def _list_windows_drives(ttl=15.0):
    """Windows drive letters, cached briefly: probing every letter (esp. disconnected
    network drives or empty card readers) can take seconds and made the folder picker lag."""
    if platform.system().lower() != "windows":
        return []
    now = time.time()
    if now - _DRIVES_CACHE["t"] < ttl and _DRIVES_CACHE["data"]:
        return _DRIVES_CACHE["data"]
    import string
    drives = []
    for letter in string.ascii_uppercase:
        drv = f"{letter}:\\"
        if os.path.exists(drv):
            drives.append({"name": f"{letter}:", "path": drv})
    _DRIVES_CACHE["t"] = now
    _DRIVES_CACHE["data"] = drives
    return drives


@app.route("/api/browse-folders", methods=["GET"])
@app.route("/api/browse-folder", methods=["GET"])
def browse_folders():
    req_path = request.args.get("path", "").strip()
    ensure = request.args.get("ensure", "") == "1"
    home = os.path.expanduser("~")

    # Android: accept paths relative to shared storage ("Download/ReClip")
    if req_path and is_android() and not os.path.isabs(req_path) and not req_path.startswith("~") \
            and not re.match(r"^[A-Za-z]:[\\/]", req_path):
        req_path = os.path.join(ANDROID_SHARED_ROOT, req_path.strip("/\\"))

    # Optionally create the folder (used by the "ReClip" shortcut) so it can be opened directly
    if ensure and req_path and is_safe_system_path(req_path):
        try:
            os.makedirs(os.path.abspath(os.path.expanduser(req_path)), exist_ok=True)
        except Exception as e:
            print(f"Could not create folder {req_path}: {e}")

    # If no path provided or path is forbidden, default to Downloads or home
    if not req_path or not is_safe_system_path(req_path):
        req_path = os.path.join(home, "Downloads")
        if not os.path.exists(req_path):
            req_path = home

    target_path = os.path.abspath(os.path.expanduser(req_path))
    # A folder that doesn't exist yet (e.g. the default ".../ReClip") -> open its nearest existing parent
    probe = target_path
    while probe and not os.path.isdir(probe) and os.path.dirname(probe) != probe:
        probe = os.path.dirname(probe)
    if probe and os.path.isdir(probe) and is_safe_system_path(probe):
        target_path = probe
    if not os.path.exists(target_path) or not is_safe_system_path(target_path):
        target_path = os.path.join(home, "Downloads")
        if not os.path.exists(target_path):
            target_path = home

    # Android: never go above shared storage (/storage/emulated/0)
    if is_android() and os.path.isdir(ANDROID_SHARED_ROOT) and target_path.rstrip("/") in ("/storage", "/storage/emulated", "/storage/self", ""):
        target_path = ANDROID_SHARED_ROOT

    # Parent directory
    parent_path = os.path.dirname(target_path)
    if parent_path == target_path or not is_safe_system_path(parent_path):
        parent_path = ""  # Reached safe boundary or root
    if is_android() and target_path.rstrip("/") == ANDROID_SHARED_ROOT:
        parent_path = ""  # Home of shared storage: can't go higher

    # Quick locations
    quick_locations = []
    for name, sub in [
        ("Downloads", "Downloads"),
        ("Desktop", "Desktop"),
        ("Videos", "Videos"),
        ("Documents", "Documents"),
        ("Music", "Music"),
    ]:
        p = os.path.join(home, sub)
        if os.path.exists(p) and is_safe_system_path(p):
            quick_locations.append({"name": name, "path": p})
    if is_android():
        # Android shortcut order (after "ReClip", which the UI adds first): Home, Download, Movies, Music
        quick_locations = []
        for name, p in [
            ("Shared storage", "/storage/emulated/0"),  # shown as "Home" in the UI
            ("Download", "/storage/emulated/0/Download"),
            ("Movies", "/storage/emulated/0/Movies"),
            ("Music", "/storage/emulated/0/Music"),
        ]:
            if os.path.isdir(p) and os.access(p, os.R_OK):
                quick_locations.append({"name": name, "path": p})

    # Available drives on Windows
    drives = _list_windows_drives()

    # Scan subfolders safely
    folders = []
    try:
        with os.scandir(target_path) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name.startswith((".", "$")):
                            continue
                        if entry.name.lower() in ("system volume information", "recovery", "windows", "program files", "program files (x86)"):
                            continue
                        folders.append(entry.name)
                except OSError:
                    continue
        folders.sort(key=lambda s: s.lower())
    except Exception as e:
        print(f"Error scanning directory {target_path}: {e}")

    return jsonify({
        "current_path": target_path,
        "parent_path": parent_path,
        "quick_locations": quick_locations,
        "is_android": is_android(),
        "reclip_path": get_system_default_folder(),
        "drives": drives,
        "folders": folders,
    })


@app.route("/api/create-folder", methods=["POST"])
def create_folder():
    data = request.get_json(silent=True) or {}
    parent = data.get("parent", "").strip()
    name = data.get("name", "").strip()
    if not parent or not name:
        return jsonify({"error": "Parent folder and name required"}), 400

    parent_abs = os.path.abspath(parent)
    if not os.path.exists(parent_abs) or not os.path.isdir(parent_abs) or not is_safe_system_path(parent_abs):
        return jsonify({"error": "Cannot create folder in protected or invalid system path"}), 403

    safe_name = "".join(c for c in name if c not in r'\/:*?"<>|' and ord(c) >= 32).strip()
    if not safe_name or safe_name in (".", ".."):
        return jsonify({"error": "Invalid folder name"}), 400

    new_path = os.path.join(parent_abs, safe_name)
    try:
        os.makedirs(new_path, exist_ok=True)
        return jsonify({"success": True, "path": new_path})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/history", strict_slashes=False)
def history_page():
    return render_template("index.html")


@app.route("/settings", strict_slashes=False)
def settings_page():
    return render_template("index.html")


def _linux_opener():
    """xdg-open on Linux desktops, termux-open on Termux/Android; None if neither exists."""
    if is_android() and shutil.which("termux-open"):
        return "termux-open"
    return shutil.which("xdg-open")


def open_in_file_manager(abs_path):
    """
    Opens the file manager on Windows, macOS, or Linux.
    On Windows, uses Win32 ShellExecuteW with explorer.exe /select,"<file>" to highlight
    the file and bring Explorer directly into the foreground without falling back to Documents.
    """
    abs_path = os.path.abspath(os.path.normpath(abs_path))
    target_dir = os.path.dirname(abs_path) if os.path.isfile(abs_path) else abs_path

    if sys.platform == "win32":
        folder = target_dir if os.path.exists(target_dir) else abs_path
        if not os.path.exists(folder):
            folder = get_system_default_folder()
            os.makedirs(folder, exist_ok=True)

        # 1. Primary: Win32 ShellExecuteW via ctypes with SW_SHOWNORMAL (1) to activate window
        try:
            import ctypes
            shell32 = ctypes.windll.shell32
            if os.path.isfile(abs_path):
                params = f'/select,"{abs_path}"'
            else:
                params = f'"{folder}"'
            ret = shell32.ShellExecuteW(None, "open", "explorer.exe", params, None, 1)
            if ret > 32:
                return True
        except Exception:
            pass

        # 2. Fallback: WScript.Shell via PowerShell to force foreground window
        try:
            target = abs_path if os.path.isfile(abs_path) else folder
            arg = f'/select,"{target}"' if os.path.isfile(target) else f'"{target}"'
            env = os.environ.copy()
            env["RECLIP_EXPLORER_ARG"] = arg
            ps = """
$a = $env:RECLIP_EXPLORER_ARG
$w = New-Object -ComObject WScript.Shell
$w.Run("explorer.exe " + $a, 1, $false)
"""
            subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], env=env)
            return True
        except Exception:
            pass

        # 3. Fallback: Native os.startfile on folder
        try:
            os.startfile(folder)
            return True
        except Exception:
            pass

        # 4. Fallback: direct subprocess
        try:
            subprocess.Popen(f'explorer.exe "{folder}"')
            return True
        except Exception:
            pass

        return False
    elif sys.platform == "darwin":
        if os.path.isfile(abs_path):
            subprocess.Popen(["open", "-R", abs_path])
        else:
            subprocess.Popen(["open", target_dir])
        return True
    else:
        folder = target_dir if os.path.exists(target_dir) else abs_path
        opener = _linux_opener()
        if not opener:
            return False
        subprocess.Popen([opener, folder])
        return True


@app.route("/api/open-folder", methods=["POST"])
def open_folder():
    data = request.json or {}
    path = data.get("path", "").strip()
    job_id = data.get("job_id", "").strip()

    if not path and job_id:
        job = jobs.get(job_id)
        if job:
            path = job.get("file") or job.get("saved_dir")

    if not path:
        path = get_system_default_folder()

    abs_path = os.path.abspath(path)
    if not is_safe_system_path(abs_path):
        return jsonify({"error": "Access to protected system path denied", "ok": False}), 403

    target_dir = abs_path
    if os.path.isfile(abs_path):
        target_dir = os.path.dirname(abs_path)

    if not os.path.exists(target_dir):
        parent = os.path.dirname(target_dir)
        if os.path.exists(parent):
            target_dir = parent
            abs_path = parent
        else:
            target_dir = get_system_default_folder()
            abs_path = target_dir
            os.makedirs(target_dir, exist_ok=True)

    try:
        ok = open_in_file_manager(abs_path)
        if ok:
            return jsonify({"success": True, "ok": True, "opened": target_dir})
        return jsonify({"error": "Could not open folder in file manager", "ok": False}), 500
    except Exception as e:
        return jsonify({"error": str(e), "ok": False}), 500


@app.route("/api/play-file", methods=["POST"])
def play_file():
    data = request.json or {}
    path = data.get("path", "").strip()
    job_id = data.get("job_id", "").strip()

    if not path and job_id:
        job = jobs.get(job_id)
        if job and job.get("file"):
            path = job.get("file")

    if not path:
        return jsonify({"error": "File path not available", "ok": False}), 400

    abs_path = os.path.abspath(path)
    if not is_safe_system_path(abs_path):
        return jsonify({"error": "Access to protected system path denied", "ok": False}), 403

    if not os.path.exists(abs_path) or not os.path.isfile(abs_path):
        return jsonify({"error": f"File not found on disk: {os.path.basename(abs_path)}", "ok": False}), 404

    try:
        if sys.platform == "win32":
            try:
                os.startfile(abs_path)
                return jsonify({"success": True, "ok": True})
            except Exception:
                pass
            try:
                subprocess.Popen(f'cmd.exe /c start "" "{abs_path}"')
                return jsonify({"success": True, "ok": True})
            except Exception:
                pass
            env = os.environ.copy()
            env["RECLIP_PLAY_TARGET"] = abs_path
            ps_script = """
$p = $env:RECLIP_PLAY_TARGET
if (Test-Path -LiteralPath $p) {
    Start-Process -LiteralPath $p
}
"""
            subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script], env=env)
            return jsonify({"success": True, "ok": True})
        elif sys.platform == "darwin":
            subprocess.Popen(["open", abs_path])
            return jsonify({"success": True, "ok": True})
        else:
            opener = _linux_opener()
            if not opener:
                return jsonify({"error": "No system opener available on this device (install termux-api on Android)" if is_android() else "No system opener (xdg-open) available on this server", "ok": False}), 500
            subprocess.Popen([opener, abs_path])
            return jsonify({"success": True, "ok": True})
    except Exception as e:
        return jsonify({"error": str(e), "ok": False}), 500



@app.route("/api/history", methods=["GET"])
def get_history():
    history = load_history()
    dirty = False
    for item in history:
        file_path = item.get("file_path", "")
        item["file_exists"] = os.path.exists(file_path) if file_path else False
        current_q = item.get("quality")
        if not current_q:
            if item.get("format") == "audio":
                item["quality"] = "320kbps"
                dirty = True
            elif file_path and os.path.exists(file_path):
                actual_h = probe_video_height(file_path)
                if actual_h:
                    item["quality"] = f"{actual_h}p"
                    dirty = True
    if dirty:
        save_history(history)
    return jsonify({"history": history})


@app.route("/api/history", methods=["DELETE"])
def clear_history():
    save_history([])
    return jsonify({"success": True})


@app.route("/api/history/<item_id>", methods=["DELETE"])
def delete_history_item(item_id):
    history = load_history()
    history = [h for h in history if h.get("id") != item_id]
    save_history(history)
    return jsonify({"success": True})


@app.route("/api/cookies/status", methods=["GET"])
def get_cookies_status():
    has_file = os.path.exists(COOKIE_FILE) and os.path.getsize(COOKIE_FILE) > 0
    size = os.path.getsize(COOKIE_FILE) if has_file else 0
    size_str = f"{size / 1024:.1f} KB" if size >= 1024 else f"{size} B"
    has_ff = detect_firefox_profile()
    return jsonify({
        "has_cookie_file": has_file,
        "cookie_file_size": size_str if has_file else None,
        "firefox_available": has_ff,
    })


@app.route("/api/cookies/upload", methods=["POST"])
def upload_cookies():
    content = ""
    if "file" in request.files:
        f = request.files["file"]
        if f and f.filename:
            raw = f.read(512 * 1024 + 1)
            if len(raw) > 512 * 1024:
                return jsonify({"error": "Cookie file exceeds 512 KB limit"}), 400
            content = raw.decode("utf-8", errors="ignore")
    elif request.json and "content" in request.json:
        content = request.json.get("content", "")
        if len(content.encode("utf-8")) > 512 * 1024:
            return jsonify({"error": "Cookie content exceeds 512 KB limit"}), 400

    content = content.strip()
    if not content:
        return jsonify({"error": "No cookies provided"}), 400

    # Basic Netscape or header format validation
    lines = [l for l in content.splitlines() if l.strip()]
    is_plausible = any(
        "# Netscape" in l or "\t" in l or l.startswith(".") or "Cookie:" in l
        for l in lines[:15]
    )
    if not is_plausible:
        return jsonify({"error": "File does not appear to be a valid Netscape cookie file"}), 400

    with open(COOKIE_FILE, "w", encoding="utf-8") as f:
        f.write(content)
    return jsonify({"success": True, "message": "cookies.txt saved successfully"})


@app.route("/api/cookies/delete", methods=["POST"])
def delete_cookies():
    if os.path.exists(COOKIE_FILE):
        try:
            os.remove(COOKIE_FILE)
            return jsonify({"success": True, "message": "cookies.txt deleted"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"success": True, "message": "No cookies.txt found"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "0.0.0.0")
    if is_android() and not shutil.which("node"):
        print("\n  [!] nodejs is missing, so YouTube will only show 360p.\n"
              "      Fix:  pkg install -y nodejs && pip install -U yt-dlp yt-dlp-ejs\n"
              "      Then restart ReClip.\n")
    app.run(host=host, port=port)
