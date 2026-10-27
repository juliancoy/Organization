# CompAI genome GPU worker

`lifetech-compai-gpu-qc.service` runs the existing Vulkan QC/mask engine against
the NVIDIA ICD. Startup requires physical-GPU fixtures and rejects software
fallback. It reserves one CPU and 1 GiB RAM in the shared node-local resource
pool and runs under matching systemd limits. The native controller owns its
flock lease independently of launcher-agent updates; it checks enrolled machine
identity and worker admission policy. Its process group stops before releasing
capacity. Neither application GPU clocks nor other GPU processes are changed.

`lifetech-compai-gpu-backfill.service` processes publisher-verified public Watson
reads already cached on CompAI. Each file reserves one CPU and 768 MiB through
the same pool. Results stay in the private WGS `gpu-qc/compai-backfill` directory.
The primary QC records remain the comparison baseline; a backfill is verified
only when every base was GPU-counted, full input checks pass, and the aggregate
results match the baseline. CPU fallback remains correct but cannot silently
qualify a file as fully GPU-enriched.

The MiniPC service remains independent. `--gpu-host` defaults to MiniPC for
existing backfill commands; CompAI's private host entry explicitly opts into the
native backend. The engine's `daemon.json` exposes device and batch/base counts;
backfill `state.json` records per-file progress, GPU counts and validation.

GPU activity may remain bursty: CPU FASTQ parsing/decompression and input I/O
feed relatively short kernels. Allocated VRAM is not a measure of completed
compute work. Increase concurrency only after measuring those bottlenecks and
retaining shared resource admission.

CompAI uses a cooperative QC shader: each 64-lane workgroup counts 1,024 bases
through a shared histogram. The legacy shader and dispatch remain the default
for other workers. The native parser keeps at most one GPU batch in flight while
building the next, then commits validated results on the parser thread. EOF drains
the pending batch before publisher checks; failed requests retain CPU fallback.
The additional buffer stays within the existing 768 MiB parser reservation.

On the RTX 3060, five-request medians for a 4 MiB batch improved from 13.038 ms
to 3.034 ms in the GPU stage. Three-run medians on the same cached public Watson
file (22,694,396 bases, publisher MD5 and original QC checked each run) improved
from 1.202 s to 0.834 s: 18.9 to 27.2 million bases/s, or 44% higher throughput.
These are warm-cache measurements, not a promise for download-bound work.
Kernel validation covered 18 CPU-crosschecked cases per implementation, including
partial words, workgroup boundaries, uniform input and the 64 MiB engine limit.
