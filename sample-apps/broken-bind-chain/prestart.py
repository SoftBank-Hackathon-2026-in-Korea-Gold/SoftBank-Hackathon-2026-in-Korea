import json
import platform
import time
from pathlib import Path

info = {
    "python": platform.python_version(),
    "arch": platform.machine(),
    "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
}
Path("build-info.json").write_text(json.dumps(info))
print("prestart: wrote build-info.json", flush=True)
