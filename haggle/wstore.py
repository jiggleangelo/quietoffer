import copy
import hashlib
import json
import os
import secrets
import shutil
import threading
import time
import weakref
from pathlib import Path

MAX_WORKSPACE_BYTES = 5 * 1024 * 1024
MAX_ITEMS = 200
MAX_LOGS = 100
MAX_WORKSPACES = 3000


class StoreError(Exception):
    def __init__(self, message, status=413):
        super().__init__(message)
        self.status = status


class _LockBox:
    def __init__(self):
        self.lock = threading.RLock()


class WorkspaceStore:
    def __init__(self, root, data_cap_bytes=200 * 1024 * 1024):
        self.root = Path(root)
        self.ws_root = self.root / "ws"
        self.tomb_root = self.root / "tomb"
        self.ws_root.mkdir(parents=True, exist_ok=True)
        self.tomb_root.mkdir(parents=True, exist_ok=True)
        self.data_cap_bytes = data_cap_bytes
        self.max_workspace_bytes = MAX_WORKSPACE_BYTES
        self._locks = weakref.WeakValueDictionary()
        self._locks_guard = threading.Lock()
        self._meta_guard = threading.Lock()
        self._usage_guard = threading.Lock()
        self._usage_bytes = self._scan_usage()
        self._workspace_count = sum(1 for path in self.ws_root.iterdir() if path.is_dir())

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def valid_token(token):
        return isinstance(token, str) and len(token) == 32 and all(c.isalnum() or c in "-_" for c in token)

    def _paths(self, token):
        digest = self.digest(token)
        return digest, self.ws_root / digest, self.tomb_root / digest

    def _lock(self, digest):
        with self._locks_guard:
            holder = self._locks.get(digest)
            if holder is None:
                holder = _LockBox()
                self._locks[digest] = holder
            return holder

    def _scan_usage(self):
        total = 0
        for base, _, files in os.walk(self.root):
            for name in files:
                try:
                    total += (Path(base) / name).stat().st_size
                except FileNotFoundError:
                    pass
        return total

    @staticmethod
    def _tree_size(path):
        total = 0
        if not path.exists():
            return 0
        for base, _, files in os.walk(path):
            for name in files:
                try:
                    total += (Path(base) / name).stat().st_size
                except FileNotFoundError:
                    pass
        return total

    @staticmethod
    def _encode(data):
        return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def _validate(data):
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise StoreError("Workspace data is invalid.", 400)
        if len(data["items"]) > MAX_ITEMS:
            raise StoreError("A workspace can hold at most 200 items.")
        for item in data["items"]:
            if not isinstance(item, dict) or not isinstance(item.get("log", []), list):
                raise StoreError("Workspace data is invalid.", 400)
            if len(item.get("log", [])) > MAX_LOGS:
                raise StoreError("An item can hold at most 100 log entries.")

    def _load_locked(self, directory, tombstone):
        if tombstone.exists() or not directory.is_dir():
            return None
        try:
            with open(directory / "data.json", encoding="utf-8") as handle:
                data = json.load(handle)
        except (FileNotFoundError, OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _write_locked(self, directory, tombstone, data):
        if tombstone.exists() or not directory.is_dir():
            raise StoreError("Workspace not found.", 404)
        self._validate(data)
        data["touched"] = int(time.time())
        payload = self._encode(data)
        if len(payload) > self.max_workspace_bytes:
            raise StoreError("This workspace has reached its 5 MB limit.")
        target = directory / "data.json"
        temporary = directory / ("data." + secrets.token_hex(6) + ".tmp")
        with self._usage_guard:
            try:
                old_size = target.stat().st_size
            except FileNotFoundError:
                old_size = 0
            if self._usage_bytes - old_size + len(payload) > self.data_cap_bytes:
                raise StoreError("Storage is full right now; export your data.")
            try:
                with open(temporary, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
                self._usage_bytes += len(payload) - old_size
                try:
                    descriptor = os.open(directory, os.O_RDONLY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                except OSError:
                    pass
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    def exists(self, token):
        if not self.valid_token(token):
            return False
        _, directory, tombstone = self._paths(token)
        return directory.is_dir() and not tombstone.exists()

    def create(self):
        with self._meta_guard:
            if self._workspace_count >= MAX_WORKSPACES:
                raise StoreError("Too many workspaces exist right now.", 429)
            with self._usage_guard:
                if self._usage_bytes >= self.data_cap_bytes:
                    raise StoreError("Storage is full right now; export your data.")
            self._workspace_count += 1
        directory = None
        try:
            while True:
                token = secrets.token_urlsafe(24)
                digest, directory, tombstone = self._paths(token)
                if not directory.exists() and not tombstone.exists():
                    break
            holder = self._lock(digest)
            with holder.lock:
                directory.mkdir(mode=0o700)
                now = int(time.time())
                self._write_locked(directory, tombstone, {"version": 1, "created": now, "touched": now, "items": []})
            return token
        except BaseException:
            if directory is not None:
                with self._usage_guard:
                    size = self._tree_size(directory)
                    shutil.rmtree(directory, ignore_errors=True)
                    self._usage_bytes = max(0, self._usage_bytes - size)
            with self._meta_guard:
                self._workspace_count = max(0, self._workspace_count - 1)
            raise

    def read(self, token):
        if not self.exists(token):
            return None
        digest, directory, tombstone = self._paths(token)
        holder = self._lock(digest)
        with holder.lock:
            data = self._load_locked(directory, tombstone)
            if data is None:
                return None
            try:
                os.utime(directory / "data.json", None)
            except OSError:
                pass
            return copy.deepcopy(data)

    def update(self, token, mutator):
        if not self.exists(token):
            raise StoreError("Workspace not found.", 404)
        digest, directory, tombstone = self._paths(token)
        holder = self._lock(digest)
        with holder.lock:
            data = self._load_locked(directory, tombstone)
            if data is None:
                raise StoreError("Workspace not found.", 404)
            result = mutator(data)
            self._write_locked(directory, tombstone, data)
            return result

    def save(self, token, data):
        return self.update(token, lambda current: current.clear() or current.update(copy.deepcopy(data)))

    def delete(self, token):
        if not self.valid_token(token):
            return False
        digest, directory, tombstone = self._paths(token)
        if not directory.exists() and tombstone.exists():
            return False
        holder = self._lock(digest)
        with holder.lock:
            existed = directory.is_dir()
            stamp = str(int(time.time())).encode("ascii")
            with self._usage_guard:
                old = self._tree_size(directory)
                try:
                    old += tombstone.stat().st_size
                except FileNotFoundError:
                    pass
                tombstone.parent.mkdir(parents=True, exist_ok=True)
                with open(tombstone, "wb") as handle:
                    handle.write(stamp)
                    handle.flush()
                    os.fsync(handle.fileno())
                shutil.rmtree(directory, ignore_errors=True)
                self._usage_bytes = max(0, self._usage_bytes - old + len(stamp))
            if existed:
                with self._meta_guard:
                    self._workspace_count = max(0, self._workspace_count - 1)
            return existed

    def sweep(self, now=None):
        now = int(time.time() if now is None else now)
        for directory in list(self.ws_root.iterdir()):
            if not directory.is_dir():
                continue
            holder = self._lock(directory.name)
            with holder.lock:
                try:
                    target = directory / "data.json"
                    with open(target, encoding="utf-8") as handle:
                        data = json.load(handle)
                    created = int(data.get("created", 0))
                    touched = max(int(data.get("touched", created)), int(target.stat().st_mtime))
                    empty_old = not data.get("items") and now - created >= 86400
                    idle = now - touched >= 90 * 86400
                    if empty_old or idle:
                        stamp = str(now).encode("ascii")
                        tombstone = self.tomb_root / directory.name
                        with self._usage_guard:
                            old = self._tree_size(directory)
                            tombstone.write_bytes(stamp)
                            shutil.rmtree(directory, ignore_errors=True)
                            self._usage_bytes = max(0, self._usage_bytes - old + len(stamp))
                        with self._meta_guard:
                            self._workspace_count = max(0, self._workspace_count - 1)
                except (OSError, ValueError, TypeError):
                    continue
        for tombstone in list(self.tomb_root.iterdir()):
            try:
                stamp = int(tombstone.read_text(encoding="ascii"))
                if now - stamp >= 7 * 86400:
                    size = tombstone.stat().st_size
                    tombstone.unlink()
                    with self._usage_guard:
                        self._usage_bytes = max(0, self._usage_bytes - size)
            except (OSError, ValueError):
                pass
