from __future__ import annotations

from contextlib import contextmanager

from hashlib import sha256

from threading import BoundedSemaphore

import numpy as np

from PIL import Image

from PIL import ImageCms

from .ep_format import TILE_SIZE

from .ep_stream_pyramid import DEFAULT_PENDING_BYTES

from .transform_source import NativeTransformSource

from .transform_source import _OpenSlideInput

_APERIO_RGB = "1397095fe6a2c61e1bc11889f56b8762cc0a588aff43ca0634cefacab990d7bb"

_MIN_PIXELS = 32 * 1024 * 1024

_RESERVE = 64 * 1024 * 1024

_slot = BoundedSemaphore(1)

def _eligible(source, budget):
    if type(source) is not NativeTransformSource or source.closed or type(source.backend) is not _OpenSlideInput:
        return False
    backend = source.backend
    levels = source.metadata.levels
    if (levels[0].width * levels[0].height < _MIN_PIXELS or backend._ep_volume_apply is not None
            or backend.color.transform is None or backend.slide.properties.get("openslide.vendor") != "aperio"
            or sha256(backend.color.profile).hexdigest() != _APERIO_RGB):
        return False
    # The 2x2 base-block traversal only has a partial row of parents and
    # the row being completed at any given level, including recursive emits.
    row_bound = 2 * sum(p.columns for p in levels[1:]) * TILE_SIZE**2 * 3
    return row_bound + _RESERVE <= budget

def _table(color, check):
    check()
    try:
        lut = np.empty((256, 256, 256, 3), np.uint8)
        slab = np.empty((256, 256, 3), np.uint8)
        axis = np.arange(256, dtype=np.uint8)
        slab[:, :, 1], slab[:, :, 2] = axis[:, None], axis
    except MemoryError:
        return None
    for r in range(256):
        if r % 8 == 0:
            check()
        try:
            slab[:, :, 0] = r
            lut[r] = np.asarray(ImageCms.applyTransform(Image.fromarray(slab), color.transform))
        except MemoryError:
            return None
    lut.setflags(write=False)
    check()
    return lut

@contextmanager
def ep_volume_color(source, max_pending_bytes=DEFAULT_PENDING_BYTES):
    """Yield the remaining pyramid budget; leave all source checks in read_plane."""
    if not _eligible(source, max_pending_bytes):
        yield max_pending_bytes
        return
    try:
        from easypath._ep_icc import apply_rgb8
    except ImportError:
        yield max_pending_bytes
        return
    if not _slot.acquire(blocking=False):
        yield max_pending_bytes
        return
    backend = source.backend
    try:
        table = _table(backend.color, source.check_cancel)
        if table is None:
            yield max_pending_bytes
            return

        def apply(image):
            if image.mode not in ("RGB", "RGBA") or not (0 < image.width <= TILE_SIZE and 0 < image.height <= TILE_SIZE):
                return backend.color.apply(image)
            return apply_rgb8(np.asarray(image), table)

        backend._ep_volume_apply = apply
        try:
            yield max_pending_bytes - _RESERVE
        finally:
            backend._ep_volume_apply = None
    finally:
        _slot.release()
