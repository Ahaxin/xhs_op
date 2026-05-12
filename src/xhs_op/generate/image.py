from __future__ import annotations

import argparse
import base64
import hashlib
import logging
import mimetypes
from datetime import datetime, timezone
from pathlib import Path

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from xhs_op.config import get_settings

logger = logging.getLogger(__name__)

_OUTPUT_DIR = Path("data/assets/generated")

# Generic transient-error retry policy inside a single provider.
_TRANSIENT_RETRY = retry(
    reraise=True,
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    retry=retry_if_exception_type((ConnectionError, TimeoutError, OSError)),
)


def _output_path() -> Path:
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # Short hash makes the path collision-resistant for same-second generations.
    digest = hashlib.sha1(ts.encode() + str(id(object())).encode()).hexdigest()[:8]
    return _OUTPUT_DIR / f"{ts}-{digest}.png"


def _write_bytes(data: bytes) -> str:
    path = _output_path()
    path.write_bytes(data)
    return str(path)


@_TRANSIENT_RETRY
def _gen_gemini(prompt: str, ref_paths: list[str] | None) -> bytes:
    """Generate via google-genai. Model id under that SDK is bare `gemini-2.5-flash-image`."""
    from google import genai  # type: ignore[import-not-found]
    from google.genai import types as genai_types  # type: ignore[import-not-found]

    settings = get_settings()
    client = genai.Client(api_key=settings.gemini_api_key or None)

    contents: list[object] = [prompt]
    for ref in ref_paths or []:
        ref_path = Path(ref)
        mime, _ = mimetypes.guess_type(ref_path.name)
        contents.append(
            genai_types.Part.from_bytes(
                data=ref_path.read_bytes(),
                mime_type=mime or "image/png",
            )
        )

    response = client.models.generate_content(
        model="gemini-2.5-flash-image",
        contents=contents,
    )
    # Walk candidates → parts and pull the first inline image payload.
    for candidate in getattr(response, "candidates", []) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", []) or []:
            inline = getattr(part, "inline_data", None)
            if inline and getattr(inline, "data", None):
                raw = inline.data
                # SDK may hand us either raw bytes or a base64-encoded str.
                if isinstance(raw, bytes):
                    return raw
                return base64.b64decode(raw)
    raise RuntimeError("gemini response contained no inline image data")


@_TRANSIENT_RETRY
def _gen_openai(prompt: str, size: str) -> bytes:
    """Generate via OpenAI images API (`gpt-image-1`). No ref-image support here."""
    from openai import OpenAI

    settings = get_settings()
    client = OpenAI(api_key=settings.openai_api_key or None)
    result = client.images.generate(model="gpt-image-1", prompt=prompt, size=size)  # type: ignore[arg-type]
    b64 = result.data[0].b64_json
    if not b64:
        raise RuntimeError("openai image response missing b64_json")
    return base64.b64decode(b64)


def _dispatch(model_id: str, prompt: str, ref_paths: list[str] | None, size: str) -> bytes:
    if model_id == "gemini/gemini-2.5-flash-image":
        return _gen_gemini(prompt, ref_paths)
    if model_id == "openai/gpt-image-1":
        if ref_paths:
            logger.warning("openai/gpt-image-1 ignores ref_paths; dropping them")
        return _gen_openai(prompt, size)
    raise ValueError(f"unsupported image model: {model_id}")


def generate_image(
    prompt: str,
    *,
    ref_paths: list[str] | None = None,
    size: str = "1024x1024",
) -> str:
    """Generate an image and return its saved local path.

    Walks `settings.image_models` in order. First success wins. Each provider has its own
    in-band tenacity retries; we do NOT retry across providers.
    """
    settings = get_settings()
    attempts: list[str] = []
    for model_id in settings.image_models:
        try:
            logger.info("image.generate model=%s", model_id)
            data = _dispatch(model_id, prompt, ref_paths, size)
            return _write_bytes(data)
        except Exception as exc:  # noqa: BLE001 — we want to record any provider failure
            attempts.append(f"{model_id}: {type(exc).__name__}: {exc}")
            logger.warning("image provider %s failed: %s", model_id, exc)
    raise RuntimeError(
        "all image providers failed:\n  " + "\n  ".join(attempts) if attempts else "no providers configured"
    )


def _main() -> None:
    parser = argparse.ArgumentParser(description="XHS image generation (Nano Banana primary).")
    parser.add_argument("--prompt", required=True, help="Image prompt.")
    parser.add_argument(
        "--ref",
        action="append",
        default=None,
        help="Reference image path (repeatable). Gemini only.",
    )
    parser.add_argument("--size", default="1024x1024", help="Image size (OpenAI only).")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    path = generate_image(args.prompt, ref_paths=args.ref, size=args.size)
    print(path)


if __name__ == "__main__":
    _main()
