"""Container liveness check; reads the configured token without printing it."""
from pathlib import Path
import os
import sys
import urllib.request

try:
    token = os.environ.get("WHALE_API_TOKEN", "").strip()
    if not token:
        token = (Path("/data/whale") / "api_token").read_text().strip()
    request = urllib.request.Request("http://127.0.0.1:8080/api/v1/health",
                                     headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(request, timeout=5) as response:
        sys.exit(0 if response.status == 200 else 1)
except Exception:
    sys.exit(1)
