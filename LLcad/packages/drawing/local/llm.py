"""Client for a local llama.cpp ``llama-server`` (OpenAI-compatible, JSON-schema output).

Starts or reuses one server, guards RAM and a cross-process lock, and offers a
narrow ``chat`` call with schema-constrained answers. Nothing here depends on a
particular model: everything model-specific lives in the config profile.

``llama.pid`` contract (JSON, written next to the logs when this client starts a server):
``{"pid": int, "started_by_runner": true, "name": "<lower-case exe name>" | null,
"created": int | null}``. ``created`` is the RAW process creation time: on Windows the
FILETIME integer (100 ns ticks since 1601-01-01, exceeds 2^53 -> read it as BigInt/string
in JS, never as a double); on Linux the start tick from /proc/<pid>/stat field 22.
Lock file: an OS lock on the open file; the OS frees it when the holding process dies. On Windows
(msvcrt.locking) it covers one byte beyond the info block, on POSIX (flock) the whole file.
Content ``{"pid", "started"}`` is informative.
"""
from __future__ import annotations

import base64
import errno
import http.client
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

IS_WIN = sys.platform == "win32"
DEFAULT_CTX = 16384
DEFAULT_ARGS = ["-ngl", "99", "-fa", "on", "-np", "1"]
OPTIONAL_FLAGS = [("--cache-ram", "0"), ("--reasoning-budget", "0")]
_FLAG_CACHE: dict = {}


class ModelBusy(Exception):
    """The local model cannot be used right now (message starts with 'local model busy')."""


# ---------------------------------------------------------------- profiles

def active_profile(config: dict) -> dict:
    """Resolve ``drawing-config.json -> local_model`` to one profile dict (also accepts the flat form)."""
    if "profiles" in config:
        name = config.get("active")
        profiles = config["profiles"]
        if name is None and len(profiles) == 1:
            name = next(iter(profiles))
        if name not in profiles:
            raise KeyError(f"unknown model profile: {name!r}")
        prof = dict(profiles[name])
        prof.setdefault("name", name)
        return prof
    prof = dict(config)
    prof.setdefault("name", "default")
    return prof


# ---------------------------------------------------------------- system helpers

EMERGENCY_FREE_GB = 2.5   # below this limit the own llama-server is stopped immediately (emergency stop)


def free_ram_gb() -> float:
    if IS_WIN:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]

        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        return st.ullAvailPhys / 1024 ** 3
    with open("/proc/meminfo") as fh:
        for line in fh:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024 ** 2
    return 0.0


def process_identity(pid: int) -> Optional[tuple]:
    """(process name lower-case, creation time) of a living process, else None."""
    if IS_WIN:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return None
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != 259:
                return None
            c, e, k, u = (wintypes.FILETIME() for _ in range(4))
            if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
                return None
            created = (c.dwHighDateTime << 32) | c.dwLowDateTime
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            name = ""
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                name = os.path.basename(buf.value).lower()
            return name, created
        finally:
            k32.CloseHandle(h)
    try:
        with open(f"/proc/{int(pid)}/stat") as fh:
            stat = fh.read()
        name = stat[stat.index("(") + 1:stat.rindex(")")].lower()
        start = int(stat[stat.rindex(")") + 2:].split()[19])
        return name, start
    except (OSError, ValueError):
        return None


LOCK_OFFSET = 1 << 20
_CONTENDED = {errno.EACCES, errno.EAGAIN, errno.EDEADLK, getattr(errno, "EDEADLOCK", errno.EDEADLK),
              errno.EWOULDBLOCK}
PROBE_TIMEOUT = 5.0  # Windows needs ~2 s to refuse a closed localhost port: keep a clear margin


def _os_lock(fd: int) -> bool:
    """Non-blocking exclusive OS lock (one byte on Windows, whole file on POSIX); freed by the OS on
    process death. False = held by another process; permanent errors raise OSError."""
    try:
        if IS_WIN:
            import msvcrt
            os.lseek(fd, LOCK_OFFSET, 0)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as e:
        if e.errno in _CONTENDED:
            return False
        raise


def _os_unlock(fd: int) -> None:
    try:
        if IS_WIN:
            import msvcrt
            os.lseek(fd, LOCK_OFFSET, 0)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def _help_text(server: str) -> str:
    try:
        r = subprocess.run([server, "--help"], capture_output=True, text=True, timeout=60,
                           encoding="utf-8", errors="replace",
                           creationflags=subprocess.CREATE_NO_WINDOW if IS_WIN else 0)
        return (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.SubprocessError):
        return ""


def optional_flags(server: str) -> list:
    """Extra start flags, only those the configured binary lists in ``--help`` (checked once)."""
    if server not in _FLAG_CACHE:
        text = _help_text(server)
        out: list = []
        for flag, val in OPTIONAL_FLAGS:
            if flag in text:
                out += [flag, val]
        _FLAG_CACHE[server] = out
    return list(_FLAG_CACHE[server])


def image_part(png: bytes) -> dict:
    b64 = base64.b64encode(png).decode("ascii")
    return {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}}


def _http_json(url: str, timeout: float, body: Optional[dict] = None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _size_gb(path: Optional[str]) -> float:
    try:
        return os.path.getsize(path) / 1024 ** 3 if path else 0.0
    except OSError:
        return 0.0


# ---------------------------------------------------------------- client

class LocalModel:
    def __init__(self, cfg: dict, work_dir: Path, lock_path: Path,
                 on_wait: Callable[[str], None] = lambda m: None,
                 wait_s: float = 1800, min_free_gb: Optional[float] = None,
                 deadline: Optional[float] = None, watch_interval: float = 1.0,
                 emergency_free_gb: float = EMERGENCY_FREE_GB):
        self.cfg = cfg
        self.hard_deadline = deadline        # absolute deadline (time.time()) for lock wait time, server start and calls
        self.watch_interval = float(watch_interval)
        self.emergency_free_gb = float(emergency_free_gb)
        self.emergency: Optional[str] = None  # text of the emergency stop, otherwise None
        self.min_free_seen: Optional[float] = None
        self._watch_thread = None
        self._watch_stop = threading.Event()
        self._proc_lock = threading.RLock()
        self.work_dir = Path(work_dir)
        self.lock_path = Path(lock_path)
        self.on_wait = on_wait
        self.wait_s = wait_s
        if min_free_gb is None:
            min_free_gb = cfg.get("min_free_gb")
        if min_free_gb is None:
            min_free_gb = _size_gb(cfg.get("model")) + _size_gb(cfg.get("mmproj")) + 2.0
        self.min_free_gb = float(min_free_gb)
        self.stats = {"calls": 0, "failures": 0, "seconds": 0.0}
        self.available = True
        self.base_url = ""
        self._proc = None
        self._started_identity = None
        self._lock_fd = None
        self._consecutive = 0
        self._deadline = 0.0

    # -- lifecycle
    def __enter__(self):
        self._deadline = time.time() + self.wait_s  # one deadline for lock and port wait together
        if self.hard_deadline is not None:
            self._deadline = min(self._deadline, self.hard_deadline)
        self._acquire_lock()
        try:
            self._attach_or_start()
        except BaseException:
            try:
                self._stop_watchdog()
                self._stop_own_server()
            finally:
                self._release_lock()
            raise
        return self

    def __exit__(self, *exc):
        try:
            self._stop_watchdog()
            self._stop_own_server()
        finally:
            self._release_lock()
        return False

    # -- RAM watchdog: kills the OWN server when free RAM drops below emergency_free_gb
    def _start_watchdog(self):
        if self._watch_thread is not None:
            return
        self._watch_stop = threading.Event()
        stop = self._watch_stop

        def run():
            while True:
                try:
                    free = free_ram_gb()
                except Exception:
                    free = None
                if free is not None:
                    if self.min_free_seen is None or free < self.min_free_seen:
                        self.min_free_seen = free
                    if free < self.emergency_free_gb:
                        self._emergency_stop(free)
                        return
                if stop.wait(self.watch_interval):
                    return

        self._watch_thread = threading.Thread(target=run, name="llm-ram-watch", daemon=True)
        self._watch_thread.start()

    def _stop_watchdog(self):
        t, self._watch_thread = self._watch_thread, None
        self._watch_stop.set()
        if t is not None and t is not threading.current_thread():
            t.join(timeout=5)

    def _emergency_stop(self, free: float):
        with self._proc_lock:
            self.emergency = f"Emergency stop: free RAM {free:.1f} GB < {self.emergency_free_gb:.1f} GB"
            self.available = False
            proc = self._proc
            if proc is None or proc.poll() is not None:
                return
            recorded = self._started_identity
            if recorded is not None and process_identity(proc.pid) != recorded:
                return  # not the process we started (reused PID): never touch it
            try:
                proc.kill()
                proc.wait(timeout=20)
            except Exception:
                pass

    # -- lock (OS lock on the open handle; released automatically when the process dies)
    def _acquire_lock(self):
        deadline = self._deadline
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise ModelBusy(f"local model busy (lock file not usable: {e})")
        while True:
            fd = None
            try:
                fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR)
                if _os_lock(fd):
                    os.lseek(fd, 0, 0)
                    data = json.dumps({"pid": os.getpid(), "started": time.time()}).encode()
                    os.write(fd, data)
                    os.ftruncate(fd, len(data))
                    self._lock_fd = fd
                    return
            except OSError as e:  # permanent problem (permissions, directory, disk full): no waiting
                if fd is not None:
                    os.close(fd)
                raise ModelBusy(f"local model busy (lock file not usable: {e})")
            os.close(fd)
            if time.time() >= deadline:
                raise ModelBusy("local model busy (lock held by another run)")
            self.on_wait("waiting for local model …")
            time.sleep(max(0.0, min(5.0, deadline - time.time())))

    def _release_lock(self):
        fd, self._lock_fd = self._lock_fd, None
        if fd is None:
            return
        _os_unlock(fd)
        os.close(fd)

    # -- server
    @staticmethod
    def _classify(exc: BaseException):
        """Map a failed probe request: None = nothing listens (connection refused),
        {"loading": True} = timeout or 503 (wait), {"foreign": True} = anything else answered/accepted."""
        if isinstance(exc, urllib.error.HTTPError):
            return {"loading": True} if exc.code == 503 else {"foreign": True}
        if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, BaseException):
            exc = exc.reason
        if isinstance(exc, ConnectionRefusedError):
            return None
        if isinstance(exc, TimeoutError):
            return {"loading": True}
        return {"foreign": True}

    def _probe(self, port: int):
        """None = nothing listening; {"loading": True} = timeout/503 (wait); {"foreign": True} =
        something else answers; else dict with url/ids/vision of a model server."""
        url = f"http://127.0.0.1:{port}"
        try:
            models = _http_json(url + "/v1/models", PROBE_TIMEOUT)
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:
            return self._classify(e)
        try:
            ids = [str(m.get("id", "")) for m in models.get("data", [])]
            ids += [str(m.get(k, "")) for m in models.get("models", []) for k in ("name", "model")]
        except (AttributeError, TypeError):
            return {"foreign": True}
        try:
            props = _http_json(url + "/props", PROBE_TIMEOUT)
            vision = bool(props.get("modalities", {}).get("vision"))
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError, AttributeError):
            vision = False
        return {"url": url, "ids": ids, "vision": vision}

    def _attach_or_start(self):
        model_file = os.path.basename(str(self.cfg["model"]))
        stem = model_file.rsplit(".", 1)[0]
        need_vision = bool(self.cfg.get("mmproj"))
        ports = []
        for port in list(self.cfg.get("reuse_ports", [])) + [self.cfg["port"]]:
            if port not in ports:
                ports.append(port)
        own = self.cfg["port"]
        deadline = self._deadline
        while True:
            loading = None
            for port in ports:
                info = self._probe(port)
                if info is None:
                    continue
                if info.get("foreign"):
                    if port == own:
                        raise ModelBusy(f"local model busy (port {port}: foreign service)")
                    continue  # foreign occupant of a reuse port: not usable, try the next port
                if info.get("loading"):
                    loading = port if loading is None else loading
                    continue
                same = any(model_file in i or stem in i for i in info["ids"])
                if same and (info["vision"] or not need_vision):
                    self.base_url = info["url"]
                    return
                raise ModelBusy(f"local model busy (port {port}: other model or without image understanding)")
            if loading is None:
                break
            if time.time() >= deadline:
                raise ModelBusy(f"local model busy (port {loading}: server not clearly ready)")
            self.on_wait("waiting for local model …")
            time.sleep(max(0.0, min(5.0, deadline - time.time())))
        free = free_ram_gb()
        if free < self.min_free_gb:
            raise ModelBusy(f"local model busy (free RAM {free:.1f} GB < {self.min_free_gb:.1f} GB)")
        self._start_server()

    def _start_server(self):
        cfg = self.cfg
        port = cfg["port"]
        ctx = cfg.get("ctx", DEFAULT_CTX)
        args = list(cfg.get("args", DEFAULT_ARGS))
        cmd = [cfg["server"], "-m", cfg["model"]]
        if cfg.get("mmproj"):
            cmd += ["--mmproj", cfg["mmproj"]]
        cmd += ["--host", "127.0.0.1", "--port", str(port), "-c", str(ctx), "--jinja"]
        cmd += args
        extra = optional_flags(cfg["server"])
        for i in range(0, len(extra), 2):
            if extra[i] not in args:
                cmd += extra[i:i + 2]
        self.work_dir.mkdir(parents=True, exist_ok=True)
        out = open(self.work_dir / "llama.out.log", "wb")
        err = open(self.work_dir / "llama.err.log", "wb")
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if IS_WIN else 0)
        except OSError as e:
            raise ModelBusy(f"local model busy (start failed: {e})")
        finally:
            out.close()
            err.close()
        ident = process_identity(self._proc.pid)
        self._started_identity = ident
        (self.work_dir / "llama.pid").write_text(json.dumps({
            "pid": self._proc.pid, "started_by_runner": True,
            "name": ident[0] if ident else None, "created": ident[1] if ident else None}),
            encoding="utf-8")
        self.base_url = f"http://127.0.0.1:{port}"
        self._start_watchdog()
        start_s = 180.0
        if self.hard_deadline is not None:
            start_s = max(1.0, min(start_s, self.hard_deadline - time.time()))
        deadline = time.time() + start_s
        while time.time() < deadline:
            if self.emergency:
                self._stop_own_server()
                raise ModelBusy(f"local model busy ({self.emergency})")
            if self._proc.poll() is not None:
                self._cleanup_pid_file()
                self._proc = None
                raise ModelBusy("local model busy (server exited during start, see llama.err.log)")
            try:
                if _http_json(self.base_url + "/health", 3).get("status") == "ok":
                    return
            except (urllib.error.URLError, OSError, ValueError):
                pass
            time.sleep(1.0)
        self._stop_own_server()
        raise ModelBusy(f"local model busy (server not ready after {start_s:.0f} s)")

    def _cleanup_pid_file(self):
        try:
            (self.work_dir / "llama.pid").unlink()
        except OSError:
            pass

    def _stop_own_server(self):
        with self._proc_lock:
            self._stop_own_server_locked()

    def _stop_own_server_locked(self):
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is not None:
            self._proc = None
            self._cleanup_pid_file()
            return
        recorded = self._started_identity
        if recorded is not None and process_identity(proc.pid) != recorded:
            if proc.poll() is not None:  # ended between poll() and the identity check
                self._proc = None
                self._cleanup_pid_file()
            return  # otherwise not the process we started (reused PID): never touch it
        try:
            proc.terminate()  # via the held handle, immune to PID reuse
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=20)
        except Exception:
            return  # could not stop: keep llama.pid as evidence
        self._proc = None
        self._cleanup_pid_file()

    def _server_dead(self) -> bool:
        return self._proc is not None and self._proc.poll() is not None

    # -- chat
    def chat(self, messages: list, schema: dict, *, name: str, temperature: float = 0.2,
             max_tokens: int = 800, timeout: float = 120) -> Optional[dict]:
        if not self.available:
            return None
        if self._server_dead():
            self.available = False
            return None
        body = dict(self.cfg.get("options") or {})
        body.update({
            "messages": messages, "temperature": temperature, "max_tokens": max_tokens,
            "stream": False,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": name, "schema": schema, "strict": True}},
        })
        self.stats["calls"] += 1
        t0 = time.time()
        result = None
        for attempt in range(2):
            try:
                resp = _http_json(self.base_url + "/v1/chat/completions", timeout, body)
            except (urllib.error.URLError, OSError, TimeoutError):
                if self._server_dead():
                    break
                continue
            except ValueError:
                break
            try:
                result = json.loads(resp["choices"][0]["message"]["content"])
            except (KeyError, IndexError, TypeError, ValueError):
                result = None
            break
        self.stats["seconds"] += time.time() - t0
        if isinstance(result, dict):
            self._consecutive = 0
            return result
        self.stats["failures"] += 1
        self._consecutive += 1
        if self._server_dead() or self._consecutive >= 3:
            self.available = False
        return None

    def chat_choice(self, messages: list, *, temperature: float = 0.0, timeout: float = 60.0) -> Optional[dict]:
        """Pairwise comparison "A" or "B" at a fixed temperature (default 0.0) with logprobs (if supported by the server).
        Returns: {'choice': 'A'|'B', 'logprobs': {'A': float, 'B': float}, 'raw': str} or None on error."""
        if not self.available:
            return None
        if self._server_dead():
            self.available = False
            return None
        schema = {
            "type": "object",
            "properties": {"choice": {"type": "string", "enum": ["A", "B"]}},
            "required": ["choice"],
            "additionalProperties": False,
        }
        base_body = dict(self.cfg.get("options") or {})
        base_body.update({
            "messages": messages,
            "temperature": temperature,
            "max_tokens": 24,
            "stream": False,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "layout_choice", "schema": schema, "strict": True}},
        })
        self.stats["calls"] += 1
        t0 = time.time()
        resp = None
        t_end = t0 + float(timeout)          # one call stays within a single timeout overall (remaining budget), never 2x
        for with_lp in (True, False):
            body = dict(base_body)
            if with_lp:
                body["logprobs"] = True
                body["top_logprobs"] = 5
            try:
                resp = _http_json(self.base_url + "/v1/chat/completions", max(0.5, t_end - time.time()), body)
                break
            except urllib.error.HTTPError as e:
                if with_lp and e.code == 400 and time.time() < t_end:
                    continue  # server rejects logprobs -> one-time fallback without logprobs
                break
            except (urllib.error.URLError, OSError, TimeoutError, ValueError):
                break  # timeout/connection error: no retry (hard budget)
        self.stats["seconds"] += time.time() - t0
        if not isinstance(resp, dict):
            self.stats["failures"] += 1
            self._consecutive += 1
            if self._server_dead() or self._consecutive >= 3:
                self.available = False
            return None
        try:
            ch0 = resp["choices"][0]
            raw = str(ch0["message"].get("content") or "").strip()
        except (KeyError, IndexError, TypeError):
            raw, ch0 = "", {}
        choice = None
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and parsed.get("choice") in ("A", "B"):
                choice = parsed["choice"]
        except ValueError:
            pass
        if choice is None:
            for tok in raw.replace('"', ' ').replace('{', ' ').replace('}', ' ').split():
                if tok in ("A", "B"):
                    choice = tok
                    break
        if choice not in ("A", "B"):
            self.stats["failures"] += 1
            self._consecutive += 1
            return None
        lp_ab = {}
        lp_content = ((ch0.get("logprobs") or {}).get("content") or []) if isinstance(ch0, dict) else []
        for entry in lp_content:
            if not isinstance(entry, dict):
                continue
            top = entry.get("top_logprobs") or []
            found = {}
            for cand in top:
                tok = str(cand.get("token") or "").strip().strip('"')
                if tok in ("A", "B") and tok not in found:
                    found[tok] = round(float(cand.get("logprob")), 4)
            tok0 = str(entry.get("token") or "").strip().strip('"')
            if tok0 in ("A", "B") and tok0 not in found and entry.get("logprob") is not None:
                found[tok0] = round(float(entry.get("logprob")), 4)
            if found:
                lp_ab = found
                if tok0 in ("A", "B"):
                    break
        self._consecutive = 0
        return {"choice": choice, "logprobs": lp_ab, "raw": raw}
