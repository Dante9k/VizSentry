"""Weight integrity manifest: no network code exists in the runtime package."""
import hashlib
from pathlib import Path

WEIGHTS_URL = "https://github.com/photosynthesis-team/piq/releases/download/v0.4.0/brisque_svm_weights.pt"
WEIGHTS_SHA256 = "e5b048ef5325bae6c57965cf077fadfa5834fcefb02e13611b4ea2d36cf3c52c"


def verify_weights(path: Path) -> None:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    if digest.hexdigest() != WEIGHTS_SHA256:
        raise ValueError("BRISQUE weights checksum mismatch; run scripts/prepare_models.py again")
