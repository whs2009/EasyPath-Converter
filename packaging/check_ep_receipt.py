"""Check that the General writer's receipt represents a completed pixel replay."""
import hashlib
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from easypath.wsi.ep_container import EPFile
from easypath.wsi.ep_format import EPError, EPLevel, EPMetadata
from easypath.wsi.ep_stream_writer import write_ep_streaming_artifact


def check():
    rgb = np.arange(189, dtype=np.uint8).reshape(9, 7, 3)
    meta = EPMetadata(source_color_policy="declared-srgb", levels=(EPLevel(width=7, height=9),))
    def write(path):
        return write_ep_streaming_artifact(path, meta, lambda p, x, y, w, h: rgb[y:y+h, x:x+w],
                                           max_file_bytes=10_000_000, max_workspace_bytes=10_000_000)
    with TemporaryDirectory() as tmp:
        path = Path(tmp) / "single.partial"
        report = write(path)
        assert report["verified"] is True and report["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        with EPFile(path) as f:
            np.testing.assert_array_equal(f.read_region(0, 0, 0, 7, 9), rgb)
        with patch.object(EPFile, "validate_all_tiles", lambda *a, **kw: None):
            try:
                write(Path(tmp) / "incomplete.partial")
            except EPError as exc:
                if "missed tiles" not in str(exc): raise
            else:
                raise AssertionError("Missing final pixel replay was accepted")


if __name__ == "__main__":
    check()
