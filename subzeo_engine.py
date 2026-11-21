"""
Subzeo engine: multi-segment HTTP downloader with pause/resume,
dynamic segment splitting, retries and a global speed limiter.
No GUI dependency.
"""
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

APP_DIR = Path.home() / ".subzeo"
STATE_FILE = APP_DIR / "state.json"
SETTINGS_FILE = APP_DIR / "settings.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 Subzeo/1.0")
CHUNK = 64 * 1024
MIN_SPLIT = 1024 * 1024      # never split a segment smaller than 2 x this
RETRIES = 6
PATH_LOCK = threading.Lock()

CATEGORIES = {
    "Video": {"mp4", "mkv", "avi", "mov", "wmv", "flv", "webm", "m4v", "mpg", "mpeg", "3gp", "ts"},
    "Music": {"mp3", "wav", "flac", "aac", "ogg", "m4a", "wma", "opus"},
    "Documents": {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "epub", "odt", "csv", "rtf"},
    "Archives": {"zip", "rar", "7z", "tar", "gz", "bz2", "xz", "iso", "tgz"},
    "Programs": {"exe", "msi", "apk", "dmg", "deb", "rpm", "appimage", "pkg", "bat"},
}
ALL_EXT = set().union(*CATEGORIES.values())

DEFAULT_SETTINGS = {
    "folder": str(Path.home() / "Downloads" / "Subzeo"),
    "connections": 8,
    "max_active": 3,
    "speed_limit_kb": 0,
    "by_category": True,
    "clipboard": True,
    "token": "",
    "tray": True,
    "schedule": {"enabled": False, "start": "02:00", "stop": "", "when_done": "none"},
}


def ext_of(name):
    path = urlparse(name).path if "://" in name else name
    return os.path.splitext(path)[1].lstrip(".").lower()


def category_of(name):
    e = ext_of(name)
    for cat, exts in CATEGORIES.items():
        if e in exts:
            return cat
    return "Other"


def fmt_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024


def fmt_speed(bps):
    return "" if bps < 1 else fmt_size(bps) + "/s"


def fmt_eta(sec):
    if sec is None or sec < 0:
        return ""
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def filename_from(resp, url):
    cd = resp.headers.get("Content-Disposition", "")
    name = ""
    m = re.search(r"filename\*\s*=\s*[^']*'[^']*'([^;]+)", cd, re.I)
    if m:
        name = unquote(m.group(1).strip().strip('"'))
    else:
        m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.I)
        if m:
            name = m.group(1).strip()
    if not name:
        name = unquote(os.path.basename(urlparse(resp.url or url).path))
    name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", name).strip(" .")
    if not name:
        name = "download"
    if not os.path.splitext(name)[1]:
        ctype = resp.headers.get("Content-Type", "").split(";")[0].strip()
        guess = mimetypes.guess_extension(ctype) if ctype else None
        if guess and guess != ".bin":
            name += guess
    return name


def unique_path(folder, name):
    p = Path(folder) / name
    stem, suf = p.stem, p.suffix
    i = 1
    while p.exists() or Path(str(p) + ".subzeo").exists():
        p = Path(folder) / f"{stem} ({i}){suf}"
        i += 1
    return p


class RateLimiter:
    """Global speed limiter shared by every connection."""

    def __init__(self):
        self.limit = 0  # bytes per second, 0 = unlimited
        self._lock = threading.Lock()
        self._next = 0.0

    def acquire(self, n):
        if self.limit <= 0:
            return
        with self._lock:
            now = time.monotonic()
            start = max(self._next, now)
            self._next = start + n / self.limit
            wait = start - now
        if wait > 0:
            time.sleep(min(wait, 5))


LIMITER = RateLimiter()


class Download:
    kind = "file"

    def __init__(self, url, base, name="", by_category=True, connections=8,
                 referer="", cookie="", uid=None):
        self.id = uid or uuid.uuid4().hex[:10]
        self.url = url
        self.base = str(base)
        self.name = name
        self.by_category = by_category
        self.connections = connections
        self.referer = referer
        self.cookie = cookie
        self.path = ""
        self.size = 0
        self.resumable = False
        self.probed = False
        self.status = "queued"   # queued | downloading | paused | done | error
        self.error = ""
        self.added = time.time()
        self.segments = []       # each: [start, end, next_byte_to_fetch]
        self.single_done = 0
        self._init_runtime()

    def _init_runtime(self):
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.user_stop = False
        self.worker = None
        self.claimed = set()
        self.failed = None
        self.counter = 0
        self.speed = 0.0
        self._lc = 0
        self._lt = time.monotonic()

    # ---------- persistence ----------
    def to_dict(self):
        with self.lock:
            segs = [list(s) for s in self.segments]
        return {
            "id": self.id, "url": self.url, "base": self.base, "name": self.name,
            "by_category": self.by_category, "connections": self.connections,
            "referer": self.referer, "cookie": self.cookie, "path": self.path,
            "size": self.size, "resumable": self.resumable, "probed": self.probed,
            "status": self.status, "error": self.error, "added": self.added,
            "segments": segs, "single_done": self.single_done,
            "kind": self.kind, "fmt": getattr(self, "fmt", ""),
        }

    @classmethod
    def from_dict(cls, d):
        o = cls(d["url"], d["base"], d.get("name", ""), d.get("by_category", True),
                d.get("connections", 8), d.get("referer", ""), d.get("cookie", ""), d["id"])
        for k in ("path", "size", "resumable", "probed", "status", "error",
                  "added", "segments", "single_done"):
            if k in d:
                setattr(o, k, d[k])
        if o.status in ("downloading", "queued"):
            o.status = "paused"
        return o

    # ---------- info ----------
    @property
    def tmp(self):
        return self.path + ".subzeo"

    @property
    def category(self):
        return category_of(self.name or self.url)

    @property
    def done(self):
        if self.status == "done":
            return self.size
        if self.segments:
            return sum(min(s[2], s[1] + 1) - s[0] for s in list(self.segments))
        return self.single_done

    @property
    def percent(self):
        return (self.done / self.size * 100) if self.size else 0.0

    @property
    def eta(self):
        if self.status != "downloading" or self.speed < 1 or not self.size:
            return None
        return (self.size - self.done) / self.speed

    def tick(self):
        now = time.monotonic()
        dt = now - self._lt
        if dt < 0.4:
            return
        c = self.counter
        inst = (c - self._lc) / dt
        self.speed = inst if self.speed == 0 else self.speed * 0.6 + inst * 0.4
        self._lc, self._lt = c, now
        if self.status != "downloading":
            self.speed = 0.0

    def headers(self, extra=None):
        h = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "identity"}
        if self.referer:
            h["Referer"] = self.referer
        if self.cookie:
            h["Cookie"] = self.cookie
        if extra:
            h.update(extra)
        return h

    # ---------- control ----------
    def start(self):
        self.user_stop = False
        self.stop = threading.Event()
        self.failed = None
        self.status = "downloading"
        self.error = ""
        self.speed = 0.0
        self.worker = threading.Thread(target=self._run, daemon=True)
        self.worker.start()

    def pause(self):
        self.user_stop = True
        self.stop.set()
        if self.status in ("downloading", "queued"):
            self.status = "paused"

    def discard(self, delete_file=False):
        """Stop and (optionally) remove the files once the worker has exited."""
        self.pause()

        def cleanup():
            if self.worker:
                self.worker.join(timeout=8)
            for p in (self.tmp, self.path if delete_file else None):
                if p and p != ".subzeo":
                    try:
                        os.remove(p)
                    except OSError:
                        pass
        threading.Thread(target=cleanup, daemon=True).start()

    # ---------- engine ----------
    def _run(self):
        try:
            if not self.probed:
                self._probe()
            if self.user_stop:
                return
            if self.resumable and self.size > 0:
                self._segmented()
            else:
                self._single()
            if self.failed:
                raise self.failed
            if self.user_stop:
                return
            if self.size and self.done < self.size:
                raise IOError("Download incomplete")
            os.replace(self.tmp, self.path)
            self.status = "done"
        except Exception as e:  # noqa: BLE001
            if self.user_stop:
                return
            self.status = "error"
            self.error = str(e) or e.__class__.__name__

    def _probe(self):
        with requests.get(self.url, headers=self.headers({"Range": "bytes=0-0"}),
                          stream=True, timeout=(10, 20)) as r:
            if r.status_code == 206:
                m = re.search(r"/(\d+)$", r.headers.get("Content-Range", ""))
                self.size = int(m.group(1)) if m else 0
                self.resumable = bool(m)
            elif r.status_code == 200:
                self.size = int(r.headers.get("Content-Length") or 0)
                self.resumable = False
            elif r.status_code == 416:
                self.size, self.resumable = 0, False
            else:
                r.raise_for_status()
                raise IOError(f"HTTP {r.status_code}")
            name = self.name or filename_from(r, self.url)

        folder = Path(self.base)
        if self.by_category:
            folder = folder / category_of(name)
        folder.mkdir(parents=True, exist_ok=True)
        if self.size and shutil.disk_usage(folder).free < self.size:
            raise IOError("Not enough disk space")
        with PATH_LOCK:
            p = unique_path(folder, name)
            open(str(p) + ".subzeo", "ab").close()
        self.path, self.name, self.probed = str(p), p.name, True

    def _single(self):
        self.single_done = 0
        with requests.get(self.url, headers=self.headers(), stream=True,
                          timeout=(10, 30)) as r:
            r.raise_for_status()
            with open(self.tmp, "wb") as f:
                for chunk in r.iter_content(CHUNK):
                    if self.stop.is_set():
                        return
                    LIMITER.acquire(len(chunk))
                    f.write(chunk)
                    self.single_done += len(chunk)
                    self._count(len(chunk))
        if not self.size:
            self.size = self.single_done

    def _segmented(self):
        if not self.segments:
            n = max(1, min(self.connections, self.size // MIN_SPLIT))
            part = self.size // n
            self.segments = [
                [i * part, (i + 1) * part - 1 if i < n - 1 else self.size - 1, i * part]
                for i in range(n)
            ]
        if not os.path.exists(self.tmp) or os.path.getsize(self.tmp) != self.size:
            with open(self.tmp, "ab") as f:
                f.truncate(self.size)
        self.claimed = set()
        unfinished = sum(1 for s in self.segments if s[2] <= s[1])
        workers = [threading.Thread(target=self._worker, daemon=True)
                   for _ in range(max(1, min(self.connections, unfinished)))]
        for t in workers:
            t.start()
        for t in workers:
            t.join()

    def _count(self, n):
        with self.lock:
            self.counter += n

    def _next_segment(self):
        with self.lock:
            for seg in self.segments:
                if seg[2] <= seg[1] and id(seg) not in self.claimed:
                    self.claimed.add(id(seg))
                    return seg
            best, rem = None, 0
            for seg in self.segments:
                r = seg[1] - seg[2] + 1
                if r > rem:
                    best, rem = seg, r
            if best is not None and rem >= 2 * MIN_SPLIT:
                mid = best[2] + rem // 2
                new = [mid, best[1], mid]
                best[1] = mid - 1
                self.segments.append(new)
                self.claimed.add(id(new))
                return new
        return None

    def _worker(self):
        f = None
        try:
            f = open(self.tmp, "r+b")
            s = requests.Session()
            while not self.stop.is_set():
                seg = self._next_segment()
                if seg is None:
                    break
                self._fetch(s, f, seg)
        except Exception as e:  # noqa: BLE001
            if not self.stop.is_set():
                self.failed = e
                self.stop.set()
        finally:
            if f:
                f.close()

    def _fetch(self, session, f, seg):
        tries = 0
        while seg[2] <= seg[1] and not self.stop.is_set():
            before = seg[2]
            try:
                r = session.get(
                    self.url, stream=True, timeout=(10, 20),
                    headers=self.headers({"Range": f"bytes={seg[2]}-{seg[1]}"}))
                if r.status_code != 206:
                    r.close()
                    raise IOError(f"HTTP {r.status_code}")
                f.seek(seg[2])
                for chunk in r.iter_content(CHUNK):
                    if self.stop.is_set():
                        break
                    n = min(len(chunk), seg[1] - seg[2] + 1)
                    if n <= 0:
                        break
                    LIMITER.acquire(n)
                    f.write(chunk if n == len(chunk) else chunk[:n])
                    seg[2] += n
                    self._count(n)
                    if seg[2] > seg[1]:
                        break
                r.close()
                tries = tries + 1 if seg[2] == before else 0
                if tries > RETRIES:
                    raise IOError("Server sent no data")
            except Exception:  # noqa: BLE001
                tries += 1
                if tries > RETRIES:
                    raise
                self.stop.wait(min(2 * tries, 8))
        f.flush()


# ---------------------------------------------------------------------------
VIDEO_FORMATS = ["Best quality", "1080p", "720p", "480p", "Audio only (MP3)"]


def _video_opts(fmt, have_ffmpeg):
    if fmt.startswith("Audio"):
        if not have_ffmpeg:
            raise IOError("MP3 conversion needs ffmpeg installed")
        return {"format": "ba/b", "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]}
    h = {"1080p": 1080, "720p": 720, "480p": 480}.get(fmt)
    if have_ffmpeg:
        sel = f"bv*[height<={h}]+ba/b[height<={h}]/b" if h else "bv*+ba/b"
    else:  # no ffmpeg: only single-file formats can be used
        sel = f"b[height<={h}]/b" if h else "b"
    return {"format": sel}


class VideoDownload(Download):
    """Video/audio from YouTube and ~1000 other sites, powered by yt-dlp."""
    kind = "video"

    def __init__(self, *a, fmt="Best quality", **k):
        super().__init__(*a, **k)
        self.fmt = fmt

    @property
    def category(self):
        return "Music" if self.fmt.startswith("Audio") else "Video"

    def _run(self):
        try:
            import yt_dlp
            from yt_dlp.utils import DownloadCancelled
        except ImportError:
            self.status, self.error = "error", "yt-dlp is not installed (pip install yt-dlp)"
            return

        files = {}

        def hook(h):
            if self.stop.is_set():
                raise DownloadCancelled()
            info = h.get("info_dict") or {}
            if not self.name and info.get("title"):
                self.name = info["title"]
            key = h.get("filename") or h.get("tmpfilename") or ""
            done = h.get("downloaded_bytes") or 0
            total = h.get("total_bytes") or h.get("total_bytes_estimate") or done
            prev = files.get(key, (0, 0))[0]
            if done > prev:
                self._count(done - prev)
            files[key] = (done, total)
            self.single_done = sum(v[0] for v in files.values())
            self.size = max(self.size, sum(v[1] for v in files.values()))

        try:
            folder = Path(self.base) / (self.category if self.by_category else "")
            folder.mkdir(parents=True, exist_ok=True)
            opts = {
                "outtmpl": str(folder / "%(title).150B [%(id)s].%(ext)s"),
                "progress_hooks": [hook], "noplaylist": True, "quiet": True,
                "no_warnings": True, "continuedl": True, "retries": RETRIES,
                "fragment_retries": RETRIES,
                "concurrent_fragment_downloads": max(1, min(self.connections, 16)),
                "http_headers": {"User-Agent": UA},
            }
            opts.update(_video_opts(self.fmt, bool(shutil.which("ffmpeg"))))
            with yt_dlp.YoutubeDL(opts) as y:
                info = y.extract_info(self.url, download=True)
                if self.user_stop:
                    return
                reqs = (info or {}).get("requested_downloads") or []
                fp = reqs[0].get("filepath") if reqs else ""
                if not fp or not os.path.exists(fp):
                    fp = y.prepare_filename(info)
            self.path, self.name = fp, os.path.basename(fp)
            self.size = os.path.getsize(fp) if os.path.exists(fp) else self.size
            self.status = "done"
        except DownloadCancelled:
            return
        except Exception as e:  # noqa: BLE001
            if self.user_stop:
                return
            self.status = "error"
            self.error = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "").strip()[:200] \
                or e.__class__.__name__


def download_from_dict(d):
    cls = VideoDownload if d.get("kind") == "video" else Download
    o = cls.from_dict(d)
    if cls is VideoDownload:
        o.fmt = d.get("fmt") or o.fmt
    return o


# ---------------------------------------------------------------------------
def power_action(kind):
    """Run a power command: 'shutdown' | 'sleep'."""
    if sys.platform.startswith("win"):
        cmds = {"shutdown": ["shutdown", "/s", "/t", "0"],
                "sleep": ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"]}
    elif sys.platform == "darwin":
        cmds = {"shutdown": ["osascript", "-e", 'tell app "System Events" to shut down'],
                "sleep": ["pmset", "sleepnow"]}
    else:
        cmds = {"shutdown": ["systemctl", "poweroff"], "sleep": ["systemctl", "suspend"]}
    if kind in cmds:
        subprocess.Popen(cmds[kind])


class Scheduler:
    """Timed start/stop of the queue and an 'after everything finished' action."""

    def __init__(self, mgr):
        self.m = mgr
        self._last_key = None
        self._had_work = False

    def tick(self, now=None):
        ev = []
        now = now or datetime.now()
        hm = now.strftime("%H:%M")
        sc = self.m.s["schedule"]
        key = (now.date(), hm)
        if sc.get("enabled") and key != self._last_key:
            if sc.get("start") == hm:
                self.m.start_all()
                ev.append("start")
            if sc.get("stop") and sc["stop"] == hm:
                self.m.pause_all()
                ev.append("stop")
            self._last_key = key
        busy = any(d.status in ("downloading", "queued") for d in self.m.items)
        if busy:
            self._had_work = True
        elif self._had_work:
            self._had_work = False
            if self.m.when_done != "none":
                ev.append("finished")
        return ev


# ---------------------------------------------------------------------------
class Manager:
    """Owns the download list, the queue, settings and persistence."""

    def __init__(self):
        APP_DIR.mkdir(parents=True, exist_ok=True)
        self.s = dict(DEFAULT_SETTINGS)
        try:
            saved = json.loads(SETTINGS_FILE.read_text("utf-8"))
            sched = {**DEFAULT_SETTINGS["schedule"], **saved.pop("schedule", {})}
            self.s.update(saved)
            self.s["schedule"] = sched
        except (OSError, ValueError, AttributeError):
            self.s["schedule"] = dict(DEFAULT_SETTINGS["schedule"])
        self.when_done = "none"   # session only: none | close | sleep | shutdown
        if not self.s["token"]:
            self.s["token"] = uuid.uuid4().hex
        self.items = []
        try:
            for d in json.loads(STATE_FILE.read_text("utf-8")):
                self.items.append(download_from_dict(d))
        except (OSError, ValueError, KeyError):
            pass
        self.apply_settings()

    def apply_settings(self):
        LIMITER.limit = int(self.s["speed_limit_kb"]) * 1024
        self.save_settings()

    def save_settings(self):
        self._atomic(SETTINGS_FILE, self.s)

    def save(self):
        self._atomic(STATE_FILE, [d.to_dict() for d in self.items])

    @staticmethod
    def _atomic(path, data):
        try:
            tmp = str(path) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError:
            pass

    def add(self, url, folder=None, connections=None, referer="", cookie="",
            name="", start=True):
        d = Download(url, folder or self.s["folder"], name, self.s["by_category"],
                     connections or self.s["connections"], referer, cookie)
        if not start:
            d.status = "paused"
        self.items.append(d)
        self.save()
        return d

    def add_video(self, url, fmt="Best quality", folder=None, start=True):
        d = VideoDownload(url, folder or self.s["folder"], "", self.s["by_category"],
                          self.s["connections"], fmt=fmt)
        if not start:
            d.status = "paused"
        self.items.append(d)
        self.save()
        return d

    def start_all(self):
        for d in self.items:
            if d.status in ("paused", "error"):
                d.status, d.error = "queued", ""

    def pause_all(self):
        for d in self.items:
            if d.status in ("downloading", "queued"):
                d.pause()

    def pump(self):
        active = sum(1 for d in self.items if d.status == "downloading")
        for d in self.items:
            if active >= int(self.s["max_active"]):
                break
            if d.status == "queued" and not (d.worker and d.worker.is_alive()):
                d.start()
                active += 1

    def remove(self, d, delete_file=False):
        d.discard(delete_file)
        if d in self.items:
            self.items.remove(d)
        self.save()

    def shutdown(self):
        for d in self.items:
            if d.status in ("downloading", "queued"):
                d.pause()
        deadline = time.time() + 3
        for d in self.items:
            if d.worker and d.worker.is_alive():
                d.worker.join(max(0, deadline - time.time()))
        self.save()
        self.save_settings()
