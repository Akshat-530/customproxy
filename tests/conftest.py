import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

# Tests never touch real providers or the real .env.
os.environ.setdefault("PROXY_AUTH_TOKEN", "test-token-0123456789abcdef")
os.environ.setdefault("KILO_CONFIG_PATH", str(ROOT / "state" / "test_kilo_models.json"))
