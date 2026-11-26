# EasyPath Format Converter
The desktop app converts slides on this computer. It does not log in, upload slides or change the EasyPath catalog. Select **Add files**, choose an input row to edit its output name (without the extension), choose an existing destination folder, select the format and press **Convert files**. Each input has its own result. Existing outputs are never replaced. Cancel discards the unfinished output; previously completed outputs stay in the chosen folder.

## Formats
- **EP:** 5, 6, 7 or 8 bits per channel; 0–15% error; JPEG XL distance 1–15. Defaults: 8 bits, 5%, distance 4. Only 8 bits and 0% preserves the original decoded RGB exactly. A positive error limit is checked against source RGB and includes precision reduction; 0% with reduced precision preserves the quantized pixels. Distance is not an error percentage.
- **TIFF:** lossless tiled, pyramidal TIFF. **DICOM WSI:** a directory named `your-name.dcm` containing the verified DICOM instances. Lossless describes the output transfer encoding, not any prior scanner compression. No patient or acquisition facts are invented.
- Embedded color profiles are used where supported. Confirm sRGB only when appropriate for the source. Unsupported inputs, unverified focal geometry or incomplete metadata stop that file. The NDPI focal-volume path writes a new EP with all focal planes; scanner macro/tissue-detection auxiliaries stay in the original.
- OpenSlide handles supported scanner families. Philips iSyntax is not available in the General edition.

## DICOM facts
Choose a source row, then **DICOM facts...**. Supply a UTF-8 JSON document with a top-level `dicom` object. Required fields in that object are `acquisition_datetime` (ISO 8601, including time zone), `focus_method` (AUTO or MANUAL), `source_was_lossy`, `contains_patient_text`, `imaged_depth_um`, `origin_x_mm`, `origin_y_mm`, `rotation_degrees`, `slide_identifier`, and `specimen_identifier`. Optional: `patient_identifier`, `patient_name`. Supply `compression_history` with actual method/ratio entries when the source was lossy. Supply an `associated` list with the role and verified acquisition, focus, text and compression facts for each associated image. For an eligible focal stack, supply measured `focal_origin_um` and `focal_thickness_um` alongside the top-level `dicom` object. Use TIFF or EP if the required facts are unknown. Keep these files private.

Preserve all input files, including MRXS companions. Allow room for the temporary pyramid and final output.
