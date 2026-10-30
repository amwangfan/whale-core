"""Private, bounded media storage. API access must retain owner authentication."""
from __future__ import annotations

import base64
import hashlib
import os
import math
import threading
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from whale_core.plugins.web_fetch import WebFetcher, _get_header, _read_limited

_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
          "image/gif": ".gif", "audio/mpeg": ".mp3", "audio/wav": ".wav",
          "audio/x-wav": ".wav", "audio/ogg": ".ogg", "video/mp4": ".mp4",
          "audio/webm": ".webm"}
_ID = re.compile(r"^[a-f0-9]{32}$")


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    content_type: str
    size_bytes: int
    sha256: str
    created_at: float
    expires_at: float

    @property
    def url(self) -> str:
        return "/api/v1/artifacts/" + self.artifact_id

    def as_dict(self) -> dict:
        return {**asdict(self), "url": self.url}


class ArtifactStore:
    def __init__(self, directory: str | Path, store, *, max_bytes: int = 100*1024*1024,
                 retention_seconds: float = 7*86400, fetcher: WebFetcher | None = None):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self._lock = threading.RLock()
        self.max_total_bytes = 512*1024*1024
        self.store = store
        self.max_bytes = int(max_bytes)
        self.retention_seconds = float(retention_seconds)
        if self.max_bytes <= 0 or not math.isfinite(self.retention_seconds) or self.retention_seconds <= 0:
            raise ValueError("artifact limits must be positive")
        self._fetcher = fetcher or WebFetcher(timeout=30, max_bytes=self.max_bytes+1)

    def save_bytes(self, data: bytes, content_type: str) -> Artifact:
        with self._lock:
            return self._save_bytes(data, content_type)

    def _save_bytes(self, data: bytes, content_type: str) -> Artifact:
        content_type = content_type.split(";", 1)[0].strip().lower()
        if content_type not in _TYPES:
            raise ValueError("unsupported artifact content type")
        if not data or len(data) > self.max_bytes:
            raise ValueError("artifact is empty or exceeds size limit")
        self.cleanup()
        total = sum(int((self.store.get(k) or {}).get("size_bytes", 0)) for k in self.store.keys()
                    if k.startswith("artifact:") and isinstance(self.store.get(k), dict))
        if total + len(data) > self.max_total_bytes:
            raise ValueError("artifact storage quota exceeded")
        now = time.time()
        artifact = Artifact(uuid4().hex, content_type, len(data), hashlib.sha256(data).hexdigest(),
                            now, now+self.retention_seconds)
        path = self.directory / (artifact.artifact_id + _TYPES[content_type])
        # Exclusive creation prevents following attacker-controlled symlinks.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            self.store.put("artifact:" + artifact.artifact_id, asdict(artifact))
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return artifact

    def save_reference(self, reference: str, *, content_type: str = "") -> Artifact:
        if reference.startswith("data:"):
            header, sep, encoded = reference.partition(",")
            if not sep or not header.endswith(";base64") or len(encoded) > self.max_bytes*4//3+8:
                raise ValueError("invalid or oversized media data URL")
            mime = header[5:-7]
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError) as exc:
                raise ValueError("invalid media base64") from exc
            return self.save_bytes(data, mime)
        current = reference
        deadline = time.monotonic()+120
        for _ in range(4):
            # Reuse the web fetcher's DNS + peer checks, including every redirect.
            self._fetcher._check_url(current)
            parts = urllib.parse.urlsplit(current)
            if parts.username or parts.password:
                raise ValueError("artifact URL cannot contain credentials")
            req = urllib.request.Request(current, headers={"Accept-Encoding": "identity"})
            try:
                response = self._fetcher._opener.open(req, timeout=30)
            except urllib.error.HTTPError as exc:
                if exc.code in (301, 302, 303, 307, 308) and exc.headers.get("Location"):
                    current = urllib.parse.urljoin(current, exc.headers["Location"])
                    continue
                raise ValueError("artifact download failed") from None
            try:
                mime = _get_header(response.headers, "Content-Type").split(";", 1)[0]
                if content_type and mime != content_type:
                    raise ValueError("artifact content type mismatch")
                data = _read_limited(response, self.max_bytes+1, deadline)
            finally:
                response.close()
            return self.save_bytes(data, mime)
        raise ValueError("too many artifact redirects")

    def get(self, artifact_id: str) -> Artifact:
        if not _ID.fullmatch(artifact_id):
            raise KeyError("artifact not found")
        data = self.store.get("artifact:" + artifact_id)
        if not isinstance(data, dict):
            raise KeyError("artifact not found")
        try:
            item = Artifact(**data)
        except (TypeError, ValueError):
            raise KeyError("invalid artifact metadata") from None
        if item.artifact_id != artifact_id or not _ID.fullmatch(item.artifact_id):
            raise KeyError("invalid artifact metadata")
        if item.expires_at <= time.time() or item.content_type not in _TYPES:
            raise KeyError("artifact expired")
        return item

    def read(self, artifact_id: str) -> tuple[Artifact, bytes]:
        item = self.get(artifact_id)
        path = self.directory / (item.artifact_id + _TYPES[item.content_type])
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            data = stream.read(self.max_bytes+1)
        if len(data) != item.size_bytes or hashlib.sha256(data).hexdigest() != item.sha256:
            raise ValueError("artifact integrity check failed")
        return item, data

    def cleanup(self) -> int:
        count = 0
        for key in self.store.keys():
            if not key.startswith("artifact:"):
                continue
            raw = self.store.get(key)
            if not isinstance(raw, dict) or raw.get("expires_at", 0) > time.time():
                continue
            aid, mime = key.removeprefix("artifact:"), raw.get("content_type")
            if _ID.fullmatch(aid) and mime in _TYPES:
                (self.directory / (aid+_TYPES[mime])).unlink(missing_ok=True)
                self.store.delete(key)
                count += 1
        return count
