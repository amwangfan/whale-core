"""One V2 contract locator for schema validation and capability lookup."""
import os
from pathlib import Path


def messages_dir() -> Path:
    override = os.environ.get('WHALE_CONTRACTS_DIR')
    if override:
        base = Path(override).expanduser()
        candidates = (base, base / 'messages', base / 'v2/messages')
    else:
        package = Path(__file__).resolve().parents[1]
        candidates = (package / 'contracts/v2/messages', *(
            parent / 'contracts/v2/messages' for parent in Path(__file__).resolve().parents))
    for candidate in candidates:
        if (candidate / 'device.hello.schema.json').is_file():
            return candidate.resolve()
    raise FileNotFoundError('V2 message schemas not found; set WHALE_CONTRACTS_DIR to contracts, v2 or messages')
