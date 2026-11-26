from __future__ import annotations

import hashlib

import os

from pathlib import Path

import threading

import numpy as np

from .ep_format import EPError

from .ep_format import EPMetadata

from .ep_format import MAX_FILE_BYTES

from .ep_format import MAX_REGION_PIXELS

from .ep_format import TILE_SIZE

from .ep_format import integer

TILE_BYTES = TILE_SIZE * TILE_SIZE * 3

REFERENCE_POLICY = "rgb8-srgb-2x-box-half-up-valid-edge-v1"

def reference_workspace_bytes(metadata: EPMetadata) -> int:
    return sum(p.tiles for p in metadata.planes) * TILE_BYTES

def downsample_box2(source: np.ndarray) -> np.ndarray:
    if source.dtype != np.uint8 or source.ndim != 3 or source.shape[2] != 3:
        raise EPError("Reference pyramid requires RGB8 pixels.")
    h, w = source.shape[:2]
    if not 1 <= h <= 1024 or not 1 <= w <= 1024:
        raise EPError("Reference downsampling block exceeds its bounds.")
    from .ep_quality import _native_pixels
    native = _native_pixels()
    if native is not None:
        return native.box2(np.ascontiguousarray(source))
    return _downsample_numpy(source)

def _downsample_numpy(source):
    sums = source[0::2, 0::2].astype(np.uint16)
    counts = np.ones(sums.shape[:2] + (1,), dtype=np.uint16)
    for oy, ox in ((0,1), (1,0), (1,1)):
        part = source[oy::2, ox::2]
        sums[:part.shape[0], :part.shape[1]] += part
        counts[:part.shape[0], :part.shape[1]] += 1
    return ((sums + counts // 2) // counts).astype(np.uint8)

class RGBPyramid:
    def __init__(self, metadata: EPMetadata):
        self._metadata = metadata
        self._planes = []
        self._lock = threading.RLock()
        self._complete = False
        self._closed = False

    @classmethod
    def build(cls, directory: Path, metadata: EPMetadata, read_source_plane, *, max_workspace_bytes: int,
              check_cancel=lambda: None, progress=lambda value: None) -> RGBPyramid:
        integer(max_workspace_bytes, "workspace budget", 1, MAX_FILE_BYTES)
        if reference_workspace_bytes(metadata) > max_workspace_bytes:
            raise EPError("Insufficient reserved reference-pyramid workspace.")
        check_cancel()
        directory = Path(directory)
        directory.mkdir(exist_ok=False)
        result = cls(metadata)
        done, total = 0, sum(p.tiles for p in metadata.planes)
        try:
            for plane_id, plane in enumerate(metadata.planes):
                file = (directory / f"plane-{plane_id:03d}.rgb").open("xb+")
                digests = bytearray(plane.tiles * 32)
                state = [file, digests, None]
                result._planes.append(state)
                for ty in range(plane.rows):
                    for tx in range(plane.columns):
                        check_cancel()
                        x, y = tx*TILE_SIZE, ty*TILE_SIZE
                        w, h = min(TILE_SIZE, plane.width-x), min(TILE_SIZE, plane.height-y)
                        if 0 < plane_id < len(metadata.levels):
                            parent = metadata.levels[plane_id-1]
                            source = result._read_region(plane_id-1, 2*x, 2*y,
                                min(2*w, parent.width-2*x), min(2*h, parent.height-2*y))
                            source = downsample_box2(source)
                        else:
                            source = read_source_plane(plane_id, x, y, w, h)
                        if not isinstance(source, np.ndarray) or source.dtype != np.uint8 or source.shape != (h,w,3):
                            raise EPError("Source did not return exact native RGB8 geometry.")
                        tile = np.full((TILE_SIZE,TILE_SIZE,3), 255, dtype=np.uint8)
                        tile[:h, :w] = source
                        payload = tile.tobytes()
                        file.write(payload)
                        at = (ty*plane.columns + tx)*32
                        digests[at:at+32] = hashlib.sha256(payload).digest()
                        done += 1
                        progress(done/total)
                file.flush()
                os.fsync(file.fileno())
                state[2] = result._fingerprint(file)
            check_cancel()
            result._complete = True
            return result
        except BaseException:
            result.close()
            raise

    @staticmethod
    def _fingerprint(file):
        info = os.fstat(file.fileno())
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns

    def metadata(self) -> EPMetadata:
        if self._closed or not self._complete:
            raise EPError("Reference pyramid is not complete and open.")
        return self._metadata

    def _read_tile(self, plane_id, tx, ty):
        plane = self._metadata.planes[plane_id]
        file, digests, fingerprint = self._planes[plane_id]
        index = ty*plane.columns + tx
        with self._lock:
            if self._closed or fingerprint is None or self._fingerprint(file) != fingerprint:
                raise EPError("Reference pyramid changed or is incomplete.")
            file.seek(index*TILE_BYTES)
            payload = file.read(TILE_BYTES)
            if (len(payload) != TILE_BYTES or self._fingerprint(file) != fingerprint
                    or hashlib.sha256(payload).digest() != digests[index*32:(index+1)*32]):
                raise EPError("Reference tile checksum mismatch.")
        return np.frombuffer(payload, dtype=np.uint8).reshape(TILE_SIZE,TILE_SIZE,3)

    def _read_region(self, plane_id, x, y, width, height):
        integer(plane_id, "plane", 0, len(self._metadata.planes)-1)
        integer(x, "x", -(1<<53), 1<<53)
        integer(y, "y", -(1<<53), 1<<53)
        integer(width, "width", 1, MAX_REGION_PIXELS)
        integer(height, "height", 1, MAX_REGION_PIXELS)
        if width*height > MAX_REGION_PIXELS:
            raise EPError("Reference region exceeds its pixel limit.")
        plane = self._metadata.planes[plane_id]
        output = np.full((height,width,3), 255, dtype=np.uint8)
        left, top, right, bottom = max(0,x), max(0,y), min(plane.width,x+width), min(plane.height,y+height)
        if left >= right or top >= bottom:
            return output
        for ty in range(top//TILE_SIZE, (bottom-1)//TILE_SIZE+1):
            for tx in range(left//TILE_SIZE, (right-1)//TILE_SIZE+1):
                tile = self._read_tile(plane_id,tx,ty)
                sx, sy = tx*TILE_SIZE, ty*TILE_SIZE
                l, t, r, b = max(left,sx), max(top,sy), min(right,sx+TILE_SIZE), min(bottom,sy+TILE_SIZE)
                output[t-y:b-y,l-x:r-x] = tile[t-sy:b-sy,l-sx:r-sx]
        return output

    def read_region(self, plane_id, x, y, width, height):
        self.metadata()
        return self._read_region(plane_id,x,y,width,height)

    def close(self):
        with self._lock:
            for file, _, _ in self._planes:
                file.close()
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
