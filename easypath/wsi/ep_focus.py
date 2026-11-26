from __future__ import annotations

import hashlib

import os

import stat

import struct

import threading

from types import SimpleNamespace

from functools import partial

from pathlib import Path

from .ep_container import DEFAULT_CACHE_BYTES

from .ep_container import EPFile

from .ep_format import EPError

from .ep_format import ENTRY

from .ep_format import HEADER

from .ep_format import MAX_FILE_BYTES

from .ep_format import MAX_METADATA

from .ep_format import MAX_TILES

from .ep_format import SOLID_STORAGE

from .ep_format import TILE_SIZE

from .ep_format import integer

from .ep_stream_writer import write_ep_streaming_artifact

from .focal_stack import FocusStack

from .transform_integrity import artifact_snapshot

STACK_MAGIC = b"EPZWSI\r\n"

HEAD = struct.Struct("<8sHHIQ32s")

ROW = struct.Struct("<dQQ32s")

COMPARE_BLOCK = 512 * 1024

INDEX_AND_RGB_BUDGET = MAX_TILES * ENTRY.size + DEFAULT_CACHE_BYTES

def _retained_rgb_limit(count, planes):
    available = INDEX_AND_RGB_BUDGET - count * ENTRY.size * planes - max(MAX_METADATA, 2 * COMPARE_BLOCK)
    return min(DEFAULT_CACHE_BYTES, available) if available >= 4 * TILE_SIZE**2 * 3 else 0

def _match_directory(member, header):
    # Exact comparison to bytes authenticated during open, including off-screen entries.
    f = member._file
    fields = HEADER.unpack(header)
    size, meta_hash = fields[4], fields[8]
    if hashlib.sha256(f.read(size)).digest() != meta_hash:
        raise EPError("Changed EP focus member metadata.")
    frozen = member._index
    for start in range(0, len(frozen), COMPARE_BLOCK):
        end = min(start + COMPARE_BLOCK, len(frozen))
        if f.read(end-start) != frozen[start:end]:
            raise EPError("Changed EP focus member index.")
    member._check_identity()

def open_ep_path(path, *, check_cancel=lambda: None):
    """Dispatch on the magic of the owned descriptor, without reopening by path."""
    stream = Path(path).open("rb",buffering=0)
    try:
        if stream.read(len(STACK_MAGIC)) == STACK_MAGIC:
            return EPFocusFile(stream=stream, owns_stream=True, check_cancel=check_cancel)
        opened=EPFile.__new__(EPFile)
        opened._open_stream(stream,owns_stream=True,pending=False,cache_bytes=DEFAULT_CACHE_BYTES)
        return opened
    except BaseException:
        stream.close()
        raise

class EPFocusFile:
    def __init__(self, path=None, *, stream=None, owns_stream=False, check_cancel=lambda: None):
        self.file = stream if stream is not None else Path(path).open("rb",buffering=0)
        self.owns_stream = stream is None or owns_stream
        self.lock = threading.RLock()
        self.active = None
        self._retained = []
        self._closed = False
        try:
            f = self.file
            f.seek(0)
            st = os.fstat(f.fileno())
            self.identity = EPFile._fingerprint(st)
            if not stat.S_ISREG(st.st_mode) or not HEAD.size <= st.st_size <= MAX_FILE_BYTES:
                raise EPError("Invalid EP focus file.")
            magic,version,has_z,n,total,checksum = HEAD.unpack(f.read(HEAD.size))
            if (magic,version) != (STACK_MAGIC,3) or has_z not in (0,1) or not 2 <= n <= 20 or total != st.st_size:
                raise EPError("Invalid EP focus header.")
            raw = f.read(n*ROW.size)
            if len(raw) != n*ROW.size or hashlib.sha256(raw).digest() != checksum:
                raise EPError("Invalid EP focus directory.")
            self.rows = tuple(ROW.iter_unpack(raw))
            at = HEAD.size + len(raw)
            zs, rasters = [], []
            expected_count = None
            for z,start,length,header_hash in self.rows:
                check_cancel()
                if (not has_z and z != 0 or start != at or not HEADER.size <= length <= total-at):
                    raise EPError("Invalid EP focus member range.")
                zs.append(z if has_z else None)
                f.seek(start)
                inner = f.read(HEADER.size)
                if len(inner) != HEADER.size or hashlib.sha256(inner).digest() != header_hash:
                    raise EPError("Changed EP focus member header.")
                count = HEADER.unpack(inner)[5]
                if expected_count is None:
                    expected_count = count
                    rgb_limit = _retained_rgb_limit(count, n)
                elif count != expected_count:
                    raise EPError("Focus plane tile counts differ.")
                member = self._member(start,length)
                if rgb_limit:
                    member._cache_limit = rgb_limit
                    self._retained.append(member)
                try:
                    rasters.append(member.metadata())
                finally:
                    if not rgb_limit:
                        member.close()
                del member
                at += length
            if at != total:
                raise EPError("Undeclared EP focus data.")
            self.stack = FocusStack(tuple(rasters),tuple(zs))
            self.stack.validate("ep")
            self._check()
        except BaseException:
            self.close()
            raise

    def _check(self):
        if self._closed or self.file.closed or self.identity != EPFile._fingerprint(os.fstat(self.file.fileno())):
            raise EPError("EP focus source changed or closed.")

    def _member(self,start,length):
        obj = EPFile.__new__(EPFile)
        obj._open_stream(self.file, owns_stream=False, pending=False,
                         cache_bytes=DEFAULT_CACHE_BYTES,span=(start,length))
        return obj

    def read_region(self,z,level,x,y,w,h):
        with self.lock:
            self._check()
            integer(z,"focus index",0,len(self.rows)-1)
            if self.active is None or self.active[0] != z:
                if self.active is not None:
                    if self._retained:
                        self.active[1].clear_cache()
                    else:
                        self.active[1].close()
                    self.active = None
                _,start,length,digest = self.rows[z]
                self.file.seek(0, os.SEEK_END)
                self.file.seek(start)
                header = self.file.read(HEADER.size)
                if hashlib.sha256(header).digest() != digest:
                    raise EPError("Changed focus member.")
                if self._retained:
                    member = self._retained[z]
                    _match_directory(member, header)
                else:
                    member = self._member(start,length)
                self.active = z,member
            return self.active[1].read_region(level,x,y,w,h)

    def close(self):
        with self.lock:
            if self._closed:
                return
            self._closed = True
            if self.active is not None and not self._retained:
                self.active[1].close()
            self.active = None
            for member in self._retained:
                member.close()
            self._retained.clear()
            if self.owns_stream:
                self.file.close()

    def metadata(self):
        self._check()
        return self.stack.rasters[0]

    def focus_view(self,z):
        integer(z,"focal index",0,len(self.rows)-1)
        return SimpleNamespace(metadata=self.metadata,read_region=partial(self.read_region,z))

    def clear_cache(self):
        with self.lock:
            self._check()
            if self.active: self.active[1].clear_cache()

    def cache_info(self):
        with self.lock:
            self._check()
            if self.active: return self.active[1].cache_info()
            return dict(limit_bytes=DEFAULT_CACHE_BYTES,bytes=0,tiles=0,hits=0,misses=0)
    def __enter__(self): return self
    def __exit__(self,*args): self.close()

def write_focus_ep(path, source, *, max_file_bytes, bits=8, loss=5, distance=4,
                   check_cancel=lambda: None, scratch_dir, admit_file_extent=None,
                   progress=lambda current, total: None):
    """Stream and verify each focus plane, then seal the single output in source order."""
    source.stack.validate("ep")
    positions = source.stack.positions_um
    path = Path(path)
    if path.suffix != ".partial":
        raise EPError("Stack conversion requires an owned partial output.")
    integer(bits, "channel bits", 5, 8)
    integer(loss, "loss limit", 0, 15)
    integer(distance, "distance", 1, 15)
    integer(max_file_bytes, "stack file budget", HEAD.size, MAX_FILE_BYTES)
    first_payload = HEAD.size + len(positions) * ROW.size
    source_bytes = first_payload
    members, reports = [], []
    records = bytearray()
    tiles = sum(p.tiles for p in source.metadata.planes)
    total_steps = 3 * tiles * len(positions)
    for i, (z, raster) in enumerate(zip(positions, source.stack.rasters, strict=True)):
        check_cancel()
        folder = scratch_dir / f"z{i:02d}"
        folder.mkdir()
        scratch = folder / "plane.ep.partial"
        updates = dict(storage_encoding=SOLID_STORAGE)
        if bits < 8:
            updates.update(profile="rgb-jxl-precision-v1", channel_bits=bits, maximum_nrmse_percent=loss)
        elif loss:
            updates.update(profile="rgb8-jxl-bounded-lossy-v1", maximum_nrmse_percent=loss)
        metadata = type(raster).model_validate(raster.model_dump() | updates)
        remaining = max_file_bytes - source_bytes
        if remaining < HEADER.size:
            raise EPError("EP stack exceeds its file budget.")
        def admit(local_extent):
            if not 0 < local_extent <= remaining:
                raise EPError("EP member exceeds the remaining file budget.")
            if admit_file_extent is not None:
                admit_file_extent(source_bytes + local_extent)
        def advanced(done, total):
            if total != 3 * tiles:
                raise EPError("Unexpected EP member tile count.")
            progress(i * total + done, total_steps)
        report = write_ep_streaming_artifact(
            scratch, metadata, partial(source.read, i), max_file_bytes=remaining,
            max_workspace_bytes=remaining, jxl_distance=distance, check_cancel=check_cancel,
            admit_file_extent=admit, progress=advanced,
        )
        check_cancel()
        with scratch.open("rb") as f:
            header = f.read(HEADER.size)
            length = os.fstat(f.fileno()).st_size
        if length != report["size_bytes"] or report["tile_count"] != tiles or length > remaining:
            raise EPError("Verified EP focal member length or tile count changed.")
        records.extend(ROW.pack(0 if z is None else z, source_bytes, length, hashlib.sha256(header).digest()))
        source_bytes += length
        members.append(scratch)
        reports.append(report)
    check_cancel()
    with path.open("xb+") as out:
        owner = os.fstat(out.fileno())
        out.write(HEAD.pack(STACK_MAGIC, 3, int(positions[0] is not None), len(positions), source_bytes,
                            hashlib.sha256(records).digest()))
        out.write(records)
        for member, report in zip(members, reports, strict=True):
            digest = hashlib.sha256()
            remaining = report["size_bytes"]
            with member.open("rb") as src:
                while remaining:
                    check_cancel()
                    block = src.read(min(1 << 20, remaining))
                    if not block:
                        raise EPError("Truncated EP focal member.")
                    out.write(block)
                    digest.update(block)
                    remaining -= len(block)
                if src.read(1) or digest.hexdigest() != report["sha256"]:
                    raise EPError("EP focal member changed during assembly.")
        out.flush()
        os.fsync(out.fileno())
    before = artifact_snapshot(path, check_cancel)
    with EPFocusFile(path) as reader:
        st = os.fstat(reader.file.fileno())
        if ((st.st_dev, st.st_ino) != (owner.st_dev, owner.st_ino) or reader.stack.positions_um != positions
                or st.st_size != source_bytes):
            raise EPError("EP stack verification opened the wrong artifact.")
        for (_, at, length, _), report in zip(reader.rows, reports, strict=True):
            reader.file.seek(at)
            digest = hashlib.sha256()
            while length:
                check_cancel()
                block = reader.file.read(min(length, 1 << 20))
                if not block:
                    raise EPError("Truncated EP focal member.")
                digest.update(block)
                length -= len(block)
            if digest.hexdigest() != report["sha256"]:
                raise EPError("Embedded EP plane does not match its verified pixels.")
    if before != artifact_snapshot(path, check_cancel):
        raise EPError("EP stack changed during verification.")
    return dict(before, format="ep-focus-v3", focal_positions_um=positions, planes=reports,
                verified=True, registered=False)
