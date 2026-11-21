"""Run the OpenCV Aruco QR fallback in a separate user scope.

The QQ bridge has a 150 MiB service memory limit. Importing OpenCV inside the
bridge cgroup can exceed it, so the bridge sets GUET_AC_ARUCO_SCOPE=1 and this
worker is launched through ``systemd-run --user --scope``: the heavy import
lands in the user slice instead of the bridge service cgroup.

Input: image path, optional remaining decode budget in seconds.
Output: one JSON line {"ok": true, "texts": [...]} (payload only on the pipe,
never in logs). Any failure is {"ok": false}.
"""

import importlib.util
import json
import sys
from pathlib import Path
from time import monotonic

MAX_BUDGET = 4.2


def _preview_module():
    path = Path(__file__).resolve().with_name("guet_qr_preview.py")
    spec = importlib.util.spec_from_file_location("guet_qr_preview_worker", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print('{"ok":false}')
        return 2
    image_path = Path(args[0])
    try:
        budget = float(args[1]) if len(args) > 1 else MAX_BUDGET
    except ValueError:
        budget = MAX_BUDGET
    budget = max(0.2, min(MAX_BUDGET, budget))
    try:
        preview = _preview_module()
        image = preview._load(image_path)
        texts = preview.aruco_pass(image, monotonic() + budget,
                                   image_path=image_path, use_scope=False)
        print(json.dumps({"ok": True, "texts": list(texts)}, ensure_ascii=False))
    except Exception:
        print('{"ok":false}')
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
