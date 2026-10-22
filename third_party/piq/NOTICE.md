# PIQ BRISQUE subset

Source: https://github.com/photosynthesis-team/piq

Pinned revision: `9948a52fc09ac5f7fb3618ce64b7086f5c3109da`.
Upstream is licensed under Apache License 2.0; the complete license is in `LICENSE`.
Copyright belongs to the PIQ contributors. The vendored resize module retains its
author attribution to Sanghyun Son. See its module header.

Vendored files: `piq/brisque.py`, `piq/functional/resize.py`. The
`gaussian_filter` and `rgb2yiq` functions in `helpers.py` are extracted from
`piq/functional/filters.py` and `piq/functional/colour_conversion.py` at the same revision.

VizSentry modifications (2026-10-22): isolated relative imports to avoid importing
torchvision or unrelated PIQ models; local validation/reduction helpers;
required local model path; SHA-256 validation and `torch.load(weights_only=True)`;
CPU weight caching; non-mutating support-vector transpose for deterministic repeated
inference. Runtime never downloads weights. `BRISQUELoss` is retained for source
traceability but is not a supported VizSentry interface. Call `brisque(..., weights_path=...)`.

Weights originate from PIQ's v0.4.0 release:
https://github.com/photosynthesis-team/piq/releases/download/v0.4.0/brisque_svm_weights.pt

SHA-256: `e5b048ef5325bae6c57965cf077fadfa5834fcefb02e13611b4ea2d36cf3c52c`.
This hash was computed from the pinned upstream release artifact during integration;
it detects altered or corrupt copies and is not an independent authenticity attestation.

BRISQUE reference: Anish Mittal et al., *No-Reference Image Quality Assessment
in the Spatial Domain*, IEEE Transactions on Image Processing (2012).
