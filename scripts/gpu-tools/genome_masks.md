# Tiled reference-status mask prototype

This tool counts supplied evidence. Missing evidence is unknown. It does not infer reference matches from an absent VCF record, compare alternate alleles, publish chart data, or replace the Genome Explorer categories.

A manifest declares GRCh38, one canonical chromosome, its length, a zero-based half-open region (`start`, `end`), six sample IDs in the order Julian, HG001, HG002, HG005, Watson, Venter, and an explicit `reference_N` interval list. Each sample can provide these interval lists:

- `confirmed_reference`: independently established reference evidence.
- `variant_ref_spans`: full original and normalized ALT REF-span exclusions, including deletion interiors and insertion anchors. These are not confident alternate genotypes.
- `confident_variant_starts`: independently confident non-reference call anchors, not deletion interiors.
- `unknown`: explicit uncertainty, overriding other evidence.

Set `source_scope` to `synthetic_known_fixture` or `curated_confidence_evidence`; retain source hashes and confidence-model provenance in the manifest. Lists use chromosome coordinates, not tile-relative coordinates. The CLI censors all Watson evidence on chromosome 19. Reference Ns, other reference ambiguities, and conflicting reference/span evidence stay unknown.

The three exclusive counts per person are confirmed reference, confident variant-site starts, and unknown. `variant_span_covered` is an overlapping diagnostic count. Pairwise counts report jointly known **reference-status** agreement or disagreement; two non-reference starts do not imply matching alleles or genotypes. No jointly known positions produces a null agreement fraction.

## CPU path

The interval sweep avoids materializing dense masks and needs no GPU:

```sh
python -m scripts.gpu_genome_masks --manifest /private/masks.json \
  --cpu-only --output /private/mask-results
```

Each run writes owner-only aggregate results and provenance outside Git. The CPU path is appropriate when sparse intervals are already available.

## Persistent Vulkan daemon

Use the existing serialized GPU QC daemon, so mask requests share its warm context and resource lease rather than starting competing GPU contexts:

```sh
python -m scripts.gpu_genome_masks --manifest /private/masks.json \
  --host julian-minipc.local --hosts /private/verified-hosts.json \
  --socket /home/julian/.local/state/lifetech-launcher/gpu-qc/service.sock \
  --batch-dir /home/julian/.local/state/lifetech-launcher/gpu-qc/batches \
  --output /private/mask-results --tile-positions 1048576
```

Omit `--host` and `--hosts` for a local socket. Remote requests verify signed worker identity/admission and use enrolled pinned SSH. Temporary tile files are removed after each serialized request. The client requires physical Vulkan hardware and exact agreement of all 69 counters with its independent CPU interval oracle.

Tiles contain at most 1,048,576 positions. Each position is one little-endian uint32: six four-bit sample fields (`REF=1`, `REF-span=2`, `unknown=4`, `confident variant-start=8`), plus bit 31 for reference N. GPU counters are uint32 per bounded invocation; the host reduces into uint64. Output is 24 person counters plus 15 person pairs × three counters. GPU work is coalesced across invocations.

## Standalone build and benchmark

The separate native harness keeps its Vulkan context alive across tiles and crosschecks each tile against a native CPU oracle:

```sh
cc -O3 -march=native scripts/gpu-tools/genome_masks.c -lvulkan -o /private/genome_masks
glslangValidator -V scripts/gpu-tools/genome_masks.comp -o /private/genome_masks.spv
python -m scripts.gpu_genome_masks --manifest /private/masks.json \
  --executable /private/genome_masks --shader /private/genome_masks.spv \
  --output /private/mask-results
```

`--pack-only` writes a private packed fixture plus CPU oracle. The native harness also supports `genome_masks --cpu-only INPUT.bin TILE_POSITIONS` for single-thread timing. Mask buffers are about 4.27 MiB at the maximum tile size; the daemon additionally owns its existing bounded QC buffers/context.

A real 2,097,152-position chr1 GIAB fixture passed on AMD Renoir with all counters exact. Only HG001/HG002/HG005 had curated evidence; the other three people remained entirely unknown. The native CPU baseline took 55.9 ms; GPU engine requests took 59.6 ms total including initial warmup, and the full LAN/controller path took 8.34 seconds. These measurements establish correctness, **not end-to-end acceleration**. Production defaults and charts remain unchanged.
