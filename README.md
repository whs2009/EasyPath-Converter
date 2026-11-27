# EasyPath Converter · General edition

Batch-convert local whole-slide images into an EasyPath EP, lossless tiled pyramidal TIFF, or lossless JPEG 2000 DICOM microscopy series. Source slides are never overwritten.

## Downloads

Download the latest tested General build from [Releases](https://github.com/whs2009/EasyPath-Converter/releases/latest). Verify the package with [SHA256SUMS](https://github.com/whs2009/EasyPath-Converter/releases/latest/download/SHA256SUMS). The source archive is not a standalone executable.

| Platform | Release asset |
|---|---|
| Windows x86-64 | [EasyPath-Format-Converter-windows-x64.exe](https://github.com/whs2009/EasyPath-Converter/releases/latest/download/EasyPath-Format-Converter-windows-x64.exe) |
| macOS Apple silicon | [EasyPath-Format-Converter-macos-arm64.dmg](https://github.com/whs2009/EasyPath-Converter/releases/latest/download/EasyPath-Format-Converter-macos-arm64.dmg) |
| macOS Intel | [EasyPath-Format-Converter-macos-x64.dmg](https://github.com/whs2009/EasyPath-Converter/releases/latest/download/EasyPath-Format-Converter-macos-x64.dmg) |
| Ubuntu x86-64 | [EasyPath-Format-Converter-ubuntu-x64.AppImage](https://github.com/whs2009/EasyPath-Converter/releases/latest/download/EasyPath-Format-Converter-ubuntu-x64.AppImage) |
| Ubuntu ARM64 | [EasyPath-Format-Converter-ubuntu-arm64.AppImage](https://github.com/whs2009/EasyPath-Converter/releases/latest/download/EasyPath-Format-Converter-ubuntu-arm64.AppImage) |
| CentOS Stream 9 x86-64 | [EasyPath-Format-Converter-centos-stream9-x64.AppImage](https://github.com/whs2009/EasyPath-Converter/releases/latest/download/EasyPath-Format-Converter-centos-stream9-x64.AppImage) ([SHA-256](https://github.com/whs2009/EasyPath-Converter/releases/latest/download/EasyPath-Format-Converter-centos-stream9-x64.AppImage.sha256)) |

Use the SHA-256 file in the CentOS row to verify that AppImage, or use the checksum list for the complete release.

On Windows, compare `Get-FileHash .\<filename> -Algorithm SHA256` with the published checksum. On macOS use `shasum -a 256`, then open the DMG. On Ubuntu or CentOS use `sha256sum` and give the AppImage execute permission before launching it. No Python installation is needed for packaged downloads. Follow your organization's signing and execution policies.

## Convert slides

1. Open **Add files** and select one or more supported scanner WSIs. Keep MRXS companions together. General uses OpenSlide for supported scanner formats (including NDPI, SVS, MRXS, SCN and BIF); it also accepts supported EP and tiled/focal TIFF inputs. Output support does not imply DICOM is a converter input.
2. Select a row to edit its output **stem**. Choose an existing destination with enough space for temporary data and output.
3. Select **EP**, **TIFF** or **DICOM**. EP defaults to **8 bits/channel, 5% maximum error, JXL distance 4**. Choose 5–8 bits, 0–15% and distance 1–15. Only 8 bits at 0% preserves all canonical RGB8 samples. TIFF and DICOM use lossless output encoding; they cannot restore detail lost at the scanner.
4. Use the embedded color profile when available; confirm sRGB only if appropriate. DICOM requires verified acquisition/specimen facts and geometry. A DICOM result is a directory named `<stem>.dcm` containing separate instances. Keep it intact. See the [field guide](tools/format_converter/USER-GUIDE.md).
5. Start the batch. Each file has a separate result; cancellation does not delete already completed outputs. Supported focal stacks preserve their planes. EP supports up to 20 matched planes, with a 500,000 x 500,000 per-plane ceiling; lossless TIFF/DICOM cap the long and short axes at 200,000 and 100,000 pixels per plane. Other bounds and geometry checks still apply.

## Understand EP

Read the [byte-level EP specification](docs/ep-format.md) and [minimal independent writer](docs/minimal_ep.py) for the header, metadata, tile index, precision, JPEG XL/solid records, memory layout and stack envelope. [Preliminary benchmarks](docs/preliminary-benchmarks.md) report original and output sizes, conversion time and matched original-SDK versus EP tissue reads for NDPI, MRXS, iSyntax, BIF and four SVS slides. Broader validation is planned.

## Scope and licensing

This General distribution contains **no Philips PixelEngine or Hamamatsu NDP.read SDK**. OpenSlide support for an NDPI is not a vendor license. For a restricted edition, first obtain vendor authorization covering the intended use and platform, then contact the EasyPath core team. Use public issues only for nonconfidential inquiries. Do not post slide images, barcodes, patient identifiers, license letters, credentials or annotation text.

The EasyPath-authored standalone source and examples in this repository are licensed under [Apache-2.0](LICENSE). Third-party components retain their own licenses and notices. The private EasyPath Server is not distributed or licensed by this repository. This software is not clinically validated.

## Build from source

Use a clean Python 3.12 environment on a supported native machine, a C++17 compiler and CMake. Install `tools/format_converter/requirements-build.txt`, then run `python packaging/build.py` from a fresh checkout (Linux: under Xvfb for GUI tests). The [release workflow](.github/workflows/general.yml) lists Linux build dependencies. The build downloads hash-checked FLTK, compiles the native UI, freezes the local reader/codec engine with its license notices and tests the packaged app with generated nonclinical data. No credentials or private server checkout are required to build.
