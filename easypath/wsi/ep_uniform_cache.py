from __future__ import annotations

from collections import OrderedDict

import numpy as np

from .ep_format import EPError

from .ep_format import MAX_TILE_BYTES

from .ep_format import TILE_SIZE

from .ep_format import integer

from .ep_quality import _native_pixels

DEFAULT_UNIFORM_CACHE_BYTES = 16 * 1024 * 1024

MAX_UNIFORM_CACHE_BYTES = 64 * 1024 * 1024

def uniform_rgb(pixels):
    if (not isinstance(pixels, np.ndarray) or pixels.dtype != np.uint8 or pixels.ndim != 3
            or pixels.shape[2] != 3 or not 1 <= pixels.shape[0] <= TILE_SIZE
            or not 1 <= pixels.shape[1] <= TILE_SIZE):
        raise EPError("Uniform detection requires bounded RGB8 pixels.")
    native = _native_pixels()
    if native is not None and hasattr(native, 'uniform_rgb8'):
        return native.uniform_rgb8(np.ascontiguousarray(pixels))
    color = pixels[0, 0]
    return tuple(int(value) for value in color) if np.all(pixels == color) else None

class UniformTileCache:
    def __init__(self, limit_bytes=DEFAULT_UNIFORM_CACHE_BYTES):
        integer(limit_bytes, 'uniform codec cache budget', 0, MAX_UNIFORM_CACHE_BYTES)
        self.limit = limit_bytes
        self.entries = OrderedDict()
        self.size = self.peak = self.hits = self.misses = self.evictions = 0

    def get(self, key):
        if key not in self.entries:
            self.misses += 1
            return None
        self.entries.move_to_end(key)
        self.hits += 1
        return self.entries[key]

    def put(self, key, payload, decoded, choice):
        if (type(payload) is not bytes or not 2 <= len(payload) <= MAX_TILE_BYTES
                or decoded.dtype != np.uint8 or decoded.shape != (TILE_SIZE, TILE_SIZE, 3)):
            raise EPError('Invalid uniform codec cache entry.')
        size = len(payload) + decoded.nbytes
        if size > self.limit:
            return
        if key in self.entries:
            previous = self.entries.pop(key)
            self.size -= len(previous[0]) + previous[1].nbytes
        while self.entries and (self.size + size > self.limit or len(self.entries) >= 16):
            _, previous = self.entries.popitem(last=False)
            self.size -= len(previous[0]) + previous[1].nbytes
            self.evictions += 1
        owned = decoded.copy()
        owned.flags.writeable = False
        self.entries[key] = payload, owned, choice
        self.size += size
        self.peak = max(self.peak, self.size)

    def info(self):
        return dict(bytes=self.size, peak_bytes=self.peak, limit_bytes=self.limit,
            entries=len(self.entries), entry_limit=16, hits=self.hits, misses=self.misses,
            evictions=self.evictions, scope='exact full-size uniform RGB tiles; per-export codec settings',
            source_reads_skipped=0, final_decodes_skipped=0)
