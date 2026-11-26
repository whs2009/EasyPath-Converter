"""Write a 3 x 2 uniform v2 member with one tile. Standard library only."""
from hashlib import sha256
import json
from pathlib import Path
import struct
import sys

def encode():
    meta = json.dumps(dict(source_color_policy="declared-srgb",
        storage_encoding="jxl-solid-rgb-v2",levels=[dict(width=3,height=2)]),
        separators=(",",":"),sort_keys=True).encode("utf-8")
    record = bytes((1,12,34,56))
    index_at = 128 + len(meta)
    entry = struct.pack("<QQ32s",index_at+48,len(record),sha256(record).digest())
    header = struct.pack("<8sHHIQQQQ32s32s16s",b"EPWSI\0\r\n",2,0,1,len(meta),1,
        index_at,index_at+48+len(record),sha256(meta).digest(),sha256(entry).digest(),bytes(16))
    return header+meta+entry+record

if __name__ == "__main__":
    with Path(sys.argv[1]).open("xb") as output:
        output.write(encode())
