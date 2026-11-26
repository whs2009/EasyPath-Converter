from __future__ import annotations

import numpy as np

from .ep_format import EPError

from .ep_format import EPMetadata

from .ep_format import TILE_SIZE

from .ep_format import integer

from .transform_pyramid import downsample_box2

from .transform_pyramid import REFERENCE_POLICY

DEFAULT_PENDING_BYTES = 256 * 1024 * 1024

def base_tile_requests(base):
    """Visit one 2x2 block at a time to complete nearby parent tiles."""
    for by in range(0, base.rows, 2):
        for bx in range(0, base.columns, 2):
            for ty in range(by, min(by+2, base.rows)):
                for tx in range(bx, min(bx+2, base.columns)):
                    x, y = tx*TILE_SIZE, ty*TILE_SIZE
                    yield 0, x, y, min(TILE_SIZE, base.width-x), min(TILE_SIZE, base.height-y)

def stream_pyramid(metadata, read_source_plane, emit, *, max_pending_bytes=DEFAULT_PENDING_BYTES,
                   check_cancel=lambda: None):
    if not isinstance(metadata, EPMetadata):
        raise EPError("Streaming pyramid requires validated metadata.")
    integer(max_pending_bytes, "pending tile budget", 1, 1024**3)
    levels = metadata.levels
    for parent, child in zip(levels, levels[1:]):
        if (child.width, child.height) != ((parent.width+1)//2, (parent.height+1)//2):
            raise EPError("Streaming pyramid requires consecutive averaged levels.")
    pending = {}
    allocated = peak = base_reads = emitted = 0

    def push(level, tx, ty, pixels):
        nonlocal allocated, peak, emitted
        check_cancel()
        size = levels[level]
        shape = (min(TILE_SIZE, size.height-ty*TILE_SIZE), min(TILE_SIZE, size.width-tx*TILE_SIZE), 3)
        if not isinstance(pixels, np.ndarray) or pixels.dtype != np.uint8 or pixels.shape != shape:
            raise EPError("Source did not return exact native RGB8 geometry.")
        # Derive children from canonical pixels, never from a lossy decoded parent.
        reduced = downsample_box2(pixels) if level + 1 < len(levels) else None
        emit(level, tx, ty, pixels)
        emitted += 1
        if reduced is None:
            return
        key = (level+1, tx//2, ty//2)
        parent = levels[level+1]
        target_shape = (min(TILE_SIZE, parent.height-key[2]*TILE_SIZE),
                        min(TILE_SIZE, parent.width-key[1]*TILE_SIZE), 3)
        if key not in pending:
            nbytes = target_shape[0]*target_shape[1]*3
            if allocated + nbytes > max_pending_bytes:
                raise EPError("Pending pyramid tiles exceed their memory budget.")
            pending[key] = [np.empty(target_shape, np.uint8), 0]
            allocated += nbytes
            peak = max(peak, allocated)
        target, mask = pending[key]
        bit = 1 << ((ty % 2)*2 + tx % 2)
        if mask & bit:
            raise EPError("Duplicate pyramid quadrant.")
        x, y = (tx % 2)*(TILE_SIZE//2), (ty % 2)*(TILE_SIZE//2)
        target[y:y+reduced.shape[0], x:x+reduced.shape[1]] = reduced
        pending[key][1] |= bit
        wanted = sum(1 << (dy*2+dx) for dy in (0, 1) for dx in (0, 1)
                     if (key[1]*2+dx)*TILE_SIZE < size.width and (key[2]*2+dy)*TILE_SIZE < size.height)
        if pending[key][1] == wanted:
            push(level+1, key[1], key[2], target)
            allocated -= target.nbytes
            del pending[key]

    base = levels[0]
    for plane, x, y, w, h in base_tile_requests(base):
        check_cancel()
        pixels = read_source_plane(plane, x, y, w, h)
        base_reads += 1
        push(0, x//TILE_SIZE, y//TILE_SIZE, pixels)
    if pending or allocated:
        raise EPError("Incomplete streaming pyramid tiles.")
    for plane, size in enumerate(metadata.associated, len(levels)):
        for ty in range(size.rows):
            for tx in range(size.columns):
                check_cancel()
                x, y = tx*TILE_SIZE, ty*TILE_SIZE
                emit(plane, tx, ty, read_source_plane(plane, x, y, min(TILE_SIZE, size.width-x), min(TILE_SIZE, size.height-y)))
                emitted += 1
    return dict(policy=REFERENCE_POLICY, base_reads=base_reads, tiles_emitted=emitted,
                traversal="2x2-base-blocks-v1",
                peak_pending_bytes=peak, pending_limit_bytes=max_pending_bytes, raw_pyramid_files=0,
                budget_scope="pending tiles only; bounded temporary tiles, codec and directory buffers are separate")
