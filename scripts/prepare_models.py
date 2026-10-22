"""Download and verify model assets once. Runtime inference remains offline."""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Load the manifest without importing torch or the runtime package.
spec = importlib.util.spec_from_file_location("vizsentry_weight_manifest", ROOT / "third_party" / "piq" / "weights.py")
manifest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manifest)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    default = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share"))) / "VizSentry"
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("VIZSENTRY_DATA_DIR", str(default))))
    parser.add_argument("--check", action="store_true", help="Check installed weights without downloading")
    args = parser.parse_args()
    target = args.data_dir / "models" / "brisque_svm_weights.pt"
    try:
        manifest.verify_weights(target)
        print(f"Verified BRISQUE weights: {target}")
        return 0
    except (OSError, ValueError):
        if args.check:
            print(f"Missing or invalid BRISQUE weights: {target}", file=sys.stderr)
            return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".download")
    try:
        with urllib.request.urlopen(manifest.WEIGHTS_URL, timeout=120) as response, temporary.open("wb") as output:
            while block := response.read(1024 * 1024):
                output.write(block)
        manifest.verify_weights(temporary)
        temporary.replace(target)
        print(f"Prepared and verified offline BRISQUE weights: {target}")
        return 0
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        print(f"Model preparation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
