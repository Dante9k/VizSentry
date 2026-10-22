"""Local evidence extraction. Quality metrics are never a business verdict."""
from __future__ import annotations

import importlib.util
import math
import os
import sys
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def inspect_brisque(data_dir: Path) -> dict:
    """Cheap setup diagnostics, including checksum, without importing torch."""
    path = Path(data_dir) / "models" / "brisque_svm_weights.pt"
    result = {"torch_installed": importlib.util.find_spec("torch") is not None,
              "weights_exists": path.is_file(), "weights_valid": False}
    spec = importlib.util.spec_from_file_location("_vizsentry_weight_check", PROJECT_ROOT / "third_party" / "piq" / "weights.py")
    manifest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(manifest)
    try:
        manifest.verify_weights(path)
        result["weights_valid"] = True
    except (OSError, ValueError) as exc:
        result["error"] = str(exc)
    return result


@lru_cache(maxsize=1)
def _brisque_module():
    """Load the isolated vendored package without importing torchvision/PIQ."""
    name = "_vizsentry_piq"
    spec = importlib.util.spec_from_file_location(
        name, PROJECT_ROOT / "third_party" / "piq" / "__init__.py",
        submodule_search_locations=[str(PROJECT_ROOT / "third_party" / "piq")],
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules[name] = package
    spec.loader.exec_module(package)
    return package


def analyze_image(path: str) -> dict:
    """Pickle-safe worker entry point. All optional model failures remain evidence."""
    result: dict = {"width": None, "height": None, "metrics": {}, "signals": []}
    try:
        with Image.open(path) as original:
            original.load()
            result["metrics"]["frames"] = getattr(original, "n_frames", 1)
            normalized = ImageOps.exif_transpose(original)
            if normalized.mode in ("RGBA", "LA") or "transparency" in normalized.info:
                rgba = normalized.convert("RGBA")
                result["metrics"]["transparent_fraction"] = round(
                    float((np.asarray(rgba.getchannel("A")) == 0).mean()), 6
                )
                rgb = Image.new("RGB", rgba.size, "white")
                rgb.paste(rgba, mask=rgba.getchannel("A"))
            else:
                rgb = normalized.convert("RGB")
            result["width"], result["height"] = rgb.size
            # Metrics are bounded in work and report their sampling resolution.
            rgb.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
            pixels = np.asarray(rgb, dtype=np.float32)
            gray = pixels @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
            quantized = np.asarray(rgb, dtype=np.uint8) // 16
            colors = (quantized[:, :, 0].astype(np.int32) * 256
                      + quantized[:, :, 1].astype(np.int32) * 16 + quantized[:, :, 2])
            dominant_fraction = float(np.bincount(colors.ravel(), minlength=4096).max() / colors.size)
            if min(gray.shape) >= 3:
                laplace = (-4 * gray[1:-1, 1:-1] + gray[:-2, 1:-1] + gray[2:, 1:-1]
                           + gray[1:-1, :-2] + gray[1:-1, 2:])
                sharpness = float(laplace.var())
            else:
                sharpness = 0.0
            metrics = result["metrics"]
            metrics.update({
                "sample_width": rgb.width, "sample_height": rgb.height,
                "brightness_mean": round(float(gray.mean()), 3),
                "brightness_std": round(float(gray.std()), 3),
                "dark_fraction": round(float((gray < 10).mean()), 6),
                "bright_fraction": round(float((gray > 245).mean()), 6),
                "dominant_color_fraction": round(dominant_fraction, 6),
                "laplacian_variance": round(sharpness, 3),
                "brisque": None,
                "brisque_note": "BRISQUE 适用于自然图像，分数仅作为证据；截图和图表需结合业务标准判断。",
            })
            if min(result["width"], result["height"]) < 32:
                result["signals"].append("图片尺寸较小，细节有限")
            if dominant_fraction >= 0.98:
                result["signals"].append("图片接近单色；可能是正常背景，也可能是渲染缺失")
            if metrics["brightness_mean"] < 15:
                result["signals"].append("图片整体偏暗")
            if metrics["brightness_mean"] > 240:
                result["signals"].append("图片整体偏亮")
            if sharpness < 20:
                result["signals"].append("细节或边缘较少，需结合图片类型判断模糊情况")
            if metrics.get("frames", 1) > 1:
                result["signals"].append("动态图仅检测首帧")
            if min(rgb.size) < 32 or float(gray.std()) < 0.01:
                metrics["brisque_status"] = "not_applicable"
                metrics["brisque_error"] = "尺寸过小或完全纯色，BRISQUE 统计不适用"
            else:
                try:
                    import torch
                    torch.set_num_threads(1)
                    default_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share"))) / "VizSentry"
                    data_dir = Path(os.environ.get("VIZSENTRY_DATA_DIR", str(default_dir)))
                    model_file = data_dir / "models" / "brisque_svm_weights.pt"
                    module = _brisque_module()
                    module.verify_weights(model_file)
                    tensor = torch.from_numpy(pixels.copy()).permute(2, 0, 1).unsqueeze(0) / 255.0
                    with torch.inference_mode():
                        score = float(module.brisque(tensor, weights_path=model_file).item())
                    if not math.isfinite(score):
                        raise ValueError("BRISQUE produced a non-finite value")
                    metrics["brisque"] = round(score, 5)
                    metrics["brisque_status"] = "ready"
                except (ImportError, FileNotFoundError) as exc:
                    metrics["brisque_status"] = "unavailable"
                    metrics["brisque_error"] = "缺少 torch 或本地 BRISQUE 模型资源，请运行 setup.ps1 准备离线检测"
                except Exception as exc:
                    # Degenerate pairs (e.g. graphics with flat regions) fail upstream assumptions.
                    metrics["brisque_status"] = "not_applicable"
                    metrics["brisque_error"] = str(exc)[:500]
            result["metrics"]["decoded"] = True
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        if isinstance(exc, FileNotFoundError):
            reason = "图片文件不存在，请重新获取附件"
        elif isinstance(exc, PermissionError):
            reason = "图片文件无法读取，请检查文件权限"
        elif isinstance(exc, UnidentifiedImageError):
            reason = "无法识别图片格式，附件可能损坏或不是受支持的图片"
        elif isinstance(exc, Image.DecompressionBombError):
            reason = "图片像素量过大，无法安全解码"
        else:
            reason = "图片数据不完整或无效，请重新获取附件"
        result["error"] = "图片无法解码：" + reason
        result["metrics"]["decoded"] = False
        result["signals"].append("图片无法解码，需人工检查附件")
    return result
