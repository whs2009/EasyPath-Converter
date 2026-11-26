from __future__ import annotations

import hashlib

import os

from pathlib import Path

import numpy as np

from .ep_codec import codec_identity

from .ep_codec import encode_tile

from .ep_codec import encode_lossy_tile

from .ep_codec import encode_precision_tile

from .ep_codec import decode_tile

from .ep_codec import decode_precision_tile

from .ep_container import EPFile

from .ep_format import ENTRY

from .ep_format import HEADER

from .ep_format import MAGIC

from .ep_format import MAX_FILE_BYTES

from .ep_format import MAX_TILE_BYTES

from .ep_format import TILE_SIZE

from .ep_format import SOLID_STORAGE

from .ep_format import EPError

from .ep_format import EPMetadata

from .ep_format import integer

from .ep_records import uniform_record

from .ep_records import wrap_jxl

from .ep_records import decode_record

from .ep_records import split_record

from .ep_precision import PRECISION_PROFILE

from .ep_quality import LOSSY_PROFILE

from .ep_quality import QualitySummary

from .ep_stream_pyramid import stream_pyramid

from .ep_stream_pyramid import DEFAULT_PENDING_BYTES

from .ep_uniform_cache import UniformTileCache

from .ep_uniform_cache import uniform_rgb

from .ep_uniform_cache import DEFAULT_UNIFORM_CACHE_BYTES

def _verified_rgb8_record(record, width, height, proof):
    # Reuse only the accepted decode of this exact codestream. Final disk replay is independent.
    integer(width, "valid tile width", 1, TILE_SIZE)
    integer(height, "valid tile height", 1, TILE_SIZE)
    kind, content = split_record(record)
    encoded, pixels = proof
    if (kind != "jxl" or type(encoded) is not bytes or content != encoded
            or not isinstance(pixels, np.ndarray) or pixels.shape != (TILE_SIZE, TILE_SIZE, 3)
            or pixels.dtype != np.uint8 or pixels.flags.writeable or not pixels.flags.c_contiguous
            or not pixels.flags.owndata):
        raise EPError("Accepted RGB8 pixels do not bind to the encoded record.")
    return pixels

def _fingerprint(file):
    info = os.fstat(file.fileno())
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns

def _path_owned(path, identity):
    info = path.lstat()
    if path.is_symlink() or (info.st_dev, info.st_ino) != identity[:2]:
        raise EPError("EP streaming artifact ownership changed.")

def write_ep_streaming_artifact(path, metadata, read_source_plane, *, max_file_bytes,
                                max_workspace_bytes, max_pending_bytes=DEFAULT_PENDING_BYTES, jxl_distance=4,
                                uniform_cache_bytes=DEFAULT_UNIFORM_CACHE_BYTES,
                                check_cancel=lambda: None, progress=lambda current, total: None,
                                admit_file_extent=None):
    """Retain failed partials/spools; only the existing publisher registers results.

    Each decoded payload is checked against its live canonical reference. Its
    pixel SHA then binds final, uncached disk replay to those same measured
    pixels. This avoids retaining a whole raw pyramid without omitting either
    source quality measurement or complete final-file decoding.
    """
    if not isinstance(metadata, EPMetadata):
        raise EPError("EP streaming writer requires validated metadata.")
    from .ep_format import check_transform_dimensions
    check_transform_dimensions(metadata, "ep")
    path = Path(path)
    if path.suffix != ".partial":
        raise EPError("EP writer only writes task-owned .partial artifacts.")
    integer(max_file_bytes, "file budget", HEADER.size, MAX_FILE_BYTES)
    integer(max_workspace_bytes, "compressed spool budget", 1, MAX_FILE_BYTES)
    integer(max_pending_bytes, "pending tile budget", 1, 1024**3)
    integer(jxl_distance, "JXL distance", 1, 15)
    uniform_cache = UniformTileCache(uniform_cache_bytes)
    raw = metadata.to_bytes()
    count = sum(p.tiles for p in metadata.planes)
    index_at = HEADER.size + len(raw)
    payload_at = index_at + count*ENTRY.size
    if payload_at + 2*count > max_file_bytes or 2*count > max_workspace_bytes:
        raise EPError("EP budget cannot contain its directory and payloads.")
    starts, index = [], 0
    for plane in metadata.planes:
        starts.append(index)
        index += plane.tiles
    entries = bytearray(count*ENTRY.size)
    pixel_hashes = bytearray(count*32)
    seen = bytearray(count)
    lossy = metadata.profile in (LOSSY_PROFILE, PRECISION_PROFILE)
    quality = QualitySummary(metadata) if lossy else None
    choices = {}
    records = metadata.storage_encoding == SOLID_STORAGE
    storage = dict(version=2, solid_tiles=0, jxl_tiles=0)
    identity = codec_identity()
    if lossy:
        identity.update(profile=metadata.profile, effort="3", decoding_speed="3",
            resampling_control="not exposed by installed wrapper", distance_search="bounded-user-distance-v2")
        if metadata.maximum_nrmse_percent:
            identity['requested_distance'] = jxl_distance
        if metadata.channel_bits is not None:
            identity['channel_bits'] = metadata.channel_bits
    check_cancel()
    spool_path = path.with_name(path.name + ".tiles")
    if admit_file_extent is not None:
        admit_file_extent(payload_at)
    with path.open('xb+') as output, spool_path.open('xb+', buffering=0) as spool:
        owner, spool_owner = _fingerprint(output), _fingerprint(spool)
        output.write(bytes(HEADER.size))
        current = samples = 0

        def emit(plane, tx, ty, reference):
            nonlocal current, samples
            check_cancel()
            size = metadata.planes[plane]
            x, y = tx*TILE_SIZE, ty*TILE_SIZE
            w, h = min(TILE_SIZE, size.width-x), min(TILE_SIZE, size.height-y)
            if not isinstance(reference, np.ndarray) or reference.dtype != np.uint8 or reference.shape != (h, w, 3):
                raise EPError("EP source returned a different pixel shape or precision.")
            at = starts[plane] + ty*size.columns + tx
            if seen[at]:
                raise EPError("Duplicate streaming EP tile.")
            tile = np.full((TILE_SIZE, TILE_SIZE, 3), 255, np.uint8)
            tile[:h, :w] = reference
            volume = plane < len(metadata.levels)
            bits = metadata.channel_bits if volume else None
            solid = uniform_record(reference, channel_bits=bits) if records else None
            color = uniform_rgb(tile) if uniform_cache.limit and w == h == TILE_SIZE else None
            cache_key = (volume, *color) if color is not None else None
            cached = uniform_cache.get(cache_key) if cache_key is not None else None
            choice = proof = None

            def accepted(encoded, pixels):
                nonlocal proof
                if proof is not None:
                    raise EPError("More than one accepted decode for one tile.")
                proof = (encoded, pixels)

            if cached is not None:
                payload, decoded, choice = cached
            elif solid is not None:
                payload, choice = solid, 'solid'
            elif lossy and volume:
                if metadata.channel_bits is not None:
                    payload, choice = encode_precision_tile(tile, w, h, metadata.channel_bits,
                        metadata.maximum_nrmse_percent, distance=jxl_distance, check_cancel=check_cancel)
                elif records and metadata.maximum_nrmse_percent == 5 and jxl_distance == 4:
                    payload, choice = encode_lossy_tile(tile, w, h, 5, distance=jxl_distance,
                        check_cancel=check_cancel, _on_verified=accepted)
                else:
                    payload, choice = encode_lossy_tile(tile, w, h, metadata.maximum_nrmse_percent,
                        distance=jxl_distance, check_cancel=check_cancel)
            else:
                payload = encode_tile(tile)
            if records:
                if cached is None and solid is None:
                    payload = wrap_jxl(payload)
                storage['solid_tiles' if payload[0] == 1 else 'jxl_tiles'] += 1
            if lossy and volume:
                choices[choice] = choices.get(choice, 0) + 1
            check_cancel()
            if not 2 <= len(payload) <= MAX_TILE_BYTES:
                raise EPError("Encoded EP tile exceeds its payload bound.")
            if cached is None:
                if proof is not None:
                    decoded = _verified_rgb8_record(payload, w, h, proof)
                elif records:
                    decoded = decode_record(payload, w, h, channel_bits=bits)
                else:
                    decoded = decode_precision_tile(payload, bits) if bits is not None else decode_tile(payload)
            if quality is not None:
                quality.add(plane, x, y, reference, decoded[:h, :w])
            elif not np.array_equal(tile, decoded):
                raise EPError("Lossless streaming EP tile is not exact.")
            if cache_key is not None and cached is None:
                uniform_cache.put(cache_key, payload, decoded, choice)
            offset = spool.tell()
            if len(payload) > min(max_workspace_bytes, max_file_bytes-payload_at) - offset:
                raise EPError("EP compressed spool exceeded its reserved budget.")
            if admit_file_extent is not None:
                admit_file_extent(payload_at + offset + len(payload))
            spool.write(payload)
            ENTRY.pack_into(entries, at*ENTRY.size, offset, len(payload), hashlib.sha256(payload).digest())
            pixel_hashes[at*32:(at+1)*32] = hashlib.sha256(decoded[:h, :w].tobytes()).digest()
            seen[at] = 1
            current += 1
            samples += w*h*3
            progress(current, count*3)

        stats = stream_pyramid(metadata, read_source_plane, emit,
            max_pending_bytes=max_pending_bytes, check_cancel=check_cancel)
        if current != count or not all(seen):
            raise EPError("Streaming EP did not cover every tile.")
        quality_report = quality.report() if quality is not None else None
        spool.flush()
        os.fsync(spool.fileno())
        frozen_spool = _fingerprint(spool)
        _path_owned(spool_path, spool_owner)
        output.seek(HEADER.size)
        output.write(raw)
        output.seek(payload_at)
        for at in range(count):
            check_cancel()
            offset, length, digest = ENTRY.unpack_from(entries, at*ENTRY.size)
            if _fingerprint(spool) != frozen_spool:
                raise EPError("Compressed EP spool changed.")
            spool.seek(offset)
            payload = spool.read(length)
            if len(payload) != length or hashlib.sha256(payload).digest() != digest or _fingerprint(spool) != frozen_spool:
                raise EPError("Compressed EP spool checksum mismatch.")
            position = output.tell()
            if len(payload) > max_file_bytes - position:
                raise EPError("EP output exceeded its reserved file budget.")
            output.write(payload)
            ENTRY.pack_into(entries, at*ENTRY.size, position, length, digest)
            current += 1
            progress(current, count*3)
        _path_owned(spool_path, spool_owner)
        total = output.tell()
        output.seek(index_at)
        output.write(entries)
        args = (MAGIC, 2 if records else 1, 0, 0, len(raw), count, index_at, total,
                hashlib.sha256(raw).digest(), hashlib.sha256(entries).digest(), bytes(16))
        output.seek(0)
        output.write(HEADER.pack(*args))
        output.flush()
        os.fsync(output.fileno())
        verified_identity = _fingerprint(output)
        with EPFile(path, _pending=True, cache_bytes=0) as verification:
            if verification._identity[:2] != owner[:2]:
                raise EPError("EP artifact ownership changed before verification.")
            verified = 0

            def inspect(plane, x, y, pixels):
                nonlocal current, verified
                at = starts[plane] + (y//TILE_SIZE)*metadata.planes[plane].columns + x//TILE_SIZE
                if at != verified or hashlib.sha256(pixels.tobytes()).digest() != pixel_hashes[at*32:(at+1)*32]:
                    raise EPError("Final EP decoded pixels differ from their quality-checked reference binding.")
                verified += 1
                current += 1
                progress(current, count*3)

            verification.validate_all_tiles(check_cancel, inspect_tile=inspect)
            if verified != count:
                raise EPError("Final EP verification missed tiles.")
        check_cancel()
        if _fingerprint(output) != verified_identity:
            raise EPError("EP artifact changed during final verification.")
        final_header = HEADER.pack(*args[:3], 1, *args[4:])
        digest = hashlib.sha256(final_header)
        output.seek(HEADER.size)
        for block in iter(lambda: output.read(1024*1024), b''):
            check_cancel()
            digest.update(block)
        _path_owned(path, owner)
        if _fingerprint(output) != verified_identity:
            raise EPError("EP artifact changed after final verification.")
        _path_owned(spool_path, spool_owner)
        if _fingerprint(spool) != frozen_spool:
            raise EPError("Compressed EP spool changed before completion.")
        check_cancel()
        output.seek(0)
        output.write(final_header)
        output.flush()
        os.fsync(output.fileno())
    result = dict(sha256=digest.hexdigest(), size_bytes=total, tile_count=count,
        rgb_sample_count=samples, rgb_sse=0, rgb_nrmse_percent=0.0,
        reference="caller-supplied canonical sRGB planes; padded pixels excluded", codec=identity, registered=False,
        streaming=dict(**stats, uniform_cache=uniform_cache.info(), compressed_spool_bytes=frozen_spool[2],
            directory_and_pixel_bindings_bytes=len(entries)+len(pixel_hashes)+len(seen),
            verification="live-reference quality plus uncached final-file decoded-pixel SHA256 replay"))
    if records:
        result['storage'] = storage
    if quality is not None:
        sse = sum(row['sse'] for row in quality.rows)
        result.update(rgb_sse=sse, rgb_nrmse_percent=100/255*(sse/samples)**0.5,
                      quality=quality_report, volume_encoding_choices=choices)
    return result
