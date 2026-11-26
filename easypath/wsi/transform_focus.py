from __future__ import annotations

from contextlib import ExitStack

from functools import partial

from .ep_format import EPError

from .transform_dicom import write_dicom_artifact

from .transform_focus_tiff import write_focus_tiff

from .transform_pyramid import RGBPyramid

from .transform_pyramid import reference_workspace_bytes

def write_focus_work(source,work,fmt,*,max_file_bytes,bits,loss,distance,dicom,thickness_um,check_cancel,
                     admit_file_extent=None, progress=lambda current,total:None):
    source.stack.validate(fmt)
    if hasattr(source,"auxiliary") and fmt!="ep":
        raise EPError("NDPI focal-volume export currently supports a new EP only.")
    if fmt == "dcm":
        validate = getattr(source, "validate_dicom_history", None)
        if validate is not None:
            validate(dicom)
    reference = work/"reference"
    reference.mkdir(exist_ok=False)
    zs = source.stack.positions_um
    dest = work/("slide."+fmt+".partial")
    if fmt == "ep":
        from .ep_focus import write_focus_ep
        report = write_focus_ep(dest, source, bits=bits, loss=loss, distance=distance,
            scratch_dir=reference, max_file_bytes=max_file_bytes, check_cancel=check_cancel,
            admit_file_extent=admit_file_extent, progress=progress)
        source.check()
        check_cancel()
        return report
    with ExitStack() as owned:
        refs = tuple(owned.enter_context(RGBPyramid.build(reference/f"z{i:02d}",m,partial(source.read,i),
                          max_workspace_bytes=reference_workspace_bytes(m),check_cancel=check_cancel))
                     for i,m in enumerate(source.stack.rasters))
        options = dict(max_file_bytes=max_file_bytes,check_cancel=check_cancel)
        if fmt == "dcm":
            result = write_dicom_artifact(dest,refs[0],dicom,focal_stack=(zs,refs,thickness_um),**options)
        else:
            result = write_focus_tiff(dest,refs,zs,**options)
        source.check()
        check_cancel()
        return result
