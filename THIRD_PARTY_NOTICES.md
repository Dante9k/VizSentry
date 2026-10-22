# Third-party source notices

VizSentry includes the following pinned upstream source subsets. Their original licenses, attribution and local modification notices are retained beside the code.

| Component | Pinned revision | License | Files and modifications |
| --- | --- | --- | --- |
| [PIQ](https://github.com/photosynthesis-team/piq) BRISQUE | `9948a52fc09ac5f7fb3618ce64b7086f5c3109da` | Apache-2.0 | [License](third_party/piq/LICENSE), [notice](third_party/piq/NOTICE.md) |
| [feishu-bitable-export](https://github.com/lewishezhu-rgb/feishu-bitable-export) | `d00117fd8f744c09055b62d92415f62ed5999157` | MIT | [License](third_party/feishu-bitable-export/LICENSE), [notice](third_party/feishu-bitable-export/NOTICE.md) |

The original Feishu reader is retained for traceability. The maintained integration is in `browser/reader.mjs` and `browser/worker.mjs`. The upstream license and original reader Git blob hashes were compared with the pinned revision during integration.

BRISQUE weights are downloaded by `scripts/prepare_models.py` from the PIQ release URL recorded in `third_party/piq/weights.py`, and checked against its SHA-256 manifest. Runtime does not download weights. See the PIQ notice for the source and checksum provenance.

Other dependencies are installed using Python requirements and pnpm lockfiles; their package licenses remain applicable. No third-party service authorization or compatibility is implied by inclusion of source code.
