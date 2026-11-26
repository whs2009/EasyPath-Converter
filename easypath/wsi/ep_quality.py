from __future__ import annotations

import math

from functools import lru_cache

from importlib import import_module

import numpy as np

from .ep_format import EPError

from .ep_precision import precision_reference

LOSSY_PROFILE = "rgb8-jxl-bounded-lossy-v1"

METRIC = "100/255 * sqrt(sum squared RGB error / RGB sample count)"

FOREGROUND = "reference min<220 and (channel range>8 or channel mean<180); heuristic, not clinical tissue segmentation"

KEYS = ("sse", "sae", "samples", "foreground_sse", "foreground_sae", "foreground_samples")

@lru_cache(maxsize=1)
def _native_pixels():
    try:
        return import_module("easypath._ep_pixels")
    except ModuleNotFoundError as exc:
        if exc.name != "easypath._ep_pixels":
            raise
        return None

def measure(reference, decoded):
    if (reference.dtype != np.uint8 or decoded.dtype != np.uint8 or
            reference.shape != decoded.shape or reference.ndim != 3 or reference.shape[2] != 3):
        raise EPError("EP quality requires matching RGB8 reference pixels.")
    if reference.shape[0] <= 512 and reference.shape[1] <= 512:
        native = _native_pixels()
        if native is not None:
            return native.measure_rgb8(np.ascontiguousarray(reference), np.ascontiguousarray(decoded))
    return _measure_numpy(reference, decoded)

def _measure_numpy(reference, decoded):
    difference = reference.astype(np.int32) - decoded.astype(np.int32)
    foreground = (reference.min(axis=2) < 220) & ((np.ptp(reference, axis=2) > 8) | (reference.mean(axis=2) < 180))
    selected = difference[foreground]
    return dict(sse=int(np.square(difference).sum(dtype=np.int64)),
                sae=int(np.abs(difference).sum(dtype=np.int64)), samples=int(difference.size),
                foreground_sse=int(np.square(selected).sum(dtype=np.int64)),
                foreground_sae=int(np.abs(selected).sum(dtype=np.int64)), foreground_samples=int(selected.size))

def within_limit(row, limit):
    from .ep_format import integer
    integer(limit, "RGB RMSE limit", 0, 15)
    # Compare squared normalized RMSE without floating-point threshold rounding.
    return row["samples"] > 0 and all(row[prefix + "sse"] * 10000 <= row[prefix + "samples"] * 65025 * limit**2
                                      for prefix in ("", "foreground_"))

def metrics(row):
    result = {}
    for name, prefix in (("all_pixels", ""), ("foreground", "foreground_")):
        sse, sae, samples = (row[prefix + key] for key in ("sse", "sae", "samples"))
        result[name] = dict(sse=sse, sae=sae, samples=samples,
            nrmse_percent=100 / 255 * math.sqrt(sse / samples) if samples else None,
            nmae_percent=100 / 255 * sae / samples if samples else None)
    return result

class QualitySummary:
    """O(number of levels) totals and ten worst tiles, never a whole-slide array."""
    def __init__(self, metadata):
        self.metadata = metadata
        self.rows = [dict.fromkeys(KEYS, 0) for _ in metadata.planes]
        self.tiles = [0] * len(self.rows)
        self.worst = []

    def add(self, plane, x, y, reference, decoded):
        row = measure(reference, decoded)
        if plane >= len(self.metadata.levels):
            valid = row["sse"] == 0
        elif self.metadata.channel_bits is not None and self.metadata.maximum_nrmse_percent == 0:
            valid = np.array_equal(precision_reference(reference, self.metadata.channel_bits), decoded)
        else:
            valid = within_limit(row, self.metadata.maximum_nrmse_percent)
        if not valid:
            raise EPError("EP output exceeds its verified RGB quality limit.")
        for key in KEYS:
            self.rows[plane][key] += row[key]
        self.tiles[plane] += 1
        values = metrics(row)
        self.worst.append(dict(plane=plane, x=x, y=y, **values))
        self.worst.sort(key=lambda item: (max(item["all_pixels"]["nrmse_percent"],
            item["foreground"]["nrmse_percent"] or 0), -item["plane"], -item["y"], -item["x"]), reverse=True)
        del self.worst[10:]

    def report(self):
        if any(count != size.tiles for count, size in zip(self.tiles, self.metadata.planes)):
            raise EPError("EP quality verification did not cover every tile.")
        volume = {key: sum(row[key] for row in self.rows[:len(self.metadata.levels)]) for key in KEYS}
        report = dict(metric=METRIC, maximum_nrmse_percent=self.metadata.maximum_nrmse_percent, padding_excluded=True,
            foreground_mask=FOREGROUND, associated_lossless=True,
            verified_all_tiles=True, whole_pyramid=metrics(volume),
            planes=[dict(plane=index, role="volume" if index < len(self.metadata.levels) else size.name,
                width=size.width, height=size.height, tiles=self.tiles[index], **metrics(self.rows[index]))
                for index, size in enumerate(self.metadata.planes)], worst_tiles=self.worst)
        if self.metadata.channel_bits is not None:
            report.update(channel_bits=self.metadata.channel_bits,
                          limit_scope="total-source-rgb" if self.metadata.maximum_nrmse_percent else "quantized-reference-lossless")
        return report
