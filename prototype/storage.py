"""Private filesystem object adapter for the single-node prototype."""
import hashlib
import os
import re
from pathlib import Path


class FileObjects:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o750)

    def put(self, data):
        digest = hashlib.sha256(data).hexdigest()
        path = self.root / digest[:2] / digest
        path.parent.mkdir(exist_ok=True, mode=0o750)
        if not path.exists():
            tmp = path.with_name(path.name + ".tmp." + os.urandom(6).hex())
            with tmp.open("xb") as out:
                os.chmod(tmp, 0o640)
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, path)
        return digest

    def get(self, digest):
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Invalid object reference")
        data = (self.root / digest[:2] / digest).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Object checksum mismatch")
        return data

    def delete(self, digest):
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Invalid object reference")
        (self.root / digest[:2] / digest).unlink(missing_ok=True)
