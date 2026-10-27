"""knowledge-base Ark multimodal embedding adapter; standard library only."""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


class EmbeddingError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Do not forward the credential to redirected hosts.


@dataclass
class EmbeddingResult:
    vector: list[float]
    model: str
    request_id: str
    elapsed_ms: int
    usage: dict

    def summary(self) -> dict:
        return {
            "model": self.model, "dimension": len(self.vector),
            "request_id": self.request_id, "elapsed_ms": self.elapsed_ms,
            "usage": self.usage, "finite": True,
        }


class ArkEmbedding:
    def __init__(self, api_key: str, model: str, *,
                 base_url: str = "https://ark.cn-beijing.volces.com/api/v3",
                 dimensions: int = 2048, timeout: float = 40):
        url = urlparse(base_url)
        if (url.scheme != "https" or url.hostname != "ark.cn-beijing.volces.com"
                or url.username or url.password or url.query or url.fragment
                or url.path.rstrip("/") != "/api/v3" or url.port not in (None, 443)):
            raise EmbeddingError("Use the verified official Ark HTTPS base URL")
        if not api_key or not model:
            raise EmbeddingError("Missing Ark API key or model")
        if dimensions <= 0 or timeout <= 0:
            raise EmbeddingError("Dimensions and timeout must be positive")
        self._api_key = api_key
        self.model, self.dimensions, self.timeout = model, dimensions, timeout
        self.url = base_url.rstrip("/") + "/embeddings/multimodal"
        self._opener = urllib.request.build_opener(_NoRedirect())

    @classmethod
    def from_env(cls, path: Path | None = None) -> ArkEmbedding:
        cfg = {}
        if path is not None:
            for line in path.read_text().splitlines():
                if line.startswith("KB_ARK_") and "=" in line:
                    key, value = line.split("=", 1)
                    value = value.strip()
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                        value = value[1:-1]
                    cfg[key] = value
        cfg.update({k: v for k, v in os.environ.items() if k.startswith("KB_ARK_")})
        return cls(
            cfg.get("KB_ARK_API_KEY", ""),
            cfg.get("KB_ARK_EMBEDDING_ENDPOINT_ID") or cfg.get("KB_ARK_EMBEDDING_MODEL", ""),
            base_url=cfg.get("KB_ARK_BASE_URL") or "https://ark.cn-beijing.volces.com/api/v3",
            dimensions=int(cfg.get("KB_ARK_EMBEDDING_DIMENSIONS") or 2048),
        )

    def _embed(self, parts: list[dict], instructions: str | None) -> EmbeddingResult:
        # Parts describe ONE combined item, not a batch of independent documents.
        body = {"model": self.model, "input": parts,
                "encoding_format": "float", "dimensions": self.dimensions}
        if instructions is not None:
            body["instructions"] = instructions
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers={
            "Authorization": "Bearer " + self._api_key, "Content-Type": "application/json",
        })
        started = time.perf_counter()
        try:
            with self._opener.open(req, timeout=self.timeout) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                error = json.loads(exc.read()).get("error", {})
                code = str(error.get("code", "unknown"))
            except (ValueError, AttributeError):
                code = "unknown"
            # Never display raw server bodies, request headers or input content.
            code = code.replace(self._api_key, "[REDACTED]")[:120]
            raise EmbeddingError(f"Ark HTTP {exc.code}; code={code}") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise EmbeddingError("Ark network/timeout or JSON response failure") from None
        if not isinstance(data, dict) or not isinstance(data.get("data"), dict):
            raise EmbeddingError("Invalid multimodal response shape")
        vector = data["data"].get("embedding")
        if (not isinstance(vector, list) or len(vector) != self.dimensions
                or any(isinstance(v, bool) or not isinstance(v, (float, int))
                       or not math.isfinite(v) for v in vector)
                or not any(v != 0 for v in vector)):
            raise EmbeddingError("Invalid or mismatched embedding vector")
        if data.get("model") != self.model and not self.model.startswith("ep-"):
            raise EmbeddingError("Returned model differs from configured vector space")
        return EmbeddingResult(vector, data.get("model", ""), data.get("id", ""),
                               round((time.perf_counter() - started) * 1000), data.get("usage", {}))

    def embed_text(self, text: str, *, instructions: str | None = None) -> EmbeddingResult:
        if not text.strip():
            raise EmbeddingError("Text must not be empty")
        return self._embed([{"type": "text", "text": text}], instructions)

    def embed_image(self, image: bytes, *, mime: str = "image/png",
                    instructions: str | None = None) -> EmbeddingResult:
        if mime not in ("image/png", "image/jpeg", "image/webp") or not image:
            raise EmbeddingError("Provide a nonempty PNG/JPEG/WebP image")
        url = "data:" + mime + ";base64," + base64.b64encode(image).decode()
        return self._embed([{"type": "image_url", "image_url": {"url": url}}], instructions)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=Path(__file__).resolve().parents[1] / ".env")
    parser.add_argument("--text")
    parser.add_argument("--image", type=Path)
    args = parser.parse_args()
    if bool(args.text) == bool(args.image):
        parser.error("Provide exactly one of --text or --image")
    try:
        client = ArkEmbedding.from_env(args.env)
        if args.image:
            mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(args.image.suffix.lower())
            result = client.embed_image(args.image.read_bytes(), mime=mime)
        else:
            result = client.embed_text(args.text)
        print(json.dumps(result.summary(), ensure_ascii=False))
        return 0
    except (EmbeddingError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc) if isinstance(exc, EmbeddingError) else type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
