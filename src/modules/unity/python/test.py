import sys
from pathlib import Path

from archive import Archive

f = Archive(Path(sys.argv[1]))
for item in f.list_files():
    print(item)
