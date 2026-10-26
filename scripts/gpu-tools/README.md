# Hardware Vulkan DNA QC crosscheck

This prototype counts A/C/G/T/N/other in one bounded batch of actual publisher-verified FASTQ sequence. It checks every aggregate against an optimized CPU implementation and rejects software Vulkan devices. It does not change the production QC or alignment pipeline.

The shader reads packed four-base words with coalesced accesses. Each invocation produces six private counters; the CPU reduces the small result array. There are no global per-base atomics or repeated dummy dispatches.

Run from the repository root against an enrolled worker with a verified local FASTQ:

```sh
python3 -m scripts.gpu_qc_benchmark --host WORKER.local --run RUN_ACCESSION --output /absolute/private/receipt.json
```

Optional `--hosts /absolute/private/enrolled-hosts.json` reuses an authenticated discovery snapshot. Without it, the runner discovers signed enrolled workers. `--state` selects the downloader state; `--batch-bytes` defaults to 16 MiB. The largest matching verified FASTQ on that worker supplies a prefix of complete reads. Its compressed SHA256 is checked again before extraction. Receipts contain only counts, provenance, timings and source hashes. The temporary sequence batch is deleted.

The runner takes a signed renewable CPU1 lease and derives RAM from current free capacity. Its pinned existing Python image runs with no network, a read-only root, the worker's existing `/usr` mounted read-only for GCC/Vulkan/Mesa, and only the chosen render device passed through. The device's numeric group is added only to that container. No host group membership, device permissions, BIOS setting or Docker daemon configuration changes. Containers are removed before releasing the lease.

Required worker tools: GCC, Vulkan headers/loader and a hardware ICD, `glslangValidator`, Python with gzip, and rootful Docker device access. Direct compilation, for controlled local diagnostics, is:

```sh
glslangValidator -V base_counts.comp -o count.spv
gcc -O3 -march=native base_counts.c -lvulkan -o count
./count bases.bin count.spv
```

Prefer the leased runner for swarm work.

Measured on AMD Renoir, with 71,923 verified Watson SRR000595 reads / 16,777,145 bases: CPU thread 17.665 ms, CPU wall 17.667 ms; GPU upload + command recording + dispatch + wait + readback/reduction 7.172 ms; GPU dispatch/wait 1.377 ms. Including one-time Vulkan setup, GPU cost was 32.687 ms. Thus the warm count stage was about 2.5x faster, while the cold one-shot operation was slower than CPU. Earlier wall-time measurements varied under shared-worker load; do not claim a fixed speedup. Gzip decompression, input parsing, and source verification are excluded from these count-stage timings. Production acceleration remains disabled; a persistent context and fused QC stages would be needed to retain a useful pipeline gain.
