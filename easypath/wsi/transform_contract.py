from __future__ import annotations

from typing import Literal

from typing import Annotated

from pydantic import Field

from pydantic import model_validator

from pydantic import model_serializer

from .ep_format import ENTRY

from .ep_format import HEADER

from .ep_format import MAX_FILE_BYTES

from .ep_format import MAX_METADATA

from .ep_format import MAX_TILE_BYTES

from .ep_format import EPError

from .ep_format import EPModel

from .ep_format import integer

from .transform_dicom import DicomExportInfo

from .transform_dicom import dicom_output_reservation

from .transform_pyramid import reference_workspace_bytes

RECEIPT_BUDGET = 64 * 1024 * 1024

class TransformLimit(EPError):
    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason

class TransformOptions(EPModel):
    output_format: Literal["ep", "tiff", "dcm"]
    assume_srgb: bool = False
    pixel_selection: Literal["single-default-native-rgb-plane","all-native-focal-planes"]
    focal_thickness_um: Annotated[float, Field(gt=0, le=1000)] | None = None
    focal_origin_um: float | None = None
    dicom: DicomExportInfo | None = None
    ep_error_percent: Annotated[int, Field(strict=True, ge=0, le=15)] | None = None
    ep_channel_bits: Annotated[int, Field(strict=True, ge=5, le=8)] | None = None
    ep_jxl_distance: Annotated[int, Field(strict=True, ge=1, le=15)] | None = None

    @model_validator(mode="after")
    def required_facts(self):
        if (self.output_format=="dcm" and self.pixel_selection=="all-native-focal-planes") != (self.focal_thickness_um is not None):
            raise ValueError("DICOM focus stacks require their measured optical section thickness.")
        if (self.focal_thickness_um is None) != (self.focal_origin_um is None):
            raise ValueError("Verify the first focal position in the slide coordinate system.")
        if (self.output_format == "dcm") != (self.dicom is not None):
            raise ValueError("DICOM output requires its own source facts; other formats must not include them.")
        if self.output_format != "ep" and (self.ep_error_percent is not None or self.ep_channel_bits is not None):
            raise ValueError("EP compression settings require EP output.")
        if self.ep_channel_bits is not None and self.ep_error_percent is None:
            raise ValueError("EP channel precision requires an explicit error limit.")
        if self.ep_jxl_distance is not None and (self.output_format != "ep" or not self.ep_error_percent):
            raise ValueError("JXL distance requires EP output with a positive error limit.")
        return self

    @model_serializer(mode="wrap")
    def legacy_serialization(self, handler):
        result = handler(self)
        if self.focal_thickness_um is None:
            result.pop("focal_thickness_um",None)
            result.pop("focal_origin_um",None)
        if self.ep_error_percent is None:
            result.pop("ep_error_percent", None)
        if self.ep_channel_bits is None:
            result.pop("ep_channel_bits", None)
        if self.ep_jxl_distance is None:
            result.pop("ep_jxl_distance", None)
        return result

def encoding_receipt(options, verification):
    """Expose the frozen EP choice without changing receipts for legacy jobs."""
    if options.pixel_selection == "all-native-focal-planes":
        zs = verification.get("focal_positions_um")
        if not isinstance(zs,(list,tuple)) or not 2 <= len(zs) <= 20 or verification.get("verified") is not True:
            raise EPError("Incomplete stack verification.")
        summary = dict(axis="scanner-focus",focal_positions_um=zs)
        if options.output_format=="ep":
            planes = verification.get("planes")
            if not isinstance(planes,list) or len(planes)!=len(zs):
                raise EPError("Missing EP focal verification.")
            plane_opts = options.model_copy(update={"pixel_selection":"single-default-native-rgb-plane"})
            summary["planes"] = [encoding_receipt(plane_opts,plane) for plane in planes]
        return dict(focal_stack=summary)
    if options.ep_error_percent is None:
        return {}
    from easypath.wsi.ep_quality import LOSSY_PROFILE, METRIC
    from easypath.wsi.ep_precision import PRECISION_PROFILE
    limit = options.ep_error_percent
    bits = options.ep_channel_bits or 8
    profile = PRECISION_PROFILE if bits < 8 else LOSSY_PROFILE if limit else "rgb8-jxl-lossless-v1"
    if verification.get("codec", {}).get("profile") != profile:
        raise EPError("EP verification profile differs from its requested compression.")
    summary = dict(maximum_nrmse_percent=limit, metric=METRIC, profile=profile)
    if options.ep_jxl_distance is not None:
        actual = verification["codec"].get("requested_distance")
        if type(actual) is not int or actual != options.ep_jxl_distance:
            raise EPError("EP verification distance differs from its requested compression.")
        summary["jxl_distance"] = actual
    if options.ep_channel_bits is not None:
        summary.update(channel_bits=bits, source_lossless=bits == 8 and limit == 0)
    if limit or bits < 8:
        quality = verification.get("quality", {})
        if (type(quality.get("maximum_nrmse_percent")) is not int or quality["maximum_nrmse_percent"] != limit
                or quality.get("verified_all_tiles") is not True or quality.get("padding_excluded") is not True
                or quality.get("associated_lossless") is not True or quality.get("metric") != METRIC):
            raise EPError("EP output lacks its requested quality verification.")
        if bits < 8 and (type(quality.get("channel_bits")) is not int or quality["channel_bits"] != bits or
                type(verification["codec"].get("channel_bits")) is not int or verification["codec"]["channel_bits"] != bits or
                quality.get("limit_scope") != ("total-source-rgb" if limit else "quantized-reference-lossless")):
            raise EPError("EP output lacks its requested precision verification.")
        summary["quality"] = quality
    elif verification.get("rgb_sse") != 0:
        raise EPError("EP lossless verification is not exact.")
    return {"ep_encoding": summary}

def conversion_reservation(metadata, output_format, stack=None):
    base = metadata.levels[0]
    short, long = sorted((base.width, base.height))
    if output_format == "ep" and long > 500_000:
        raise TransformLimit("dimensions", "EP supports up to 500,000 x 500,000 pixels per plane.")
    if output_format in {"tiff", "dcm"} and (short > 100_000 or long > 200_000):
        raise TransformLimit("dimensions", "TIFF and DICOM support up to 200,000 x 100,000 pixels per plane in either orientation.")
    if stack is None:
        return _storage_ceiling(metadata, output_format, bounded=True)
    stack.validate(output_format)
    n = len(stack.rasters)
    if stack.rasters[0].levels != metadata.levels or metadata.associated:
        raise EPError("Stack budget disagrees with the source raster.")
    if output_format == "ep":
        from easypath.wsi.ep_focus import HEAD, ROW
        per = HEADER.size + MAX_METADATA + sum(p.tiles for p in metadata.planes) * (ENTRY.size + MAX_TILE_BYTES)
        output = min(HEAD.size + n * (ROW.size + per), (MAX_FILE_BYTES - RECEIPT_BUDGET) // 4)
        scratch = 2 * output  # Member plus its compressed spool, beside the envelope.
    else:
        per = _storage_ceiling(metadata, output_format, bounded=True)["output_bytes"]
        output = n * per + 65536
        scratch = n * reference_workspace_bytes(metadata)
    total = scratch + 2 * output + RECEIPT_BUDGET
    if total > MAX_FILE_BYTES:
        raise TransformLimit("capacity", "Stack exceeds its combined storage limit.")
    return dict(reference_bytes=scratch, output_bytes=output, publication_copy_bytes=output,
                receipt_bytes=RECEIPT_BUDGET, total_bytes=total, focus_planes=n)

def _storage_ceiling(metadata, output_format, *, bounded):
    tiles = sum(plane.tiles for plane in metadata.planes)
    if output_format == "ep":
        output = HEADER.size + MAX_METADATA + tiles*(ENTRY.size + MAX_TILE_BYTES)
        if bounded:
            output = min(output, (MAX_FILE_BYTES - RECEIPT_BUDGET) // 3)
    elif output_format == "tiff":
        output = MAX_METADATA*2 + tiles*MAX_TILE_BYTES
    elif output_format == "dcm":
        output = dicom_output_reservation(metadata)
    else:
        raise ValueError("Unknown slide transformation output format.")
    scratch = output if output_format == "ep" else reference_workspace_bytes(metadata)
    # Include a second output-sized destination copy and bounded recovery
    # receipts. Retaining the original in place consumes no new source copy.
    total = scratch + 2*output + RECEIPT_BUDGET
    if bounded and total > MAX_FILE_BYTES:
        raise TransformLimit("capacity", "Transform exceeds its storage limit.")
    integer(total, "conversion reservation", 1, MAX_FILE_BYTES)
    return dict(reference_bytes=scratch, output_bytes=output,
                publication_copy_bytes=output, receipt_bytes=RECEIPT_BUDGET, total_bytes=total)
