from __future__ import annotations

from contextlib import contextmanager

import threading

import numpy as np

from .ep_format import EPError

from .ep_format import MAX_TILE_BYTES

from .ep_format import TILE_SIZE

from .ep_decode_lanes import decode_lanes

_decode_context = threading.local()

@contextmanager
def single_threaded_decode():
    """One codec thread per tile while the region reader decodes a pair."""
    previous = getattr(_decode_context, "single_threaded", False)
    _decode_context.single_threaded = True
    try:
        yield
    finally:
        _decode_context.single_threaded = previous

def require_codec():
    try:
        import imagecodecs
        if not imagecodecs.JPEGXL.available:
            raise ImportError("JPEG XL unavailable")
        return imagecodecs
    except (ImportError, AttributeError) as exc:
        raise EPError("EP slides require the EasyPath JPEG XL codec.") from exc

def codec_identity() -> dict[str, str]:
    codec = require_codec()
    return {"imagecodecs": codec.__version__, "jpegxl": codec.jpegxl_version(),
            "profile": "rgb8-jxl-lossless-v1", "threads": "1", "decoder_threads": "2"}

def decode_tile(payload: bytes, *, native=None) -> np.ndarray:
    if type(payload) is not bytes or not 2 <= len(payload) <= MAX_TILE_BYTES:
        raise EPError("Invalid EP tile payload length.")
    # Supplying the exact output array rejects mismatched dimensions, channels,
    # precision and animation shapes before allocating the decoded image.
    requested = 1 if getattr(_decode_context, "single_threaded", False) else 2
    try:
        with decode_lanes(requested) as threads:
            if native is not None:
                pixels = native.decode(payload, 8, threads)
                if (not isinstance(pixels, np.ndarray) or pixels.shape != (TILE_SIZE, TILE_SIZE, 3)
                        or pixels.dtype != np.uint8 or not pixels.flags.c_contiguous):
                    raise EPError("Invalid native EP decode result.")
                return pixels
            output = np.empty((TILE_SIZE, TILE_SIZE, 3), dtype=np.uint8)
            pixels = require_codec().jpegxl_decode(payload, numthreads=threads, keeporientation=True, out=output)
    except (ValueError, RuntimeError) as exc:
        raise EPError("EP tile cannot be decoded into its declared RGB8 storage grid.") from exc
    if pixels.shape != output.shape or pixels.dtype != output.dtype or not np.shares_memory(pixels, output):
        raise EPError("EP decoder output violates the storage grid.")
    return output

def encode_tile(pixels: np.ndarray) -> bytes:
    if (not isinstance(pixels, np.ndarray) or pixels.dtype != np.uint8 or
            pixels.shape != (TILE_SIZE, TILE_SIZE, 3)):
        raise EPError("EP encoding requires a 512x512 RGB8 tile.")
    try:
        payload = require_codec().jpegxl_encode(np.ascontiguousarray(pixels), lossless=True,
            effort=3, decodingspeed=3, photometric="rgb", usecontainer=False,
            numthreads=1, out=MAX_TILE_BYTES)
    except (ValueError, RuntimeError) as exc:
        raise EPError("EP tile encoding failed within its output budget.") from exc
    if type(payload) is not bytes or not 2 <= len(payload) <= MAX_TILE_BYTES:
        raise EPError("EP encoder exceeded its tile budget.")
    if not np.array_equal(decode_tile(payload), pixels):
        raise EPError("EP lossless tile round trip differs from the RGB reference.")
    return payload

def _lossy_distances(limit: int, *, distance: int = 4) -> tuple[float, ...]:
    from .ep_format import integer
    integer(limit, "RGB RMSE limit", 1, 15)
    integer(distance, "JXL distance", 1, 15)
    # Distance controls the initial codec setting, not the allowed RGB error.
    # Gentler candidates and the lossless fallback still require verification.
    return tuple(distance / 2**step for step in range(6))

def encode_lossy_tile(pixels: np.ndarray, width: int, height: int, limit: int, *, distance: int = 4, check_cancel=lambda: None, _on_verified=None):
    """Choose bounded JXL settings, verifying valid pixels and foreground."""
    from .ep_format import integer
    from .ep_quality import measure, within_limit
    integer(width, "valid tile width", 1, TILE_SIZE)
    integer(height, "valid tile height", 1, TILE_SIZE)
    integer(limit, "RGB RMSE limit", 1, 15)
    if not isinstance(pixels, np.ndarray) or pixels.dtype != np.uint8 or pixels.shape != (TILE_SIZE, TILE_SIZE, 3):
        raise EPError("EP encoding requires a 512x512 RGB8 tile.")
    reference = pixels[:height, :width]
    for distance in _lossy_distances(limit, distance=distance):
        check_cancel()
        try:
            payload = require_codec().jpegxl_encode(np.ascontiguousarray(pixels), lossless=False,
                distance=distance, effort=3, decodingspeed=3, photometric="rgb", usecontainer=False,
                numthreads=1, out=MAX_TILE_BYTES)
        except (ValueError, RuntimeError) as exc:
            raise EPError("EP tile encoding failed within its output budget.") from exc
        decoded = decode_tile(payload)
        if within_limit(measure(reference, decoded[:height, :width]), limit):
            if _on_verified is not None:
                decoded.flags.writeable = False
                _on_verified(payload, decoded)
            return payload, str(distance)
    check_cancel()
    return encode_tile(pixels), "lossless"

def decode_precision_tile(payload: bytes, bits: int) -> np.ndarray:
    from .ep_precision import expand_rgb_codes
    return expand_rgb_codes(decode_tile(payload), bits)

def encode_precision_tile(pixels, width, height, bits, limit, *, distance: int = 4, check_cancel=lambda: None):
    """Encode actual 5/6/7-bit JXL samples; positive limits include quantization."""
    from .ep_format import integer
    from .ep_precision import quantize_rgb, expand_rgb_codes
    from .ep_quality import measure, within_limit
    integer(bits, "reduced channel bits", 5, 7)
    integer(width, "valid tile width", 1, TILE_SIZE)
    integer(height, "valid tile height", 1, TILE_SIZE)
    integer(limit, "total RGB RMSE limit", 0, 15)
    integer(distance, "JXL distance", 1, 15)
    if not isinstance(pixels, np.ndarray) or pixels.dtype != np.uint8 or pixels.shape != (TILE_SIZE, TILE_SIZE, 3):
        raise EPError("EP encoding requires a 512x512 RGB8 tile.")
    codes = quantize_rgb(pixels, bits)
    quantized = expand_rgb_codes(codes, bits)
    reference = pixels[:height, :width]
    if limit and not within_limit(measure(reference, quantized[:height, :width]), limit):
        raise EPError("Selected channel precision cannot meet the total RGB error limit; choose more bits or a larger limit.")
    distances = (*_lossy_distances(limit, distance=distance), None) if limit else (None,)
    for distance in distances:
        check_cancel()
        settings = {} if distance is None else {"distance": distance}
        try:
            payload = require_codec().jpegxl_encode(codes, bitspersample=bits, lossless=distance is None,
                effort=3, decodingspeed=3, photometric="rgb", usecontainer=False,
                numthreads=1, out=MAX_TILE_BYTES, **settings)
        except (ValueError, RuntimeError) as exc:
            raise EPError("EP precision encoding failed within its output budget.") from exc
        decoded = decode_precision_tile(payload, bits)
        if distance is None and not np.array_equal(decoded, quantized):
            raise EPError("EP lossless coding changed the quantized reference.")
        if not limit or within_limit(measure(reference, decoded[:height, :width]), limit):
            return payload, "quantization-only" if distance is None else str(distance)
    raise EPError("EP precision output cannot meet its total RGB error limit.")
