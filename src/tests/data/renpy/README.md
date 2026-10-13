# Oversized Ren'Py index fixture

`expanded-index-over-budget.rpa` is a 65,271-byte RPA-2.0 archive. Its index begins
at offset 32 and is a zlib stream that expands to exactly 67,108,865 `N` bytes:
one byte past the 64 MiB metadata budget. The expanded bytes are deliberately not
a valid Pickle; `PrepareFiles` must reject the stream at its size limit first.

SHA-256 of the complete archive:
`47638c44d5e160baaff37211fac2661279277295553ad9784e955738761b8200`.
The independent stdlib verifier is `build/tests/test_renpy_metadata_fixture.py`.

Regenerate from this directory with CPython 3.14.7 and its zlib-ng 1.3.1
compatibility API (the versions used for the recorded hash):

```python
from pathlib import Path
import zlib

expanded_size = 64 * 1024 * 1024 + 1
header = b"RPA-2.0 " + format(32, "016x").encode("ascii") + b"\n"
archive = header.ljust(32, b"\0") + zlib.compress(b"N" * expanded_size, 9)
Path("expanded-index-over-budget.rpa").write_bytes(archive)
```
