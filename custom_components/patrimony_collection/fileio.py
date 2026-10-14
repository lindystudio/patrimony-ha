"""0.4.99: event-loop-safe file I/O for every Patrimony store.

Home Assistant flags blocking file calls (open/read_text/write_text/read_bytes/
sleep...) made on its event-loop thread. Patrimony stores are small JSON files
plus one photo, read by many request/snapshot paths. This module gives them:

* an in-memory, per-path cache (warmed off-loop at setup), so loop-thread reads
  never touch disk; a cheap os.stat signature notices external writers (the
  Supervisor add-on writes mapping.json) and refreshes in the executor;
* write-through, atomic writes (tmp + fsync + os.replace, 0600) serialized on
  ONE writer thread, so on-disk order always matches call order. Loop-thread
  callers don't wait (the cache already holds the new bytes); executor/test
  callers block until the bytes are durable.

Never logs file contents.
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import threading
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

WARM_MAX_BYTES = 8 * 1024 * 1024

_LOCK = threading.RLock()
_CACHE: dict[str, dict[str, Any]] = {}
_WRITER = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="patrimony_io")
_REFRESHING: set[str] = set()
_LOOP_THREAD: int | None = None


def set_loop_thread(ident: int | None) -> None:
    """Record HA's event-loop thread (called from async setup)."""
    global _LOOP_THREAD
    _LOOP_THREAD = ident


def on_loop() -> bool:
    return _LOOP_THREAD is not None and threading.get_ident() == _LOOP_THREAD


def reset_cache() -> None:
    """Tests only."""
    with _LOCK:
        _CACHE.clear()
        _REFRESHING.clear()


def _key(path) -> str:
    return os.fspath(path)


def _sig(path: str):
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _load_from_disk(path: str) -> None:
    """Blocking. Read path into the cache (None when missing)."""
    sig = _sig(path)
    data = None
    if sig is not None:
        with open(path, "rb") as fh:
            data = fh.read()
        sig = _sig(path)
    with _LOCK:
        e = _CACHE.get(path)
        if e is not None and e.get("pending"):
            return  # a newer in-process write wins
        _CACHE[path] = {"data": data, "sig": sig, "pending": 0}


def _refresh_bg(path: str) -> None:
    try:
        _load_from_disk(path)
    except Exception:
        _LOGGER.debug("patrimony_io_refresh_failed")
    finally:
        with _LOCK:
            _REFRESHING.discard(path)


def read_bytes(path) -> bytes | None:
    """Cached read. None when the file does not exist."""
    p = _key(path)
    with _LOCK:
        e = _CACHE.get(p)
        if e is not None and e.get("pending"):
            return e["data"]
    sig = _sig(p)
    with _LOCK:
        e = _CACHE.get(p)
        if e is not None and (e.get("pending") or e.get("sig") == sig):
            return e["data"]
        if on_loop():
            if e is not None or sig is None:
                # Stale (external writer) or missing: never block the loop.
                if sig is None:
                    _CACHE[p] = {"data": None, "sig": None, "pending": 0}
                    return None
                if p not in _REFRESHING:
                    _REFRESHING.add(p)
                    threading.Thread(target=_refresh_bg, args=(p,), daemon=True).start()
                return e["data"]
            _LOGGER.debug("patrimony_io_cold_read_on_loop")
    _load_from_disk(p)
    with _LOCK:
        return _CACHE[p]["data"]


def read_text(path) -> str | None:
    data = read_bytes(path)
    return None if data is None else data.decode("utf-8")


def exists(path) -> bool:
    return read_bytes(path) is not None


def _do_write(p: str, data: bytes | None, mode: int) -> None:
    try:
        if data is None:
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass
        else:
            path = Path(p)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, path)
                try:
                    os.chmod(path, mode)
                except OSError:
                    pass
            finally:
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass
    except Exception:
        _LOGGER.error("patrimony_io_write_failed")
        with _LOCK:
            e = _CACHE.get(p)
            if e is not None:
                e["pending"] = max(0, e.get("pending", 1) - 1)
                e["sig"] = ("failed",)  # force a re-read from disk next time
        raise
    with _LOCK:
        e = _CACHE.get(p)
        if e is not None:
            e["pending"] = max(0, e.get("pending", 1) - 1)
            if not e["pending"]:
                e["sig"] = _sig(p)


def _submit(p: str, data: bytes | None, mode: int) -> None:
    with _LOCK:
        e = _CACHE.setdefault(p, {"data": None, "sig": None, "pending": 0})
        e["data"] = data
        e["pending"] = e.get("pending", 0) + 1
    fut = _WRITER.submit(_do_write, p, data, mode)
    if not on_loop():
        fut.result()


def write_bytes(path, data: bytes, mode: int = 0o600) -> None:
    _submit(_key(path), bytes(data), mode)


def write_text(path, text: str, mode: int = 0o600) -> None:
    _submit(_key(path), text.encode("utf-8"), mode)


def delete(path) -> bool:
    p = _key(path)
    existed = read_bytes(p) is not None
    _submit(p, None, 0o600)
    return existed


def warm_paths(paths) -> int:
    """Blocking. Pre-load files (and every file under directories) into the cache."""
    n = 0
    for raw in paths:
        root = Path(raw)
        try:
            files = [f for f in root.rglob("*") if f.is_file()] if root.is_dir() else [root]
        except OSError:
            continue
        for f in files:
            if f.name.startswith(".") or f.name.endswith(".tmp"):
                continue
            try:
                if f.is_file() and f.stat().st_size > WARM_MAX_BYTES:
                    continue
                _load_from_disk(_key(f))
                n += 1
            except Exception:
                _LOGGER.debug("patrimony_io_warm_skip")
    return n


async def async_warm(hass, extra_paths=()) -> None:
    """Record the loop thread and warm the Patrimony data dir off-loop."""
    set_loop_thread(threading.get_ident())
    try:
        base = hass.config.path("patrimony_collection")
    except Exception:
        base = "/config/patrimony_collection"
    paths = [base, *extra_paths]
    runner = getattr(hass, "async_add_executor_job", None)
    try:
        if runner is not None:
            await runner(warm_paths, paths)
        else:
            warm_paths(paths)
    except Exception:
        _LOGGER.warning("patrimony_io_warm_failed")


def maybe_sleep(seconds: float) -> None:
    """Retry back-off that never sleeps on the event loop."""
    if not on_loop():
        import time

        time.sleep(seconds)
