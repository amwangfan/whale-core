"""Private, bounded media artifacts. URLs are downloaded with SSRF peer checks."""

from __future__ import annotations
import base64
import os
import time
import uuid
import urllib.request
from pathlib import Path
from whale_core.plugins.web_fetch import WebFetcher, _read_limited

MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
ALLOWED_MEDIA = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "audio/mpeg",
    "audio/wav",
    "audio/ogg",
    "audio/webm",
    "video/mp4",
}


class ArtifactStore:
    def __init__(self, store, directory):
        self.store, self.directory = store, Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def save(self, data, media_type, *, ttl_s=7 * 86400):
        if (
            media_type not in ALLOWED_MEDIA
            or not data
            or len(data) > MAX_ARTIFACT_BYTES
        ):
            raise ValueError("unsupported media or artifact size")
        aid = uuid.uuid4().hex
        path = self.directory / aid
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(data)
        meta = {
            "artifact_id": aid,
            "media_type": media_type,
            "size": len(data),
            "created_at": time.time(),
            "expires_at": time.time() + ttl_s,
        }
        self.store.put("artifact:" + aid, meta)
        return meta

    def capture(self, reference):
        if not isinstance(reference, str):
            raise ValueError("expected media reference")
        if reference.startswith("data:"):
            head, sep, raw = reference.partition(",")
            if (
                not sep
                or not head.endswith(";base64")
                or len(raw) > MAX_ARTIFACT_BYTES * 4 // 3 + 4
            ):
                raise ValueError("invalid data reference")
            return self.save(base64.b64decode(raw, validate=True), head[5:-7])
        # No credentials are forwarded, no redirects followed. Fetcher verifies
        # DNS and actual socket peers, so a media URL cannot read a private host.
        fetcher = WebFetcher(timeout=20)
        fetcher._check_url(reference)
        with fetcher._opener.open(
            urllib.request.Request(reference), timeout=20
        ) as response:
            media = response.headers.get("Content-Type", "").split(";")[0].lower()
            data = _read_limited(
                response, MAX_ARTIFACT_BYTES + 1, time.monotonic() + 60
            )
        return self.save(data, media)

    def read(self, aid):
        if len(aid) != 32 or any(x not in "0123456789abcdef" for x in aid):
            raise KeyError(aid)
        meta = self.store.get("artifact:" + aid)
        if not meta or meta["expires_at"] < time.time():
            raise KeyError(aid)
        return meta, (self.directory / aid).read_bytes()

    def cleanup(self):
        count = 0
        for key in self.store.keys():
            if key.startswith("artifact:"):
                meta = self.store.get(key)
                if isinstance(meta, dict) and meta.get("expires_at", 0) < time.time():
                    aid = key[9:]
                    if len(aid) == 32 and all(x in "0123456789abcdef" for x in aid):
                        (self.directory / aid).unlink(missing_ok=True)
                        self.store.delete(key)
                        count += 1
        return count
