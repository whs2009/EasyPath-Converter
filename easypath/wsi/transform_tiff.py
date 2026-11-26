from __future__ import annotations

import hashlib

import io

import json

import os

from pathlib import Path

import numpy as np

from .ep_format import EPError

from .ep_format import MAX_FILE_BYTES

from .ep_format import TILE_SIZE

from .ep_format import integer

from .transform_pyramid import REFERENCE_POLICY

from .transform_integrity import artifact_snapshot

TIFF_SCHEMA = "easypath.slide-transform.tiff.v1"

class _BudgetFile:
    def __init__(self, file, budget):
        self._file, self._budget = file, budget

    def write(self, data):
        if self._file.tell() + memoryview(data).nbytes > self._budget:
            raise EPError("TIFF output exceeds its reserved byte budget.")
        return self._file.write(data)

    def seek(self, offset, whence=0):
        if whence not in (0,1,2):
            raise ValueError("Invalid seek mode.")
        if whence == 2:
            self._file.flush()
        position = offset + (self._file.tell() if whence == 1 else os.fstat(self._file.fileno()).st_size if whence == 2 else 0)
        if position < 0 or position > self._budget:
            raise EPError("TIFF seek exceeds its reserved byte budget.")
        return self._file.seek(position)

    def fileno(self):
        # NumPy.tofile must not bypass the byte-budget write wrapper.
        raise io.UnsupportedOperation("Budgeted TIFF stream has no direct file descriptor.")

    def __getattr__(self, name):
        return getattr(self._file, name)

def write_tiff_artifact(path: Path, pyramid, *, max_file_bytes: int,
                        check_cancel=lambda: None, progress=lambda value: None):
    import tifffile
    from PIL import ImageCms

    integer(max_file_bytes, "TIFF output budget", 1, MAX_FILE_BYTES)
    path = Path(path)
    if path.suffix != ".partial":
        raise EPError("TIFF conversion writes an owned .partial artifact only.")
    metadata = pyramid.metadata()
    from .ep_format import check_transform_dimensions
    check_transform_dimensions(metadata, "tiff")
    description = json.dumps({"schema": TIFF_SCHEMA, "reference_policy": REFERENCE_POLICY,
        "raster": metadata.model_dump(mode="json")}, ensure_ascii=True, sort_keys=True)
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    total, done = sum(p.tiles for p in metadata.planes), 0
    check_cancel()
    with path.open("xb+") as raw:
        owned = os.fstat(raw.fileno())
        with tifffile.TiffWriter(_BudgetFile(raw,max_file_bytes), bigtiff=True, byteorder="<", ome=False) as writer:
            for plane_id, plane in enumerate(metadata.planes):
                def tiles():
                    nonlocal done
                    for ty in range(plane.rows):
                        for tx in range(plane.columns):
                            check_cancel()
                            tile = pyramid.read_region(plane_id,tx*TILE_SIZE,ty*TILE_SIZE,TILE_SIZE,TILE_SIZE)
                            done += 1
                            progress(done/(2*total))
                            yield tile
                level = plane_id if plane_id < len(metadata.levels) else None
                resolution = None
                if level is not None and metadata.mpp_x is not None and metadata.mpp_y is not None:
                    resolution = (10000/(metadata.mpp_x*2**level), 10000/(metadata.mpp_y*2**level))
                writer.write(tiles(), shape=(plane.height,plane.width,3), dtype=np.uint8,
                    tile=(TILE_SIZE,TILE_SIZE), photometric="rgb", compression="deflate", predictor=True,
                    metadata=None, description=description if plane_id == 0 else getattr(plane,"name",None),
                    subfiletype=0 if plane_id == 0 or level is None else 1,
                    resolution=resolution, resolutionunit="CENTIMETER" if resolution else "NONE",
                    iccprofile=icc, software="EasyPath Slide Transform", maxworkers=1)
        raw.flush()
        os.fsync(raw.fileno())
        before_validation = artifact_snapshot(path,check_cancel)
        with tifffile.TiffFile(path) as reader:
            opened = os.fstat(reader.filehandle.fileno())
            if (owned.st_dev,owned.st_ino) != (opened.st_dev,opened.st_ino):
                raise EPError("TIFF verification source was replaced.")
            if len(reader.pages) != len(metadata.planes) or not reader.is_bigtiff:
                raise EPError("TIFF output directory disagrees with the plan.")
            if reader.pages[0].description != description:
                raise EPError("TIFF provenance was not preserved.")
            for plane_id, (page, plane) in enumerate(zip(reader.pages,metadata.planes)):
                if page.shape != (plane.height,plane.width,3) or not page.is_tiled or page.tilewidth != TILE_SIZE or page.tilelength != TILE_SIZE:
                    raise EPError("TIFF output geometry disagrees with the plan.")
                count = 0
                for tile, position, shape in page.segments(maxworkers=1, buffersize=4*1024*1024):
                    check_cancel()
                    _, _, y, x, _ = position
                    expected = pyramid.read_region(plane_id,x,y,TILE_SIZE,TILE_SIZE)
                    if tile is None or tile.dtype != np.uint8 or tile.shape != (1,TILE_SIZE,TILE_SIZE,3) or not np.array_equal(tile[0],expected):
                        raise EPError("TIFF decoded pixels disagree with the reference.")
                    count += 1
                    done += 1
                    progress(done/(2*total))
                if count != plane.tiles:
                    raise EPError("TIFF output is missing tiles.")
        raw.seek(0)
        digest = hashlib.sha256()
        while block := raw.read(1024*1024):
            check_cancel()
            digest.update(block)
        current = path.lstat()
        if path.is_symlink() or (current.st_dev,current.st_ino) != (owned.st_dev,owned.st_ino):
            raise EPError("TIFF output ownership changed.")
        check_cancel()
        if artifact_snapshot(path,check_cancel) != before_validation or digest.hexdigest() != before_validation["sha256"]:
            raise EPError("TIFF artifact changed during independent verification.")
        return {"sha256":digest.hexdigest(),"size_bytes":current.st_size,"tile_count":total,
                "reference_policy":REFERENCE_POLICY,"rgb_sse":0,"registered":False,
                "format":"tiff","verified":True,"tifffile":tifffile.__version__}
