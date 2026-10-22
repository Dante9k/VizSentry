"""PIQ 9948a52 helper subset; see LICENSE and NOTICE.md.

Gaussian and YIQ conversions preserve upstream arithmetic. Input validation
and reduction are small VizSentry-specific replacements for PIQ utils.
"""
import torch


def gaussian_filter(kernel_size, sigma, device=None, dtype=None):
    coords = torch.arange(kernel_size, dtype=dtype, device=device)
    coords -= (kernel_size - 1) / 2.
    g = coords ** 2
    g = (- (g.unsqueeze(0) + g.unsqueeze(1)) / (2 * sigma ** 2)).exp()
    g /= g.sum()
    return g.unsqueeze(0)


def rgb2yiq(x):
    yiq_weights = torch.tensor([
        [0.299, 0.587, 0.114],
        [0.5959, -0.2746, -0.3213],
        [0.2115, -0.5227, 0.3112],
    ], dtype=x.dtype, device=x.device).t()
    return torch.matmul(x.permute(0, 2, 3, 1), yiq_weights).permute(0, 3, 1, 2)


def _validate_input(tensors, dim_range, data_range):
    for tensor in tensors:
        if tensor.ndim != 4 or tensor.shape[1] not in (1, 3):
            raise ValueError("BRISQUE requires NCHW gray or RGB tensors")
        if not torch.isfinite(tensor).all() or tensor.min() < data_range[0] or tensor.max() > data_range[1]:
            raise ValueError("BRISQUE input values are outside the expected range")


def _reduce(value, reduction):
    if reduction == "mean":
        return value.mean()
    if reduction == "sum":
        return value.sum()
    if reduction == "none":
        return value
    raise ValueError("Unknown reduction")
