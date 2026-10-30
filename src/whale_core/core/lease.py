"""One job host per configuration directory (Linux/macOS advisory lock)."""

import os
from pathlib import Path


class RuntimeBusy(RuntimeError):
    pass


class RuntimeLease:
    def __init__(self, directory):
        import fcntl

        path = Path(directory).expanduser()
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.fd = os.open(path / ".runtime.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self.fd)
            self.fd = None
            raise RuntimeBusy(
                "Another runtime is using this config directory; stop it before restore or a second host"
            ) from exc

    def close(self):
        if self.fd is not None:
            import fcntl

            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
