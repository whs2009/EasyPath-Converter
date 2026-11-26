"""Entry point for the offline converter, including fixed isolated SDK children."""
from __future__ import annotations
import argparse
import ctypes as C
import os
from pathlib import Path
import sys


def main(argv):
    parser = argparse.ArgumentParser(prog="EasyPath Format Converter")
    parser.add_argument("--one", type=Path)
    parser.add_argument("--inspect", type=Path, help="Open a single-plane source and verify three native RGB reads.")
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--name")
    parser.add_argument("--format", choices=("ep", "tiff", "dcm"), default="ep")
    parser.add_argument("--bits", type=int, choices=range(5, 9), default=8)
    parser.add_argument("--loss", type=int, choices=range(16), default=5)
    parser.add_argument("--distance", type=int, choices=range(1, 16), default=4)
    parser.add_argument("--assume-srgb", action="store_true")
    parser.add_argument("--facts", type=Path)
    parser.add_argument("--gui-smoke", action="store_true")
    parser.add_argument("--version", action="version", version="EasyPath Format Converter 0.1.0")
    cfg = parser.parse_args(argv)
    from easypath.wsi.desktop_converter import convert
    if cfg.inspect:
        if cfg.one:
            parser.error("Choose either --one or --inspect")
        import hashlib, json
        from easypath.wsi.transform_source import NativeTransformSource
        with NativeTransformSource.open(cfg.inspect, assume_srgb=cfg.assume_srgb) as source:
            if getattr(source, "stack", None):
                parser.error("--inspect is only for single-plane reader checks")
            base = source.metadata.levels[0]
            width, height = base.width, base.height
            w,h=min(128,width),min(128,height)
            samples=[source.read_plane(0,x,y,w,h) for x,y in
                ((0,0),((width-w)//2,(height-h)//2),(width-w,height-h))]
            print(json.dumps(dict(width=width,height=height,reader=type(source.backend).__name__,
                samples=[hashlib.sha256(rgb.tobytes()).hexdigest() for rgb in samples])))
        return 0
    if cfg.one:
        if not cfg.destination:
            parser.error("Choose a destination with --destination")
        try:
            print(convert(cfg.one, cfg.destination, cfg.name or cfg.one.stem, cfg.format,
                bits=cfg.bits, loss=cfg.loss, distance=cfg.distance, assume_srgb=cfg.assume_srgb, facts=cfg.facts))
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0
    stop_t = C.CFUNCTYPE(C.c_int)
    prog_t = C.CFUNCTYPE(None, C.c_double)
    callback_t = C.CFUNCTYPE(C.c_int, *([C.c_char_p]*4), *([C.c_int]*4), C.c_char_p,
                             stop_t, prog_t, C.c_void_p, C.c_int)
    @callback_t
    def run(source, folder, name, fmt, bits, loss, distance, srgb, facts, stop, progress, errorbuf, size):
        def check():
            if stop():
                raise InterruptedError("Conversion canceled.")
        try:
            convert(source.decode(), folder.decode(), name.decode(), fmt.decode(),
                    bits=bits, loss=loss, distance=distance, assume_srgb=bool(srgb),
                    facts=facts.decode() or None, stop=check, progress=progress)
            return 0
        except Exception as exc:
            msg = str(exc).encode("utf8", "replace")[:size-1] + b"\0"
            C.memmove(errorbuf, msg, len(msg))
            return 1
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    libname = {"win32": "converter_ui.dll", "linux": "libconverter_ui.so",
               "darwin": "libconverter_ui.dylib"}[sys.platform]
    ui = C.CDLL(str(root / libname))
    ui.converter_show.argtypes = [callback_t, C.c_int]
    ui.converter_show.restype = C.c_int
    research = False
    return ui.converter_show(run, int(cfg.gui_smoke) | (2 if research else 0))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
