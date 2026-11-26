# Preliminary EP measurements

These are historical **measured** development runs, not predictions for the public binaries. Eight inputs, one workstation, 8 bits/channel, maximum 5% NRMSE, JPEG XL distance 4. MB is decimal; output % is EP bytes / source bytes. MRXS includes its companion files.

| Source | Original MB | EP MB | Output % | Full conversion min | Original SDK read ms | EP read ms |
|---|---:|---:|---:|---:|---:|---:|
| NDPI | 151.25 | 19.93 | 13.2 | 2.56 | 10.75 | 20.72 |
| MRXS | 1094.43 | 214.23 | 19.6 | 66.23 | 24.33 | 24.47 |
| iSyntax¹ | 701.41 | 196.23 | 28.0 | 20.89 | 89.98 | 40.44 |
| BIF | 2717.83 | 291.66 | 10.7 | 23.76 | 82.08 | 26.27 |
| SVS A | 634.10 | 140.45 | 22.1 | 12.10 | 535.19 | 24.95 |
| SVS B | 170.53 | 29.15 | 17.1 | 3.40 | 409.62 | 20.87 |
| SVS C | 267.83 | 30.25 | 11.3 | 4.46 | 397.36 | 20.42 |
| SVS D | 163.71 | 18.47 | 11.3 | 3.02 | 407.75 | 20.22 |

¹ The original iSyntax was read in an authorized vendor environment. The General converter does **not** open native iSyntax. This is a comparison of image representations, not a public feature claim.

Time covers standalone source open and reads, reference pyramid generation, encoding, final uncached pixel verification and close. It excludes separate pre/post hashing and setup. It is neither pure RGB-to-JXL time nor click-to-finish time on EasyPath Server. Read times are means of the first matched 1024 x 1024 tissue read in each pair, using aligned and unaligned regions at several equivalent magnifications (40x down to 0.625x), across three fresh reader processes. There were 126 observations per slide except iSyntax (84). Fresh application state does not imply a cold OS disk cache.

**What changed with single-color records?** A separate MRXS run on the same source reduced EP from 214.23 to 160.32 MB and full conversion from 66.23 to 47.05 minutes. The newer file held 369,488 exact solid records and 38,683 JXL records. In that run mean tissue reads were 22.27 ms original, 24.19 ms old EP and 23.01 ms new EP; uniform-region reads were 5.42, 23.95 and 11.30 ms respectively. Large blank canvases benefit more than dense tissue. The table above is the earlier series, not a mixture of best results from different runs.

The original NDPI reader was faster than EP on its measured tissue reads. No universal speedup, compression ratio or diagnostic-equivalence claim follows from these cases. A broader examination is planned: denser and sparser tissue, multiple scanners and platforms, per-zoom distributions, cold and warm storage, fidelity of fine structures, separate source-read/codec/verification timing, and UI end-to-end latency. No patient data accompany these results.
