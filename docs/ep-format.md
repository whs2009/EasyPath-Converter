# EasyPath EP: bytes to pixels and back

EP is a random-access whole-slide RGB pyramid, not a JPEG XL image with a new extension. Its index locates independently decodable tiles. An optional outer directory selects a focal plane. The index order is spatial, not the order in which tiles finished encoding. This document describes the formats accepted by this source tree. The reader's limits are part of the interoperability contract.

## 1. Coordinates, planes and color

In a member, the native level-0 origin is top left. X increases right; Y increases down. Each subsequent pyramid level has dimensions `((w+1)//2, (h+1)//2)`. Stop at the last declared level; do not infer extra levels from a requested zoom. A focal plane is a different image of the specimen, not a zoom level. Physical spacing is in micrometers per native pixel on each axis. Missing spacing or objective power is unknown, not zero or a default scanner calibration.

The canonical pixels are 3 interleaved RGB bytes in sRGB order. Apply a supported source ICC conversion first, or use an explicitly declared sRGB source. An embedded source ICC profile records provenance; do not apply it again to decoded EP pixels. Scanner macro, label and overview, when present, are additional named planes with their own dimensions, not pyramid levels. They can contain identifying information.

## 2. Single-plane header

All integers below are unsigned little endian. Byte positions start at zero relative to the **member**, even when nested. A reader must check every offset and product against the actual member length before seeking or allocating.

| Offset | Bytes | Value |
|---:|---:|---|
| 0 | 8 | Hex `45 50 57 53 49 00 0D 0A` |
| 8 | 2+2 | Major 1 (raw JXL) or 2 (typed records); minor 0 |
| 12 | 4 | Flags: only 1 is a finalized, publicly readable member; 0 is an unfinished writer artifact |
| 16 | 8 | Metadata length M |
| 24 | 8 | Record count N |
| 32 | 8 | Index offset: exactly 128 + M |
| 40 | 8 | Exact member length |
| 48 | 32 | Binary SHA-256 of the M metadata bytes |
| 80 | 32 | Binary SHA-256 of all 48*N index bytes |
| 112 | 16 | Reserved, zero |

## 3. Metadata and index

Metadata is a UTF-8 JSON object. Writers use deterministic sorted compact JSON with ASCII escaping; hash the **actual bytes** before parsing. Reject duplicate keys, nonfinite numbers, unknown schema values and incorrectly typed integer constants. Schema version 1, tile size 512, one frame per member, coordinate contract `native-top-left-xy-v1`, color space `sRGB`. Required source color policy is `declared-srgb` or `icc-to-srgb` (preserve validated base64 source ICC for the latter). Optional fields include `source_sha256`, `mpp_x`, `mpp_y`, `objective_power`. `levels` lists objects with integer width and height. `associated` is ordered and holds up to three distinct named planes with width and height. For v2, `storage_encoding` is `jxl-solid-rgb-v2`; v1 does not use it. See [metadata model](../easypath/wsi/ep_format.py) for field defaults and constraints.

| Profile | Volume sample precision and error metadata |
|---|---|
| `rgb8-jxl-lossless-v1` | Exact normalized RGB8; no reduced-bit or loss settings |
| `rgb8-jxl-bounded-lossy-v1` | RGB8 with integer `maximum_nrmse_percent` 1 through 15 |
| `rgb-jxl-precision-v1` | `channel_bits` 5, 6 or 7; `maximum_nrmse_percent` 0 through 15 |

Count tiles in each volume level, then each associated image in listed order. For a plane of W by H, C=(W+511)//512, R=(H+511)//512. Its tile (tx,ty) has index `sum(previous C*R) + ty*C + tx`. Read the 48-byte entry at `index_offset + 48*index`: uint64 record offset, uint64 record length and a 32-byte SHA-256 of the **entire stored record**. The first offset is immediately after the index. Records are nonempty, in order and contiguous; reject gaps, overlap, missing records and trailing bytes. A digest detects corruption, not authorship.

## 4. Record decoding and random reads

1. Validate the header, metadata and index, including count against the plane shapes. For each requested rectangle intersect the plane bounds and enumerate just the intersecting tiles. No scan through preceding compressed data is necessary.
2. Read each record within its indexed length and validate its digest. V1 stores a raw JPEG XL codestream starting FF 0A. V2 type byte 00 precedes that same codestream. V2 type 01 is *exactly four bytes*: 01, R, G, B. It fills the **valid** rectangle with this display color. Do not pass type bytes to libjxl, or interpret a solid color as a palette index.
3. Decode into a checked 512 x 512 x 3 buffer. Respect the declared precision. For b-bit volume codes set M=2^b-1 and expand `display=(code*255+M//2)//M`; do not multiply by 8 for a 5-bit code. Associated planes use lossless RGB8. Solid RGB values are already expanded.
4. Clip edge tiles back to the declared dimensions before compositing. Edge padding sent to a codec is white but lossy padding is not an invariant. Stitch intersections into the destination and resample only when adapting a native level to a display zoom. The reference reader fills out-of-bounds regions white.

One decoded full RGB tile costs 786,432 bytes. A 1024-square read typically touches four aligned tiles or up to nine unaligned tiles at a native level. The implementation keeps the index in memory, seeks to payloads and uses a bounded decoded-tile cache (32 MiB default); it does not expand a full slide to RAM. The cache is not an integrity override. Batching and at most two decoder lanes are implementation choices, not requirements on external readers. Smaller files do not guarantee faster tissue reads: JXL CPU time, alignment, storage cache and zoom all matter.

## 5. Encoder and memory behavior

Read the source level in bounded chunks. Derive reference pyramid pixels from *uncompressed* canonical RGB: recursively average valid 2 x 2 samples for each channel using `(sum + n//2)//n`. At odd edges, n can be 1 or 2. Do not decimate a single sample, average padding or generate children from already-lossy JXL.

The streaming writer walks base tiles in blocks, accumulating quadrants of unfinished parent tiles in bounded RAM; a completed parent contributes to the next level. It spools compressed records on disk while retaining indexing and verification data. The pending-parent budget (normally 256 MiB) is **not** a limit on total process RAM or temporary disk. It writes the final directory in canonical order. Compress constant valid regions as solid records where eligible; avoid assigning a blank color by threshold to faint tissue.

Default: 8 bits/channel, maximum 5% error, JXL distance 4. Distance is the starting codec setting, not a percentage or guaranteed achieved error. For reduced precision the reference quantizer uses `q=(v*M+127)//255`. At 0% and fewer than 8 bits, preserve the quantized codes exactly, not the original RGB8. Positive limits include quantization and codec error against original canonical pixels. Over valid RGB samples, NRMSE percent = `100/255 * sqrt(sum((decoded-reference)^2)/number_of_channel_samples)`. Check both the whole valid rectangle and its nonempty foreground subset: min(channel)<220 and (max-min>8 or mean<180). This heuristic is not clinical segmentation or a per-pixel bound. The bounded encoder checks decoded candidates, retries smaller distances and can fall back to lossless coding; reject unachievable settings rather than silently exceeding the limit.

The writer binds decoded pixels during quality checks, assembles a pending artifact, reopens it for uncached verification, checks the final decoded tiles and changes the final flag only after verification. The converter publishes a new output without replacing the input. Integrity, correct coordinates and reproducible pixels are different from diagnostic equivalence.

## 6. Multi-plane envelope (v3)

Magic `45 50 5A 57 53 49 0D 0A`. The 56-byte header is `<8sHHIQ32s`: magic, version 3 (uint16), position-present flag 0/1 (uint16), plane count 2..20 (uint32), total file length (uint64), SHA-256 of the following directory. Each 56-byte row `<dQQ32s` holds an IEEE-754 little-endian float64 focal position in micrometers, uint64 absolute member start and length, and SHA-256 of that member's **128-byte header**, not its entire body. Unknown positions use flag 0 and all-zero positions; known ones must be finite and distinct. Plane order is directory order. Do not sort on z or invent spacing.

Members are contiguous from 56+56*plane_count to end-of-file. Check each member's header, metadata, index and records using its own origin. Members in a focal stack require matching raster metadata and tile counts, and do not carry associated planes. Matching coordinates are not proof of registration between unrelated tissues.

## 7. Reader limits and starting points

Reject unsupported versions, unfinished files, invalid record tags, overflowing products, out-of-range offsets and changed sources. The implementation caps metadata at 2 MiB, one stored record at 4 MiB, file size at 1 TiB, an ROI at 16,777,216 pixels and the level directory at 32 levels. New writes support at most 500,000 x 500,000 per plane and a bounded tile count (1,281,000); legacy reader geometry has separate limits. These are ceilings, not a promise that every maximum-sized stack fits the byte limit.

Start with [EPFile](../easypath/wsi/ep_container.py), [typed records](../easypath/wsi/ep_records.py), [focal directory](../easypath/wsi/ep_focus.py), [streaming writer](../easypath/wsi/ep_stream_writer.py) and the [standalone format example](minimal_ep.py). Check the example's digest and edge behavior before implementing multi-level, lossy or focal reads.
