import json
import os
import sys

from pathlib import Path

import xxhash

items = []
output_name = "expected.json"
root = Path(sys.argv[1])
for current_folder, folder_names, file_names in os.walk(str(root)):
    for file_name in file_names:
        if file_name != output_name:
            file = Path(current_folder) / file_name

            checksum = xxhash.xxh3_64()
            with file.open('rb') as file_object:
                checksum.update(file_object.read())

            strings = [
                str(file.relative_to(root)),
                str(file.stat().st_size),
                checksum.hexdigest(),
            ]

            items.append(" ".join(strings))

with open(root.joinpath(output_name), "w", encoding="utf-8") as output:
    json.dump(sorted(items), output, indent=4, ensure_ascii=False)
