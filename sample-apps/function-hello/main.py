"""A Cloud Run function (gen2). The same source also runs as a container (see Dockerfile), so CloudMorph can
deploy it to the `function` target or to local / cloudrun / node unchanged."""

import os
import platform
from datetime import datetime, timezone

import functions_framework


@functions_framework.http
def hello(request):
    name = request.args.get("name", "CloudMorph")
    return {
        "message": f"hello, {name}",
        "served_by": "cloud run functions (gen2)" if os.environ.get("K_SERVICE") and os.environ.get("FUNCTION_TARGET") else "container",
        "python": platform.python_version(),
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
