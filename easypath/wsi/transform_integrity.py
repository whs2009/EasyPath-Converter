from __future__ import annotations

import hashlib

import os

from pathlib import Path

import stat

from .ep_format import EPError

from .ep_format import MAX_FILE_BYTES

def artifact_snapshot(path, check_cancel=lambda: None):
    path = Path(path)
    before = path.lstat()
    def identity(info):
        return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns)
    if not stat.S_ISREG(before.st_mode) or not 1 <= before.st_size <= MAX_FILE_BYTES:
        raise EPError("Conversion artifact is not a bounded regular file.")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        if identity(os.fstat(stream.fileno())) != identity(before):
            raise EPError("Conversion artifact changed while opening.")
        while block := stream.read(1024*1024):
            check_cancel()
            digest.update(block)
        if identity(os.fstat(stream.fileno())) != identity(before):
            raise EPError("Conversion artifact changed during hashing.")
    after = path.lstat()
    if not stat.S_ISREG(after.st_mode) or identity(before) != identity(after):
        raise EPError("Conversion artifact path changed during hashing.")
    return {"sha256":digest.hexdigest(),"size_bytes":before.st_size,"identity":identity(before)}
