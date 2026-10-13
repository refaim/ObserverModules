# Zanzarah PAK: the four-byte block field

Research note for [refaim/unpackrat issue #36](https://github.com/refaim/unpackrat/issues/36)
("Zanzarah: Figure out the meaning of 4-byte attr field"). It documents what the field is
and is not on the available evidence; it is documentation only and changes no parser or
footer behavior.

## Header and table of contents

All integers are little-endian. Layout observed across the three corpus archives:

    offset 0     u32 LE  0x00000000     four zero bytes (the module signature)
    offset 4     u32 LE  entry count
    offset 8     TOC rows, entry_count times:
                   u32 LE  path length
                   bytes   path (path length bytes)
                   u32 LE  block offset   relative to body start
                   u32 LE  block size     includes the four-byte prefix
    body start   end of TOC (observed 493424 for every corpus archive)
    body         packed file blocks

Each TOC row describes one block. The pre-change extractor in
`src/modules/zanzarah/zanzarah.cpp` ignores the block's first four bytes and returns the
range `offset + 4` with length `block_size - 4`. Since the TOC `block offset` is relative
to body start, the absolute extraction start is
`absolute_start = body_start + relative_block_offset + 4`, with length `block_size - 4`.
Throughout this note any later `offset + 4` shorthand is this per-block
`relative_block_offset` (relative to `body_start`), not an absolute file offset.

## The field itself

For all three corpus archives the block prefix DWORD is `0x00000101` (9549 of 9549 blocks
each, 28647 total); on disk the prefix bytes are `01 01 00 00`. The paired trailer value
seen in two archives is `0x00000202` (`02 02 00 00`).

On this evidence the field is best described as a four-byte block-start marker (a prefix).
It should not be mapped onto Windows file attributes, and it is not established to be a
decoded bitfield. There is no original-engine evidence about whether the engine validates
the value or what individual bits mean.

The original Luigi `zanzapak` packer (2005/2006) is the only writer-side evidence. It reads
the DWORD as a per-block `attr`, then writes `[attr][literal file bytes]` and records
`size = filelength + 4`, with no footer. The prefix-only ("legacy") framing therefore has a
real writer basis: it is exactly what that packer emits. That does not make the variable
name semantic evidence, and it does not establish that the engine validates the value, what
any bit means, or where a given corpus block came from.

## Body layout

- Sorted by block offset, the blocks tile the body exactly for all three archives: block
  size equals the gap to the next offset, with no gaps or overlaps. Observed, not a proven
  universal constraint.
- TOC row order differs from physical block order.

## Marker pair (0x101 ... 0x202)

For corpus2 and corpus3 every one of the 9549 blocks is
`[u32 LE 0x101][payload][u32 LE 0x202]`: a prefix plus a constant trailing DWORD `0x202`.
The stored block size includes both markers, so the payload length equals `block_size - 8`.
Internal lengths confirm this: 3676 BMP satisfy `block_size - bfSize == 8`, and 532
RIFF/WAV satisfy `block_size - (RIFF size + 8) == 8`. The candidate exact file range there
is `offset + 4`, length `block_size - 8`.

corpus1 differs. Its trailing DWORD is `0x101` in 9511 of 9549 blocks and the penultimate
DWORD is `0x202` in 9497 of 9549. 3657 BMP carry trailer `[0x202, 0x101]` (total overhead
12); the other 19 BMP have total overhead 8 with mixed endings (12 end in `0x101`, 7 in
other DWORDs). 502 WAV carry overhead 12 with trailer `[0x202, 0x101]`; 30 WAV have
overhead 4 with no bytes after the declared RIFF extent. No uniform trim rule holds for
corpus1.

The 9497 `[0x202, 0x101]` suffix-pair figure is not the whole story. A physical scan of
corpus1 finds 9496 blocks ending `0x202, 0x101`; one block, a DDS at TOC index 6942 that
runs to physical end of file, ends `0x202, 0x00`. That DDS still hash-matches its corpus2
counterpart at the corpus1 `trim12` length, so the 9497 `trim12` equality count stands even
though only 9496 blocks carry a literal `0x202, 0x101` pair. The count should not be read
as "9497 genuine two-marker suffixes".

On its own, corpus1 has 30 WAV under `RESOURCES\AUDIO\SFX\VOICES\AMY\` (TOC indices
391..420) whose complete RIFF chunk walk fills exactly `block_size - 4`, with no bytes
after the last chunk. Extracting those at `- 8` or `- 12` would drop valid RIFF file bytes, so
"corpus1 is always `size - 12`" is wrong. These are the same 30 WAV that differ between
corpus1 `trim12` and corpus2 `trim8`.

## Range naming used in the evidence

`trimN` here means: start after the four-byte prefix (`offset + 4`) and take a candidate
length of `block_size - N`. It does not mean removing N bytes from the end after the
prefix. So `trim4` is the pre-change extractor, `trim8` matches corpus2 and corpus3 blocks
with a single trailing marker, and `trim12` matches the corpus1 BMP/WAV blocks with two
trailing markers.

## Cross-archive byte-range comparison

Full streamed SHA-256 over same-named ranges:

| comparison                        | ranges | equal | differing |
| --------------------------------- | ------ | ----- | --------- |
| corpus1 trim12 vs corpus2 trim8   | 9549   | 9497  | 52        |
| corpus1 trim12 vs corpus3 trim8   | 9549   | 9489  | 60        |
| corpus2 trim8  vs corpus3 trim8   | 9549   | 9541  | 8         |

corpus1(trim12) vs corpus2(trim8): the 52 differing ranges are 30 WAV, 19 BMP and 3 DDS;
matched counts by extension are WAV 502, MP3 8, BMP 3657, SKA 575, DFF 1589, DDS 2905,
BSP 261. corpus2(trim8) vs corpus3(trim8): the 8 differing ranges are 5 BMP, 1 DDS and
2 BSP. Equality here establishes byte-range equivalence only, and says nothing about
provenance (original, Steam, repacked or localized). The origin of each corpus file is not
remembered, so no variant is labeled.

## Trailing markers are not safely removable

`TEST_MUSIC.WAV` has an inner RIFF full size of 1778748. Its corpus1 block is 1778760 with
trailer `[0x202, 0x101]`; its corpus2 and corpus3 block is 1778756 with trailer `[0x202]`.
Pre-change extraction therefore includes the known footer bytes for corpus2 and corpus3. A
blanket `-8` or `-12` trim cannot fit all three archives.

Arbitrary payloads may contain or end with marker-like DWORDs, so trimming by value alone
(scanning for `0x101`/`0x202`) is unsafe, and no extension-dependent parser rule is
justified by the evidence.

The blocks that differ between corpus1 `trim12` and corpus2 `trim8` show why no per-entry
trim can be derived from inner headers. The 19 differing BMP have `bfSize == block_size - 8`:
12 end in a trailing `0x101` and 7 in another DWORD. For 13 of those BMP the declared
`bfSize` differs from the computed pixel end by two bytes; that alone does not establish an
invalid file-size header. The 3 differing DDS
(TOC indices 4478, 4480, 4614) are RGB32 256x256 with 9 mip levels: the computed texture is
349652 bytes and the block is 349660 bytes = prefix 4 + texture 349652 + an extra DWORD
`0x101`. DDS payloads differ from corpus2 despite matching headers; neither variant's
provenance is established.

Inference (not established): the extra trailer or overread bytes in some corpus1 ranges are
consistent with a repack that carried over bytes present in the source the repacker read,
but no writer, tool or corpus provenance has been proven, so this stays a hypothesis.

## Golden and fixture implications

- The existing external golden expected sizes are all `block_size - 4`, matching the
  pre-change extractor. `src/tests/framework/tools/make_listing.py` hashes already-extracted
  files, so it cannot independently prove framing; regenerating goldens with the same
  extractor would be circular.
- The synthetic fixture in `src/tests/support/archive_fixtures.cpp` writes the prefix
  `0x12345678` and no footer. That represents the current parser contract, not a known
  canonical framing.

## History

Commit `5da17512` ("Zanzarah: Fix unpacking (fixes #25)") changed extraction from
`size - 12` to `size - 4`. The earlier `-12` code carried a comment admitting uncertainty.
There is no evidence about which program produced the current corpus goldens.

## References

- Helco/zzio `PAKArchive.cs`: reads `offset + 4`, length `- 8`, with a comment that the
  file is surrounded by four bytes on each side.
  <https://github.com/Helco/zzio/blob/16a1aa90e818e8b568727f7480d5835c22b5ad35/zzio/PAKArchive.cs#L22>
- ctpax-x `zanunpak.dpr` (Delphi, 2010): comment describing the marker pair 101, data,
  202, with `offset + 4`, length `- 8`.
  <https://www.ctpax-x.org/index.php?goto=files&lang=en&show=94>
- Luigi Auriemma `zanzapak` (C, 2005/2006): reads the DWORD as `attr`, prints and
  preserves it for repacking, and extracts `- 4`. The variable name is not semantic
  evidence; this is a standalone C program, not a QuickBMS script.
  <https://aluigi.altervista.org/papers/zanzapak.zip>
- kabbi/zanzarah-tools `zanzapack.c` fork: uses `- 12`, showing divergent tools rather
  than a universal contract.
  <https://github.com/kabbi/zanzarah-tools/blob/c1862c483dfa84783761273a87a0b334ba2ea705/vendor/zanzapack/zanzapack.c>
- unpackrat commit `5da17512`:
  <https://github.com/refaim/unpackrat/commit/5da17512cc25f524cfbc0f800ed238dda2ee8d63>

## Reproduction evidence

Research date 2026-10-13 (Vladivostok; reads began 2026-10-12). The corpus is the private
optional external set `M:\observer\_test\zanzarah`; it is not part of a fresh checkout.

| file          | size      | SHA-256 |
| ------------- | --------- | ------- |
| zanzarah1.pak | 774465076 | 4f348acfa91e87f99cb73945eb841b2e0040bc46340f4ccc7370d77fcb18ea44 |
| zanzarah2.pak | 774106628 | f3dba1ece1ee9e5c28d0121895cce7158a8f642291ae022b75e144f825475da6 |
| zanzarah3.pak | 774010464 | c1ce0ab5fbaf0e4ef9e03461070865314743689531e959083f6460b938334432 |

Local research utilities under `out/research/issue-36` are not checked in and not
publicly available; these hashes locate local evidence only:

- `deep-compare.py`: `8993b97e195f3ee7b9d67b5bff8e0ef3ae5f69a2e05942518977a52168538626`
- `deep-compare.json`: `1d52c07e370acb40175be8699bee49e9518c5dbda6bfa331e4816deebee60612`

The utility's synthetic self-tests pass (re-run by the lead). The corpus run used
`max_files = null` (full archives). Reproducing these numbers from a fresh checkout
requires both the utilities and the external corpus; the structural facts do not follow
from the repository alone.

## Supported layout policy

The chosen supported layout is an all-or-nothing, whole-archive predicate, not a per-entry
trim. Before any extraction:

1. Fully validate every TOC row and every full block range (offset/size arithmetic, body
   bounds, no overflow). If any bounds check fails, reject the archive; the framing choice
   is not reached.
2. A canonical archive is one where every block has `block_size >= 8`, its first DWORD is
   `0x101` and its last DWORD is `0x202`. For a canonical archive extract `offset + 4` with
   length `block_size - 8`. An empty canonical payload (`block_size == 8`) is permitted.
3. Otherwise treat the entire archive as literal legacy framing: extract `offset + 4` with
   length `block_size - 4` for every block.

There is no per-entry trim, no scanning for `0x101`/`0x202` markers, and no
extension/internal-format parser. This keeps corpus1 (not all-canonical) byte-for-byte
equal to the pre-change `- 4` output and applies the `- 8` canonical rule to corpus2 and corpus3.

Ambiguity: a prefix-only literal archive with every prefix equal to `0x101` and every
payload ending in `0x202` is indistinguishable from a canonical archive under this
predicate. The predicate is an
explicit support policy for the observed corpus, not a proven universal engine
specification. This note states the policy only; the parser change and its test evidence
are owned separately and are not asserted here.

Unknown: the engine-level semantics of the marker (whether it is validated, whether any bit
carries meaning) and the provenance of all three archives remain unresolved.
