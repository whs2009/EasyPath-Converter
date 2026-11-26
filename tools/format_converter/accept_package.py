"""Exercise the actual package, then read results with independent reader APIs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import numpy as np
import tifffile
from pydicom import dcmread
from pydicom.uid import JPEG2000Lossless, VLWholeSlideMicroscopyImageStorage
from wsidicom import WsiDicom
from easypath.wsi.ep_focus import open_ep_path


def accept(exe, parent=None):
    env = {k:v for k,v in os.environ.items() if not k.startswith(("PYTHON","EASYPATH_"))}
    env["APPIMAGE_EXTRACT_AND_RUN"]="1"
    def run(args,ok=True):
        p = subprocess.run([str(exe),*map(str,args)],cwd=out,env=env,capture_output=True,timeout=240)
        if (p.returncode == 0) != ok:
            raise RuntimeError((p.stderr or p.stdout).decode("utf-8","replace"))
        return (p.stderr+p.stdout).decode("utf-8","replace")
    with tempfile.TemporaryDirectory(dir=parent) as directory:
        out = Path(directory)
        y,x = np.indices((223,347))
        ref = np.stack([x%256,y%256,(x+y)%256],axis=-1).astype("u1")
        frames = np.stack([ref,255-ref,np.roll(ref,17,axis=0)])
        scan = out/"synthetic.svs"
        tifffile.imwrite(scan,ref,description="Aperio Image Library v12.4.3\n347x223 [0,0 347x223] (128x128) |AppMag = 40|MPP = 0.25",
            tile=(128,128),photometric="rgb",metadata=None,compression="deflate")
        stack = out/"synthetic.ome.tif"
        zs = [-1.,0.,1.]
        tifffile.imwrite(stack,frames,ome=True,metadata={"axes":"ZYXS","PhysicalSizeX":.25,"PhysicalSizeY":.25,
            "PhysicalSizeXUnit":"\u00b5m","PhysicalSizeYUnit":"\u00b5m","Plane":{"PositionZ":zs,"PositionZUnit":["\u00b5m"]*3}},
            tile=(128,128),photometric="rgb",compression="deflate")
        facts = {"dicom":dict(acquisition_datetime="2026-11-01T10:00:00+00:00",focus_method="AUTO",
            source_was_lossy=False,contains_patient_text=False,imaged_depth_um=3.,origin_x_mm=0.,origin_y_mm=0.,
            rotation_degrees=0.,slide_identifier="SYNTHETIC",specimen_identifier="FIXTURE")}
        for original in (scan,stack):
            before=hashlib.sha256(original.read_bytes()).hexdigest()
            is_stack=original==stack
            rasters=frames if is_stack else [ref]
            side=out/"facts.json"
            side.write_text(json.dumps(facts | ({"focal_origin_um":-1., "focal_thickness_um":1.} if is_stack else {})))
            for fmt in ("ep","tiff","dcm"):
                name=("stack" if is_stack else "single")+" renamed"
                args=["--one",original,"--destination",out,"--name",name,"--format",fmt,"--assume-srgb"]
                if fmt=="dcm": args+=["--facts",side]
                run(args)
                dest=out/(name+"."+fmt)
                if fmt=="ep":
                    with open_ep_path(dest) as ep:
                        for i,r in enumerate(rasters):
                            got=ep.read_region(*([i] if is_stack else []),0,0,0,347,223)
                            assert np.sqrt(np.mean((r.astype(float)-got)**2))/255 <= .05
                elif fmt=="tiff":
                    assert np.array_equal(tifffile.imread(dest),frames if is_stack else ref)
                else:
                    paths=list(dest.glob("*.dcm"))
                    assert paths
                    for path in paths:
                        ds=dcmread(path,stop_before_pixels=True)
                        assert ds.SOPClassUID==VLWholeSlideMicroscopyImageStorage and ds.file_meta.TransferSyntaxUID==JPEG2000Lossless
                    with WsiDicom.open(paths) as slide:
                        for i,r in enumerate(rasters):
                            got=slide.read_region((0,0),0,(347,223),z=zs[i] if is_stack else 0.)
                            assert np.array_equal(np.asarray(got),r)
                assert "already exist" in run(args,False)
                print(original.suffix,fmt,"verified",flush=True)
            assert hashlib.sha256(original.read_bytes()).hexdigest()==before
        assert not list(out.glob(".easypath-convert-*"))
    print("6 offline packaged exports, 6 no-overwrite refusals, sources unchanged.")

if __name__ == "__main__":
    p=argparse.ArgumentParser()
    p.add_argument("executable",type=Path)
    p.add_argument("--temp-parent",type=Path)
    args=p.parse_args()
    accept(args.executable.resolve(strict=True),args.temp_parent)
