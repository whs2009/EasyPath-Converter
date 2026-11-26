from __future__ import annotations

from .ep_format import EPError

from .ep_format import EPMetadata

from .ep_format import TILE_SIZE

from .ep_format import integer

from .ep_format import pyramid_levels

from .focal_stack import FocusStack

class EPFocusInput:
    def __init__(self, container, *, source_sha256, check_cancel):
        self.container = container
        self.check_cancel = check_cancel
        try:
            self.check()
            src = container.stack.rasters[0]
            self._encoded = src
            base = src.levels[0]
            # Codec settings describe the input bytes, not the requested new encoding.
            # Keep the historical ICC as provenance; never transform canonical RGB twice.
            self.metadata = EPMetadata(
                levels=pyramid_levels(base.width, base.height),
                objective_power=src.objective_power, mpp_x=src.mpp_x, mpp_y=src.mpp_y,
                source_color_policy=src.source_color_policy, source_icc_base64=src.source_icc_base64,
                source_sha256=source_sha256)
            positions = container.stack.positions_um
            self.stack = FocusStack((self.metadata,) * len(positions), positions)
            self.stack.validate("ep")
        except BaseException:
            self.close()
            raise

    def check(self):
        self.check_cancel()
        self.container._check()

    def read(self, z, plane, x, y, w, h):
        integer(z, "focus index", 0, len(self.stack.positions_um)-1)
        integer(plane, "source base level", 0, 0)
        base = self.metadata.levels[0]
        integer(x, "source x", 0, base.width-1)
        integer(y, "source y", 0, base.height-1)
        integer(w, "source width", 1, min(TILE_SIZE, base.width-x))
        integer(h, "source height", 1, min(TILE_SIZE, base.height-y))
        self.check()
        rgb = self.container.read_region(z, 0, x, y, w, h)
        self.check()
        return rgb

    def provenance(self):
        src = self._encoded
        return dict(pixel_selection="all-native-focal-planes", axis="scanner-focus",
                    focal_positions_um=list(self.stack.positions_um),
                    color_intent="already-canonical-srgb", source_profiles={"volume": src.source_icc_base64},
                    reference_pixels="decoded-ep-base-rgb", input_ep_profile=src.profile,
                    input_ep_channel_bits=src.channel_bits or 8, input_ep_error_percent=src.maximum_nrmse_percent or 0,
                    upstream_source_sha256=src.source_sha256, upstream_loss_history="not-established")

    def validate_dicom_history(self, facts):
        if self._encoded.profile != "rgb8-jxl-lossless-v1" and not facts.source_was_lossy:
            raise EPError("The EP input declares loss or reduced precision; verify its compression history.")
        if (self._encoded.maximum_nrmse_percent and
                not any(h.method == "ISO_18181_1" for h in facts.compression_history)):
            raise EPError("Confirm the JPEG XL stage in the supplied source compression history.")
        # An output error ceiling is not a measured DICOM compression ratio.
        # Use the explicitly supplied facts; never invent a ratio or upstream history.

    def close(self):
        self.container.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
