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

## Persistent fused QC service

`scripts.gpu_qc_service` keeps one renewable CPU1 lease and one physical Vulkan context on the selected worker. It compiles `qc_engine.c` / `qc_engine.comp` using the existing worker toolchain, then runs the owner-only `qc_server.py` Unix socket. Start the coordinator with `--host WORKER.local --hosts /private/enrolled-hosts.json --state /private/service.json`. The installed `lifetech-gpu-qc.service` user unit persists across reboot and restarts failures. Leases renew every30 seconds; remote container cleanup precedes lease release. No BIOS or global device permission changes.

The host socket is `~/.local/state/lifetech-launcher/gpu-qc/service.sock` (0600). Files beneath its sibling `batches/` must be owner-only regular files without symlinks. A request is one UTF-8 JSON line, at most8192 bytes:

```json
{"version":1,"request_id":"abcd1234","sequence_path":"/absolute/private/gpu-qc/batches/batch.seq","quality_path":"/absolute/private/gpu-qc/batches/batch.qual","verify":false}
```

Sequence and raw Phred+33 quality files contain equally many bytes, without read separators, bounded by the service's admitted `max_batch_bytes` (at most16 MiB each). The server checks same-user peer credentials, paths, ownership, modes and lengths. It rejects invalid Phred bytes outside33..126. Responses echo version/request_id and report `counts` (A/C/G/T/N/other), `quality_histogram` (94 integer bins), `quality_sum`, `q20`, `q30`, hardware device, timings and `validated_backend:true`. Four startup fixtures independently compare CPU/GPU results, including all quality bins and unaligned lengths. `verify:true` also crosschecks that request against C CPU counters (`exact_match:true`); normal production avoids duplicate CPU counting and reports `exact_match:null` / `cpu_crosschecked:false`. Aggregate invariants are checked every time.

The inner C process uses a little-endian binary frame: four bytes `GPQ1`, uint32 sequence length, uint32 flags (bit0 CPU crosscheck), then N sequence bytes followed by N quality bytes. Vulkan buffers, pipeline and context persist between frames. The Python adapter serializes clients. `daemon.json` and coordinator state expose only aggregate counters and preserve them across restart. Temporary batch ownership/deletion remains the caller's responsibility; no read headers or sequence bytes appear in receipts.
