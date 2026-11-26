from __future__ import annotations

import base64

from io import BytesIO

import math

from pathlib import Path

import numpy as np

from PIL import Image

from PIL import ImageCms

from .ep_format import EPAssociated

from .ep_format import EPError

from .ep_format import EPMetadata

from .ep_format import MAX_REGION_PIXELS

from .ep_format import integer

from .ep_format import pyramid_levels

def _positive(properties, name, maximum):
    value = properties.get(name)
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise EPError(f"Invalid source calibration: {name}.") from error
    if isinstance(value, bool) or not math.isfinite(result) or not 0 < result <= maximum:
        raise EPError(f"Invalid source calibration: {name}.")
    return result

class _Color:
    def __init__(self, profile, *, assume_srgb, image_role=None):
        if type(assume_srgb) is not bool:
            raise EPError("The source sRGB assumption must be explicit.")
        self.profile = profile
        self.transform = None
        if profile is None:
            if not assume_srgb:
                detail = f" ({image_role})" if image_role else ""
                raise EPError("Source color profile is missing" + detail + "; explicitly confirm the sRGB assumption.")
        else:
            if type(profile) is not bytes or not 128 <= len(profile) <= 1024 * 1024:
                raise EPError("Source color profile exceeds its bounds.")
            try:
                source = ImageCms.ImageCmsProfile(BytesIO(profile))
                self.transform = ImageCms.buildTransformFromOpenProfiles(
                    source, ImageCms.createProfile("sRGB"), "RGB", "RGB",
                    renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC)
            except (ValueError, OSError, ImageCms.PyCMSError) as error:
                raise EPError("Source ICC profile cannot be converted to RGB sRGB.") from error

    def apply(self, image):
        if image.mode not in {"RGB", "RGBA"} or image.width * image.height > MAX_REGION_PIXELS:
            raise EPError("Transform input must be bounded RGB8 or RGBA8.")
        alpha = image.getchannel("A") if image.mode == "RGBA" else None
        rgb = image.convert("RGB")
        if self.transform is not None:
            rgb = ImageCms.applyTransform(rgb, self.transform)
        if alpha is not None:
            rgb = Image.composite(rgb, Image.new("RGB", rgb.size, "white"), alpha)
        return np.array(rgb, dtype=np.uint8, copy=True)

class _OpenSlideInput:
    def __init__(self, path, *, assume_srgb):
        import openslide

        self.slide = openslide.OpenSlide(str(path))
        self._ep_volume_apply = None
        self._cached = None
        try:
            width, height = self.slide.dimensions
            self.levels = pyramid_levels(width, height)
            properties = self.slide.properties
            self.objective = _positive(properties, "openslide.objective-power", 200)
            self.mpp_x = _positive(properties, "openslide.mpp-x", 1000)
            self.mpp_y = _positive(properties, "openslide.mpp-y", 1000)
            profile = self.slide.color_profile
            self.color = _Color(profile.tobytes() if profile is not None else None,
                                assume_srgb=assume_srgb, image_role="main slide image")
            self.associated = []
            self._associated = {}
            roles = {"thumbnail": "overview", "macro": "macro", "label": "label"}
            keys = list(self.slide.associated_images)
            if any(key not in roles for key in keys):
                raise EPError("This source has unsupported associated-image roles; conversion cannot omit them.")
            pixels = 0
            for key in sorted(keys):
                # OpenSlide's mapping otherwise decodes a whole associated image
                # before its dimensions can be inspected. Bound it first.
                w, h = openslide.lowlevel.get_associated_image_dimensions(self.slide._osr, key)
                plane = EPAssociated(name=roles[key], width=w, height=h)
                pixels += w * h
                if pixels > MAX_REGION_PIXELS:
                    raise EPError("Source associated images exceed the conversion pixel budget.")
                function = openslide.lowlevel.read_associated_image_icc_profile
                raw = function(self.slide._osr, key) if function.available else None
                color = _Color(raw, assume_srgb=assume_srgb, image_role=key + " image")
                self.associated.append(plane)
                self._associated[plane.name] = (key, color)
        except BaseException:
            self.close()
            raise

    def read(self, role, x, y, width, height):
        if role is None:
            image = self.slide.read_region((x, y), 0, (width, height))
            if image.size != (width, height):
                raise EPError("OpenSlide returned unexpected native geometry.")
            return (self._ep_volume_apply or self.color.apply)(image)
        key, color = self._associated[role]
        if self._cached is None or self._cached[0] != role:
            image = self.slide.associated_images[key]
            plane = next(p for p in self.associated if p.name == role)
            if image.size != (plane.width, plane.height):
                raise EPError("Associated-image geometry changed.")
            self._cached = role, color.apply(image)
        return self._cached[1][y:y+height, x:x+width].copy()

    def color_profiles(self):
        return {"volume": self.color.profile,
                **{role: color.profile for role, (_, color) in self._associated.items()}}

    def close(self):
        self._cached = None
        self.slide.close()

class _ContainerInput:
    def __init__(self, container):
        self.container = container
        self._cached = None
        try:
            metadata = container.metadata()
            self.levels = pyramid_levels(metadata.levels[0].width, metadata.levels[0].height)
            self.objective, self.mpp_x, self.mpp_y = metadata.objective_power, metadata.mpp_x, metadata.mpp_y
            self.associated = metadata.associated
            self.color = _Color(None, assume_srgb=True)
            self._associated = {plane.name: len(metadata.levels) + index
                                for index, plane in enumerate(metadata.associated)}
        except BaseException:
            self.close()
            raise

    def read(self, role, x, y, width, height):
        if role is None:
            return self.container.read_region(0, x, y, width, height)
        if self._cached is None or self._cached[0] != role:
            self._cached = role, self.container.read_associated_image(role)
        return self._cached[1][y:y+height, x:x+width].copy()

    def color_profiles(self):
        # EP/transform TIFF pixels are already canonical sRGB. A retained source
        # profile describes provenance, not a second transform to apply here.
        return {"volume": None, **{plane.name: None for plane in self.associated}}

    def close(self):
        self._cached = None
        self.container.close()

class NativeTransformSource:
    def __init__(self, backend, *, source_sha256=None, check_cancel=lambda: None):
        self.backend = backend
        self.check_cancel = check_cancel
        self.closed = False
        try:
            profile = backend.color.profile
            self.metadata = EPMetadata(
                source_color_policy="icc-to-srgb" if profile else "declared-srgb",
                source_icc_base64=base64.b64encode(profile).decode("ascii") if profile else None,
                source_sha256=source_sha256, objective_power=backend.objective,
                mpp_x=backend.mpp_x, mpp_y=backend.mpp_y,
                levels=backend.levels, associated=tuple(backend.associated))
        except BaseException:
            self.close()
            raise

    @classmethod
    def open(cls, path, *, assume_srgb=False, source_sha256=None, check_cancel=lambda: None, ep_read_ahead=False):
        check_cancel()
        path = Path(path)
        if type(assume_srgb) is not bool:
            raise EPError("The source sRGB assumption must be explicit.")
        if type(ep_read_ahead) is not bool:
            raise EPError("Invalid EP read-ahead selection.")
        if path.suffix.lower() == ".ndpi" and path.is_file():
            from .ndpi_focus import NDPIFocus, has_ndpi_focus
            if has_ndpi_focus(path):
                return NDPIFocus(path, assume_srgb=assume_srgb, source_sha256=source_sha256, check_cancel=check_cancel)
        if path.suffix.lower() in {".tif", ".tiff"} and path.is_file():
            from .transform_focus_tiff import OMEFocusInput, has_ome_focus
            if has_ome_focus(path):
                return OMEFocusInput(path, assume_srgb=assume_srgb, source_sha256=source_sha256, check_cancel=check_cancel)
        if path.suffix.lower() == ".ep":
            from .ep_focus import EPFocusFile, open_ep_path
            container = open_ep_path(path, check_cancel=check_cancel)
            if isinstance(container, EPFocusFile):
                from .transform_ep_focus import EPFocusInput
                return EPFocusInput(container, source_sha256=source_sha256, check_cancel=check_cancel)
            backend = _ContainerInput(container)
        elif path.suffix.lower() == ".isyntax":
            raise EPError('iSyntax requires a separately authorized edition.')
        else:
            from .transform_tiff_reader import is_transform_tiff, TransformTIFFFile
            if path.suffix.lower() in {".tif", ".tiff"} and is_transform_tiff(path):
                backend = _ContainerInput(TransformTIFFFile(path))
            else:
                if path.suffix.lower() not in {".svs", ".ndpi", ".tif", ".tiff", ".mrxs", ".scn", ".bif", ".svslide"} or not path.is_file():
                    raise EPError("Exact native RGB conversion is not available for this source format.")
                if ep_read_ahead and path.suffix.lower() in {".ndpi", ".tif", ".tiff", ".mrxs"}:
                    import openslide
                    from .ep_openslide_ahead import OwnedOpenSlideSource

                    vendor = openslide.OpenSlide.detect_format(str(path))
                    expected = {"mirax"} if path.suffix.lower() == ".mrxs" else {"hamamatsu", "generic-tiff"}
                    if vendor in expected:
                        return OwnedOpenSlideSource.from_path(path, vendor=vendor, assume_srgb=assume_srgb,
                            source_sha256=source_sha256, check_cancel=check_cancel)
                backend = _OpenSlideInput(path, assume_srgb=assume_srgb)
        return cls(backend, source_sha256=source_sha256, check_cancel=check_cancel)

    def _prepare_read(self, plane_id, x, y, width, height):
        if self.closed:
            raise EPError("Transform source is closed.")
        integer(plane_id, "source plane", 0, len(self.metadata.planes)-1)
        if 0 < plane_id < len(self.metadata.levels):
            raise EPError("Lower levels must be derived from the uncompressed reference.")
        plane = self.metadata.planes[plane_id]
        integer(x, "source x", 0, plane.width-1)
        integer(y, "source y", 0, plane.height-1)
        integer(width, "source width", 1, min(512, plane.width-x))
        integer(height, "source height", 1, min(512, plane.height-y))
        self.check_cancel()
        role = None if plane_id == 0 else self.metadata.associated[plane_id-len(self.metadata.levels)].name
        return role, x, y, width, height

    def _read_pixels(self, role, x, y, width, height):
        pixels = self.backend.read(role, x, y, width, height)
        if not isinstance(pixels, np.ndarray) or pixels.dtype != np.uint8 or pixels.shape != (height, width, 3):
            raise EPError("Source returned unexpected native RGB8 pixels.")
        return pixels

    def read_plane(self, plane_id, x, y, width, height):
        pixels = self._read_pixels(*self._prepare_read(plane_id, x, y, width, height))
        self.check_cancel()
        return pixels

    def provenance(self):
        result = dict(pixel_selection="single-default-native-rgb-plane",
            source_profiles={role: base64.b64encode(profile).decode("ascii") if profile else None
                             for role, profile in self.backend.color_profiles().items()},
            color_intent="relative-colorimetric", alpha_background="white-srgb")
        extra = getattr(self.backend, "provenance", None)
        if extra is not None:
            result.update(extra())
        return result

    def close(self):
        if not self.closed:
            self.backend.close()
            self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
