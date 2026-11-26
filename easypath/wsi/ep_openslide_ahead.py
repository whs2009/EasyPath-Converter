from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from threading import get_ident

from .ep_format import EPError

from .transform_source import NativeTransformSource

from .transform_source import _OpenSlideInput

class OwnedOpenSlideSource(NativeTransformSource):
    @classmethod
    def from_path(cls, path, *, vendor, assume_srgb, source_sha256, check_cancel):
        pool = ThreadPoolExecutor(1, thread_name_prefix="ep-volume-read")

        def opened():
            backend = _OpenSlideInput(path, assume_srgb=assume_srgb)
            try:
                if backend.slide.properties.get("openslide.vendor") != vendor:
                    raise EPError("Source vendor changed while opening the EP input.")
                return backend, get_ident()
            except BaseException:
                backend.close()
                raise

        try:
            backend, worker = pool.submit(opened).result()
            # Metadata and profile bytes have already been captured by the opener.
            obj = cls.__new__(cls)
            obj._pool, obj._worker = pool, worker
            obj.__init__(backend, source_sha256=source_sha256, check_cancel=check_cancel)
            return obj
        except BaseException:
            pool.shutdown(wait=True)
            raise

    def _read_pixels(self, *request):
        if get_ident() != self._worker:
            raise EPError("OpenSlide pixel reads must use their source thread.")
        return super()._read_pixels(*request)

    def submit_pixels(self, *request):
        if self.closed:
            raise EPError("Transform source is closed.")
        return self._pool.submit(self._read_pixels, *request)

    def read_plane(self, plane_id, x, y, width, height):
        request = self._prepare_read(plane_id, x, y, width, height)
        pixels = self.submit_pixels(*request).result()
        self.check_cancel()
        return pixels

    def close(self):
        if not self.closed:
            try:
                self._pool.submit(self.backend.close).result()
            finally:
                self.closed = True
                self._pool.shutdown(wait=True)
