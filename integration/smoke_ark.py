"""Tiny synthetic text/image probe; contains no company reports or credentials."""
import argparse
import json
import struct
import zlib
from pathlib import Path

from ark_embedding import ArkEmbedding, EmbeddingError


def synthetic_png() -> bytes:
    def chunk(kind, data):
        return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))
    rows = []
    for y in range(256):
        row = bytearray(b"\0")
        for x in range(256):
            color = (244, 247, 250)
            if y < 40:
                color = (40, 70, 110)
            elif 20 < x < 236 and 70 < y < 115:
                color = (35, 155, 90)
            elif 20 < x < 236 and 140 < y < 185:
                color = (205, 65, 65)
            row.extend(color)
        rows.append(bytes(row))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!2I5B", 256, 256, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))


def run(client: ArkEmbedding, origin: str) -> dict:
    results = []
    for kind, call in (
        ("text", lambda: client.embed_text("知识库接口接入验证：查询接口契约与执行报告。")),
        ("image", lambda: client.embed_image(synthetic_png())),
    ):
        try:
            result = call()
            results.append({"kind": kind, "status": "PASS", **result.summary()})
        except EmbeddingError as exc:
            results.append({"kind": kind, "status": "FAIL", "error": str(exc)})
    return {"origin": origin, "sample": "synthetic_nonbusiness", "results": results,
            "benchmark": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=Path(__file__).resolve().parents[1] / ".env")
    args = parser.parse_args()
    report = run(ArkEmbedding.from_env(args.env), "local_workstation")
    print(json.dumps(report, ensure_ascii=False))
    raise SystemExit(0 if all(r["status"] == "PASS" for r in report["results"]) else 1)
