from __future__ import annotations

from datetime import datetime

import hashlib

import math

import os

from pathlib import Path

from typing import Annotated

from typing import Literal

import imagecodecs

import numpy as np

from pydantic import Field

from pydantic import model_validator

from .ep_format import EPError

from .ep_format import EPModel

from .ep_format import MAX_FILE_BYTES

from .ep_format import MAX_REGION_PIXELS

from .ep_format import TILE_SIZE

from .ep_format import integer

from .transform_pyramid import REFERENCE_POLICY

from .transform_integrity import artifact_snapshot

class CompressionHistory(EPModel):
    method: Literal["ISO_10918_1", "ISO_14495_1", "ISO_15444_1", "ISO_15444_15", "ISO_18181_1"]
    ratio: Annotated[float, Field(gt=0, le=1_000_000)]

class SourceImageFacts(EPModel):
    acquisition_datetime: Annotated[str, Field(max_length=64)]
    focus_method: Literal["AUTO", "MANUAL"]
    source_was_lossy: bool
    compression_history: Annotated[tuple[CompressionHistory, ...], Field(max_length=16)] = ()
    contains_patient_text: bool

    @model_validator(mode="after")
    def validate_image_facts(self):
        value = datetime.fromisoformat(self.acquisition_datetime)
        offset = value.utcoffset()
        if (offset is None or value.year < 1900 or offset.total_seconds() % 60
                or abs(offset.total_seconds()) > 14*3600):
            raise ValueError("DICOM acquisition time requires a known date and timezone.")
        if self.source_was_lossy != bool(self.compression_history):
            raise ValueError("DICOM export requires the source compression history, not an invented lossless claim.")
        return self

class AssociatedExportInfo(SourceImageFacts):
    role: Literal["overview", "macro", "label"]

class DicomExportInfo(SourceImageFacts):
    imaged_depth_um: Annotated[float, Field(gt=0, le=1000)]
    origin_x_mm: Annotated[float, Field(ge=-1000, le=1000)]
    origin_y_mm: Annotated[float, Field(ge=-1000, le=1000)]
    rotation_degrees: Annotated[float, Field(ge=0, lt=360)]
    associated: Annotated[tuple[AssociatedExportInfo, ...], Field(max_length=3)] = ()
    slide_identifier: Annotated[str, Field(min_length=1, max_length=64)]
    specimen_identifier: Annotated[str, Field(min_length=1, max_length=64)]
    patient_identifier: Annotated[str, Field(max_length=64)] = ""
    patient_name: Annotated[str, Field(max_length=64)] = ""

    @model_validator(mode="after")
    def validate_source_facts(self):
        if len({item.role for item in self.associated}) != len(self.associated):
            raise ValueError("DICOM associated-image source facts must have distinct roles.")
        for value in (self.slide_identifier, self.specimen_identifier, self.patient_identifier, self.patient_name):
            if any(ord(c) < 32 or c == "\\" for c in value):
                raise ValueError("Invalid DICOM text value.")
        return self

def dicom_output_reservation(metadata) -> int:
    volume = sum(p.tiles for p in metadata.levels) * (4*1024*1024 + 64)
    associated = sum(max(4*1024*1024,p.width*p.height*6+1024*1024) for p in metadata.associated)
    return volume + associated + len(metadata.planes)*2*1024*1024

def write_dicom_artifact(directory: Path, pyramid, info: DicomExportInfo, *, max_file_bytes: int,
                         check_cancel=lambda: None, progress=lambda value: None, focal_stack=None):
    from pydicom import dcmread
    from pydicom.multival import MultiValue
    from pydicom.uid import JPEG2000Lossless, VLWholeSlideMicroscopyImageStorage, generate_uid
    from wsidicom import WsiDicom
    from wsidicom.codec.encoder import Jpeg2kEncoder
    from wsidicom.codec.settings import Channels, Jpeg2kSettings
    from wsidicom.config import Settings, use_settings
    from wsidicom.geometry import PointMm, Size, SizeMm
    from wsidicom.instance import WsiInstance
    from wsidicom.instance.image_data import ImageData
    from wsidicom.metadata import (
        Equipment, Image, ImageCoordinateSystem, Label, OpticalPath, Patient,
        Pyramid, Series, Slide, Study, WsiMetadata,
    )
    from wsidicom.metadata.image import FocusMethod, ImageType, LossyCompression
    from wsidicom.codec import LossyCompressionIsoStandard
    from wsidicom.metadata.optical_path import Objectives
    from wsidicom.metadata.sample import SlideSample
    from wsidicom.metadata.schema.dicom.wsi import WsiMetadataDicomSchema
    from wsidicom.source import Source

    integer(max_file_bytes,"DICOM output budget",1,MAX_FILE_BYTES)
    meta = pyramid.metadata()
    from .ep_format import check_transform_dimensions
    check_transform_dimensions(meta, "dcm")
    positions = (0.0,)
    references = (pyramid,)
    thickness_um = info.imaged_depth_um
    if focal_stack is not None:
        from .focal_stack import FocusStack
        positions, references, thickness_um = focal_stack
        FocusStack(tuple(ref.metadata() for ref in references), positions).validate("dcm")
        if references[0] is not pyramid:
            raise EPError("DICOM focal reference does not match its first plane.")
        if (type(thickness_um) not in (int, float) or not math.isfinite(thickness_um)
                or not 0 < thickness_um <= info.imaged_depth_um
                or info.imaged_depth_um < positions[-1] - positions[0]):
            raise EPError("DICOM requires a known section thickness and adequate imaged volume depth.")
    if meta.mpp_x is None or meta.mpp_y is None:
        raise EPError("DICOM WSI export requires verified pixel spacing on both axes.")
    associated_facts = {item.role: item for item in info.associated}
    if set(associated_facts) != {plane.name for plane in meta.associated}:
        raise EPError("DICOM export requires exact per-associated-image source facts.")
    facts = [info]*len(meta.levels) + [associated_facts[plane.name] for plane in meta.associated]
    if max_file_bytes < len(positions)*dicom_output_reservation(meta):
        raise EPError("Insufficient reserved DICOM output space.")
    directory = Path(directory)
    if directory.suffix != ".partial":
        raise EPError("DICOM export requires a fresh owned .partial directory.")
    if os.name == "nt" and len(str(directory.resolve()).encode("utf-16-le"))//2 + 49 > 259:
        raise EPError("DICOM workspace path is too long; use a shorter owned workspace.")
    check_cancel()
    directory.mkdir(exist_ok=False)
    owned = directory.stat()
    settings = Settings(decoded_frame_cache_size=0,encoded_frame_cache_size=0,strict_uid_check=True)
    coordinate = ImageCoordinateSystem(PointMm(info.origin_x_mm,info.origin_y_mm),info.rotation_degrees)
    history = [LossyCompression(LossyCompressionIsoStandard(h.method),h.ratio) for h in info.compression_history]
    optical = OpticalPath(identifier="0",color_space="SRGB",
        objective=Objectives(objective_power=meta.objective_power))
    image = Image(acquisition_datetime=datetime.fromisoformat(info.acquisition_datetime),
        focus_method=FocusMethod[info.focus_method],image_coordinate_system=coordinate,
        pixel_spacing=SizeMm(meta.mpp_x/1000,meta.mpp_y/1000),depth_of_field=thickness_um/1000,
        lossy_compressions=history)
    metadata = WsiMetadata(study=Study(uid=generate_uid()),series=Series(uid=generate_uid()),
        patient=Patient(name=info.patient_name,identifier=info.patient_identifier),
        equipment=Equipment(),slide=Slide(identifier=info.slide_identifier,
            samples=[SlideSample(identifier=info.specimen_identifier,uid=generate_uid())]),
        pyramid=Pyramid(image=image,optical_paths=[optical],uid=generate_uid(),contains_phi=info.contains_patient_text),
        label=Label(contains_phi=info.contains_patient_text),frame_of_reference_uid=generate_uid(),
        dimension_organization_uids=[generate_uid()])
    total = len(positions)*sum(p.tiles for p in meta.levels) + len(meta.associated)
    encoded = 0

    class LosslessEncoder(Jpeg2kEncoder):
        def encode(self, pixels):
            if pixels.dtype != np.uint8 or pixels.ndim != 3 or pixels.shape[2] != 3 or pixels.shape[0]*pixels.shape[1] > MAX_REGION_PIXELS:
                raise EPError("DICOM codec input exceeds its RGB8 bounds.")
            limit = max(4*1024*1024,pixels.nbytes*2+1024*1024)
            payload = imagecodecs.jpeg2k_encode(pixels,level=0,reversible=True,bitspersample=8,
                codecformat="J2K",mct=False,numthreads=1,out=limit)
            verified = np.empty_like(pixels)
            imagecodecs.jpeg2k_decode(payload,numthreads=1,out=verified)
            if not np.array_equal(pixels,verified):
                raise EPError("DICOM lossless codec changed reference pixels.")
            return bytes(payload)

    encoder = LosslessEncoder(Jpeg2kSettings(levels=0,bits=8,channels=Channels.RGB))

    class Plane(ImageData):
        def __init__(self, index):
            super().__init__(encoder)
            self.index = index
            self.plane = meta.planes[index]
            self.volume = index < len(meta.levels)

        @property
        def transfer_syntax(self): return JPEG2000Lossless
        @property
        def image_size(self): return Size(self.plane.width,self.plane.height)
        @property
        def tile_size(self): return Size(TILE_SIZE,TILE_SIZE) if self.volume else self.image_size
        @property
        def pixel_spacing(self):
            return SizeMm(meta.mpp_x*2**self.index/1000,meta.mpp_y*2**self.index/1000) if self.volume else None
        @property
        def imaged_size(self):
            return SizeMm(meta.levels[0].width*meta.mpp_x/1000,meta.levels[0].height*meta.mpp_y/1000) if self.volume else None
        @property
        def samples_per_pixel(self): return 3
        @property
        def focal_planes(self): return list(positions)
        @property
        def photometric_interpretation(self): return "RGB"
        @property
        def bits(self): return 8
        @property
        def image_coordinate_system(self):
            return coordinate if self.volume or getattr(self.plane,"name",None) == "overview" else None
        @property
        def thread_safe(self): return False
        @property
        def lossy_compression(self):
            return [LossyCompression(LossyCompressionIsoStandard(h.method), h.ratio)
                    for h in facts[self.index].compression_history] or None
        @property
        def transcoder(self): return None

        def get_decoded_tile(self, tile_point, z, path, cache=True):
            check_cancel()
            if z not in positions or path != "0" or tile_point.x < 0 or tile_point.y < 0 or tile_point.x >= self.tiled_size.width or tile_point.y >= self.tiled_size.height:
                raise EPError("DICOM tile request is outside the fixed plane.")
            return references[positions.index(z)].read_region(self.index,tile_point.x*self.tile_size.width,tile_point.y*self.tile_size.height,
                self.tile_size.width,self.tile_size.height)

        def get_encoded_tile(self, tile, z, path):
            nonlocal encoded
            payload = encoder.encode(self.get_decoded_tile(tile,z,path))
            encoded += 1
            progress(min(0.49,encoded/(2*total)))
            return payload

    kinds = [ImageType.VOLUME]*len(meta.levels) + [
        {"overview":ImageType.THUMBNAIL,"macro":ImageType.OVERVIEW,"label":ImageType.LABEL}[p.name] for p in meta.associated]
    instances = []
    with use_settings(settings):
        for index, kind in enumerate(kinds):
            dataset = WsiMetadataDicomSchema().dump(metadata,kind,require_icc_profile=True)
            instance = WsiInstance.create_instance(Plane(index),dataset,kind,index if kind == ImageType.VOLUME else None)
            ds = instance.dataset
            # The library supplies generic defaults here. Replace them with
            # validated source facts; every exported raster is a derived image.
            ds.ImageType[0] = "DERIVED"
            frame_type = ds.SharedFunctionalGroupsSequence[0].WholeSlideMicroscopyImageFrameTypeSequence[0]
            frame_type.FrameType = list(ds.ImageType)
            ds.DerivationDescription = "EasyPath RGB sRGB conversion; " + REFERENCE_POLICY
            image_facts = facts[index]
            ds.FocusMethod = image_facts.focus_method
            ds.AcquisitionDateTime = datetime.fromisoformat(image_facts.acquisition_datetime).strftime("%Y%m%d%H%M%S.%f%z")
            ds.LossyImageCompression = "01" if image_facts.source_was_lossy else "00"
            if image_facts.source_was_lossy:
                ds.LossyImageCompressionMethod = [h.method for h in image_facts.compression_history]
                ds.LossyImageCompressionRatio = [str(h.ratio) for h in image_facts.compression_history]
            else:
                for keyword in ("LossyImageCompressionMethod", "LossyImageCompressionRatio"):
                    if keyword in ds:
                        del ds[keyword]
            if kind == ImageType.VOLUME:
                ds.ImagedVolumeDepth = info.imaged_depth_um
                ds.SharedFunctionalGroupsSequence[0].PixelMeasuresSequence[0].SliceThickness = thickness_um/1000
            ds.BurnedInAnnotation = "YES" if image_facts.contains_patient_text else "NO"
            ds.ConversionType = "WSD"
            instances.append(instance)

        class RasterSource(Source):
            @property
            def base_dataset(self): return instances[0].dataset
            @property
            def level_instances(self): return [i for i,k in zip(instances,kinds) if k == ImageType.VOLUME]
            @property
            def label_instances(self): return [i for i,k in zip(instances,kinds) if k == ImageType.LABEL]
            @property
            def overview_instances(self): return [i for i,k in zip(instances,kinds) if k == ImageType.OVERVIEW]
            @property
            def thumbnail_instances(self): return [i for i,k in zip(instances,kinds) if k == ImageType.THUMBNAIL]
            @property
            def annotation_instances(self): return []
            def close(self): self.clear_cache()

        with WsiDicom(RasterSource(),settings=settings) as slide:
            paths = [Path(p) for p in slide.save(directory,uid_generator=lambda: generate_uid(prefix=None),
                workers=1,chunk_size=1,offset_table="eot",add_missing_levels=False)]
    progress(0.5)
    if len(paths) != len(meta.planes) or set(directory.iterdir()) != set(paths):
        raise EPError("DICOM output membership disagrees with the planned instance set.")
    if sum(p.stat().st_size for p in paths) > max_file_bytes:
        raise EPError("DICOM output exceeds the reserved byte budget.")
    for path in paths:
        if path.parent != directory or path.is_symlink() or path.suffix != ".dcm":
            raise EPError("Unexpected DICOM output path.")
        with path.open("r+b") as stream:
            os.fsync(stream.fileno())
    before_validation = {p.name:artifact_snapshot(p,check_cancel) for p in paths}
    matched, files, decoded = set(), {}, 0
    with WsiDicom.open(paths,settings=settings) as verified:
        if verified.size.width != meta.levels[0].width or verified.size.height != meta.levels[0].height:
            raise EPError("DICOM WSI reader geometry disagrees.")
        for path in paths:
            check_cancel()
            if path.parent != directory or path.is_symlink() or path.suffix != ".dcm":
                raise EPError("Unexpected DICOM output path.")
            ds = dcmread(path,stop_before_pixels=True)
            kind = ImageType(ds.ImageType[2])
            candidates = [i for i,k in enumerate(kinds) if k == kind and
                (meta.planes[i].width,meta.planes[i].height) == (ds.TotalPixelMatrixColumns,ds.TotalPixelMatrixRows)]
            if len(candidates) != 1 or candidates[0] in matched or ds.SOPClassUID != VLWholeSlideMicroscopyImageStorage or ds.file_meta.TransferSyntaxUID != JPEG2000Lossless:
                raise EPError("DICOM identity/plane contract disagrees.")
            index = candidates[0]
            matched.add(index)
            plane = meta.planes[index]
            data = instances[index].image_data
            image_facts = facts[index]
            def values(keyword):
                value = getattr(ds, keyword, None)
                if value is None:
                    return []
                return list(value) if isinstance(value, (list, tuple, MultiValue)) else [value]
            if (ds.ImageType[0] != "DERIVED" or ds.LossyImageCompression != ("01" if image_facts.source_was_lossy else "00")
                    or ds.PatientID != info.patient_identifier or ds.BurnedInAnnotation != ("YES" if image_facts.contains_patient_text else "NO")
                    or ds.FocusMethod != image_facts.focus_method
                    or str(ds.AcquisitionDateTime) != datetime.fromisoformat(image_facts.acquisition_datetime).strftime("%Y%m%d%H%M%S.%f%z")
                    or values("LossyImageCompressionMethod") != [h.method for h in image_facts.compression_history]
                    or [float(v) for v in values("LossyImageCompressionRatio")] != [h.ratio for h in image_facts.compression_history]):
                raise EPError("DICOM source identity/history changed.")
            if kind == ImageType.VOLUME:
                spacing = ds.SharedFunctionalGroupsSequence[0].PixelMeasuresSequence[0].PixelSpacing
                if not math.isclose(float(spacing[0]),data.pixel_spacing.height,rel_tol=1e-9) or not math.isclose(float(spacing[1]),data.pixel_spacing.width,rel_tol=1e-9):
                    raise EPError("DICOM calibration changed.")
                if focal_stack is not None:
                    measures = ds.SharedFunctionalGroupsSequence[0].PixelMeasuresSequence[0]
                    if (int(ds.TotalPixelMatrixFocalPlanes) != len(positions)
                            or ds.DimensionOrganizationType != "TILED_FULL"
                            or float(ds.TotalPixelMatrixOriginSequence[0].ZOffsetInSlideCoordinateSystem) != positions[0]
                            or not math.isclose(float(measures.SpacingBetweenSlices)*1000,
                                                positions[1]-positions[0], rel_tol=1e-12)
                            or list(verified.levels[index].focal_planes) != list(positions)):
                        raise EPError("DICOM focal-plane count, positions or order changed.")
            # pydicom's frame iterator reads one frame at a time, without
            # assigning the complete gigapixel PixelData element to memory.
            from pydicom.pixels import iter_pixels
            count = 0
            for count, frame in enumerate(iter_pixels(path,raw=True,decoding_plugin="pillow"),1):
                check_cancel()
                z_index, tile_index = divmod(count-1, data.tiled_size.width*data.tiled_size.height)
                if z_index >= len(references):
                    raise EPError("Unexpected DICOM focal-plane frame.")
                x, y = tile_index % data.tiled_size.width, tile_index // data.tiled_size.width
                expected = references[z_index].read_region(index,x*data.tile_size.width,y*data.tile_size.height,
                    data.tile_size.width,data.tile_size.height)
                if frame.dtype != np.uint8 or not np.array_equal(frame,expected):
                    raise EPError("DICOM decoded frame differs from the reference.")
                decoded += 1
                progress(0.5+decoded/(2*total))
            if count != (plane.tiles*len(positions) if kind == ImageType.VOLUME else 1):
                raise EPError("DICOM frame coverage is incomplete.")
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                while block := stream.read(1024*1024):
                    check_cancel()
                    digest.update(block)
            files[path.name] = {"sha256":digest.hexdigest(),"size_bytes":path.stat().st_size,"sop_instance_uid":str(ds.SOPInstanceUID)}
    current = directory.lstat()
    if directory.is_symlink() or (owned.st_dev,owned.st_ino) != (current.st_dev,current.st_ino):
        raise EPError("DICOM output directory was replaced.")
    if set(directory.iterdir()) != set(paths) or {p.name:artifact_snapshot(p,check_cancel) for p in paths} != before_validation:
        raise EPError("DICOM artifact set changed during independent verification.")
    if any(files[name]["sha256"] != saved["sha256"] for name,saved in before_validation.items()):
        raise EPError("DICOM receipt does not match the verified artifact set.")
    check_cancel()
    return {"format":"dicom-wsi","files":files,"verified":True,"registered":False,
            "rgb_sse":0,"reference_policy":REFERENCE_POLICY,"frame_count":decoded,
            **({"focal_positions_um":list(positions)} if focal_stack is not None else {})}
