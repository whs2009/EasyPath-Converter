from __future__ import annotations

from contextlib import ExitStack

from contextlib import contextmanager

from .ep_codec import single_threaded_decode

from .ep_format import EPError

from .ep_format import TILE_SIZE

from .ep_stream_pyramid import DEFAULT_PENDING_BYTES

from .ep_stream_pyramid import base_tile_requests

@contextmanager
def ep_writer_reads(source, budget):
    from .ep_openslide_ahead import OwnedOpenSlideSource

    if type(source) is not OwnedOpenSlideSource:
        with ep_source_reads(source, max_pending_bytes=budget) as read:
            yield read, budget
        return

    levels = source.metadata.levels
    tile_bytes = TILE_SIZE * TILE_SIZE * 3
    # A 2x2 base block completes one level-one tile. From there children
    # finish row-major, so at most one row of parents per level is live.
    required = (1 + sum(p.columns for p in levels[2:])) * tile_bytes
    if levels[0].tiles < 2 or required + tile_bytes > budget:
        yield source.read_plane, budget
        return
    with _ordered_reads(source, source.submit_pixels) as read:
        yield read, budget - tile_bytes

@contextmanager
def ep_source_reads(source, *, max_pending_bytes=DEFAULT_PENDING_BYTES):
    yield source.read_plane

@contextmanager
def _ordered_reads(source, submit_pixels):
    order = base_tile_requests(source.metadata.levels[0])
    expected = next(order)
    pending = None
    with ExitStack() as codec_lane:
        # A checked decode must not use two threads alongside a source read.
        codec_lane.enter_context(single_threaded_decode())
        def submit(key):
            request = source._prepare_read(*key)
            return submit_pixels(*request)

        def read(plane, x, y, w, h):
            nonlocal expected, pending
            if source.closed:
                raise EPError("Transform source is closed.")
            if plane != 0:
                if expected is not None:
                    raise EPError("Associated image requested before the volume was complete.")
                return source.read_plane(plane, x, y, w, h)
            key = (plane, x, y, w, h)
            if any(type(value) is not int for value in key) or key != expected:
                raise EPError("Unexpected EP volume read order or geometry.")
            if pending is None:
                future = submit(key)
            else:
                source.check_cancel()
                future, pending = pending, None
            pixels = future.result()
            if source.closed:
                raise EPError("Transform source is closed.")
            source.check_cancel()
            expected = next(order, None)
            if expected is not None:
                pending = submit(expected)
            else:
                # Associated images are synchronous; final replay has no SDK work.
                codec_lane.close()
            return pixels

        try:
            yield read
            if expected is not None:
                raise EPError("EP writer did not consume the complete volume.")
        except BaseException as primary:
            if pending is not None and not pending.cancel():
                try:
                    pending.result()
                    source.check_cancel()
                except BaseException as secondary:
                    primary.add_note(f"Pending volume read during cleanup: {secondary!r}")
            raise
