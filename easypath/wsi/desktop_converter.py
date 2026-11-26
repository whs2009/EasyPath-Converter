from __future__ import annotations

import ctypes

import hashlib

import json

import os

import shutil

import sys

import tempfile

from pathlib import Path

from pathlib import PureWindowsPath

from .ep_format import EPError

from .ep_format import SOLID_STORAGE

from .transform_contract import TransformOptions

from .transform_contract import conversion_reservation

from .transform_contract import encoding_receipt

from .transform_source import NativeTransformSource

def output_path(directory, stem, fmt):
    if fmt not in ("ep", "tiff", "dcm"):
        raise ValueError("Unsupported output format.")
    root = Path(directory).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Choose an existing destination folder.")
    if (not stem or stem != stem.strip() or stem.endswith(".") or len(stem.encode("utf8")) > 200
            or any(c in '<>:"/\\|?*' or ord(c) < 32 for c in stem)
            or PureWindowsPath(stem).is_reserved()):
        raise ValueError("Enter a valid output name, without a path.")
    return root / (stem + "." + fmt)

def publish(source, dest):
    """Only an atomic, no-replace rename can publish a complete verified product."""
    if os.name == "nt":
        os.rename(source, dest)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    old, new = os.fsencode(source), os.fsencode(dest)
    if sys.platform == "darwin":
        rc = libc.renamex_np(old, new, 4)  # RENAME_EXCL
    elif sys.platform.startswith("linux"):
        rc = libc.renameat2(-100, old, -100, new, 1)  # RENAME_NOREPLACE
    else:
        raise OSError("No safe publication primitive on this system.")
    if rc:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), str(dest))

def _stamp(path, stop):
    def entry(p):
        stop()
        if p.is_symlink() or getattr(p, "is_junction", lambda: False)():
            raise ValueError("Linked source package members are not supported.")
        st = p.stat()
        return (str(p), st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
    result = [entry(path)]
    if path.suffix.lower() == ".mrxs":
        companion = path.with_suffix("")
        if not companion.is_dir():
            raise ValueError("The MRXS companion folder must be beside the input.")
        result.append(entry(companion))
        for member in companion.rglob("*"):
            if len(result) >= 100000:
                raise ValueError("Source package exceeds the inventory limit.")
            result.append(entry(member))
    return sorted(result)

def _facts(path):
    if not path:
        raise ValueError("Select verified DICOM facts for this input.")
    with Path(path).open("rb") as f:
        raw = f.read(1_048_577)
    if len(raw) > 1_048_576:
        raise ValueError("DICOM facts file exceeds the size limit.")
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) - {"dicom", "focal_thickness_um", "focal_origin_um"}:
        raise ValueError("Expected a facts document with a dicom object.")
    from .transform_dicom import DicomExportInfo
    if "dicom" not in value:
        raise ValueError("Missing dicom object.")
    value["dicom"] = DicomExportInfo.model_validate_json(json.dumps(value["dicom"], allow_nan=False))
    return value

def convert(path, directory, stem, fmt="ep", *, bits=8, loss=5, distance=4,
            assume_srgb=False, facts=None, stop=lambda: None, progress=lambda value: None):
    dest = output_path(directory, stem, fmt)
    path = Path(path).resolve(strict=True)
    if not path.is_file() or os.path.lexists(dest):
        raise ValueError("Input must be a file and output must not already exist.")
    if path.suffix.lower() == ".mrxs" and dest.parent.is_relative_to(path.with_suffix("")):
        raise ValueError("Choose a destination outside the MRXS source package.")
    if not (type(bits) is type(loss) is type(distance) is int and 5 <= bits <= 8
            and 0 <= loss <= 15 and 1 <= distance <= 15):
        raise ValueError("Invalid EP settings.")
    spec = _facts(facts) if fmt == "dcm" else {}
    original = _stamp(path, stop)
    with tempfile.TemporaryDirectory(prefix=".easypath-convert-", dir=dest.parent) as temp:
        work = Path(temp)
        artifact = work / ("slide." + fmt + ".partial")
        with NativeTransformSource.open(path, assume_srgb=assume_srgb, check_cancel=stop,
                                        ep_read_ahead=fmt == "ep") as source:
            stack = getattr(source, "stack", None)
            options = TransformOptions(output_format=fmt, assume_srgb=assume_srgb,
                pixel_selection="all-native-focal-planes" if stack else "single-default-native-rgb-plane",
                ep_channel_bits=bits if fmt == "ep" else None, ep_error_percent=loss if fmt == "ep" else None,
                ep_jxl_distance=distance if fmt == "ep" and loss else None, **spec)
            meta = source.metadata
            if fmt == "dcm":
                if not meta.mpp_x or not meta.mpp_y:
                    raise EPError("DICOM requires verified X and Y pixel spacing.")
                if {a.role for a in options.dicom.associated} != {a.name for a in meta.associated}:
                    raise EPError("DICOM facts must describe every associated image.")
                if stack and options.focal_origin_um != stack.positions_um[0]:
                    raise EPError("Confirmed focal origin differs from the source.")
            if fmt == "ep":
                update = {"storage_encoding": SOLID_STORAGE}
                if bits != 8:
                    from .ep_precision import PRECISION_PROFILE
                    update.update(profile=PRECISION_PROFILE, channel_bits=bits, maximum_nrmse_percent=loss)
                elif loss:
                    from .ep_quality import LOSSY_PROFILE
                    update.update(profile=LOSSY_PROFILE, maximum_nrmse_percent=loss)
                meta = meta.model_copy(update=update)
            budget = conversion_reservation(meta, fmt, stack)
            free = shutil.disk_usage(work).free - (64 << 20)
            if fmt == "ep":
                maximum = min(budget["output_bytes"], free // (3 if stack else 2))
            else:
                maximum = min(budget["output_bytes"], free - budget["reference_bytes"])
            if maximum < 1:
                raise EPError("Insufficient destination space.")
            args = dict(max_file_bytes=maximum, check_cancel=stop)
            def tiles(done, total):
                progress(.98 * done / total)
            if stack:
                from .transform_focus import write_focus_work
                report = write_focus_work(source, work, fmt, bits=bits, loss=loss, distance=distance,
                    dicom=options.dicom, thickness_um=options.focal_thickness_um, progress=tiles, **args)
            elif fmt == "ep":
                from .ep_stream_writer import write_ep_streaming_artifact
                from .ep_volume_color import ep_volume_color
                from .ep_source_reads import ep_writer_reads
                with ep_volume_color(source) as left, ep_writer_reads(source, left) as (read, pending):
                    report = write_ep_streaming_artifact(artifact, meta, read, max_pending_bytes=pending,
                        max_workspace_bytes=maximum, jxl_distance=distance, progress=tiles, **args)
            else:
                from .transform_pyramid import RGBPyramid
                with RGBPyramid.build(work/"reference", meta, source.read_plane,
                        max_workspace_bytes=budget["reference_bytes"], check_cancel=stop,
                        progress=lambda v: progress(v*.4)) as pyramid:
                    post = lambda v: progress(.4 + .58*v)
                    if fmt == "tiff":
                        from .transform_tiff import write_tiff_artifact
                        report = write_tiff_artifact(artifact, pyramid, progress=post, **args)
                    else:
                        from .transform_dicom import write_dicom_artifact
                        report = write_dicom_artifact(artifact, pyramid, options.dicom, progress=post, **args)
            encoding_receipt(options, report)
        stop()
        if original != _stamp(path, stop):
            raise EPError("Input changed during conversion.")
        if fmt == "dcm":
            files = {p: p.name for p in artifact.iterdir()}
            if not files or set(files.values()) != set(report["files"]):
                raise EPError("Verified DICOM set changed.")
            hashes = {p: report["files"][name]["sha256"] for p, name in files.items()}
        else:
            hashes = {artifact: report["sha256"]}
        for file, expected in hashes.items():
            digest = hashlib.sha256()
            with file.open("rb") as f:
                while chunk := f.read(4 << 20):
                    stop()
                    digest.update(chunk)
            if digest.hexdigest() != expected:
                raise EPError("Output changed after verification.")
        stop()
        publish(artifact, dest)
        progress(1.)
    return dest
