from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
from typing import Any

# Silence LiteLLM's chatty default logs before importing it.
os.environ.setdefault("LITELLM_LOG", "ERROR")

import litellm  # noqa: E402
from tenacity import (  # noqa: E402
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from xhs_op.config import get_settings  # noqa: E402

litellm.suppress_debug_info = True

logger = logging.getLogger(__name__)

_PERSONAS_DIR = Path(__file__).parent / "personas"
_FALLBACK_SYSTEM_PROMPT = "You are writing XHS-style content."

# Errors worth retrying — network blips, rate limits, upstream 5xx. Auth errors are NOT retried.
_RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    getattr(litellm, "Timeout", Exception),
    getattr(litellm, "APIConnectionError", Exception),
    getattr(litellm, "RateLimitError", Exception),
    getattr(litellm, "ServiceUnavailableError", Exception),
    getattr(litellm, "InternalServerError", Exception),
)


def _load_persona_prompt(persona: str) -> str:
    """Read persona system prompt; if Tasks 2/3 haven't shipped it yet, fall back gracefully."""
    path = _PERSONAS_DIR / f"{persona}.md"
    if not path.is_file():
        logger.warning("persona file missing: %s — using minimal placeholder", path)
        return _FALLBACK_SYSTEM_PROMPT
    return path.read_text(encoding="utf-8").strip() or _FALLBACK_SYSTEM_PROMPT


def _route_model(persona: str, model_hint: str | None) -> str:
    """Pick a model id for the given persona. `model_hint` always wins."""
    if model_hint:
        return model_hint
    settings = get_settings()
    routing = settings.model_routing
    if persona == "villa":
        # Future improvement: two-pass — call villa_hook for title+hook, then villa_body for the
        # long body, and concatenate. For now route everything to villa_body (Qwen3-Max) and let
        # the persona prompt handle both hook and body in a single completion.
        return routing.get("villa_body", routing["bulk"])
    if persona in ("stock_digest", "stock_hottake"):
        return routing.get(persona, routing["bulk"])
    return routing["bulk"]


def _is_lm_studio(model: str) -> bool:
    return model.startswith("lm_studio/")


def _completion_kwargs(model: str) -> dict[str, Any]:
    """Build extra kwargs for litellm.completion. LM Studio needs an api_base override."""
    settings = get_settings()
    if _is_lm_studio(model):
        # LiteLLM treats `lm_studio/<anything>` as OpenAI-compatible. The suffix is forwarded as
        # the `model` field; LM Studio serves whichever model is loaded regardless of the value.
        return {"api_base": settings.lmstudio_base_url, "api_key": "lm-studio"}
    return {}


@retry(
    reraise=True,
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    retry=retry_if_exception_type(_RETRYABLE_EXCEPTIONS),
)
def _call_litellm(model: str, messages: list[dict[str, str]]) -> str:
    extra = _completion_kwargs(model)
    response = litellm.completion(model=model, messages=messages, **extra)  # type: ignore[arg-type]
    # LiteLLM normalizes to OpenAI-style chat.completions response (not a stream — we don't pass stream=True).
    return response["choices"][0]["message"]["content"] or ""  # type: ignore[index]


def complete(
    persona: str,
    user_msg: str,
    *,
    model_hint: str | None = None,
    extra_context: str | None = None,
) -> str:
    """Generate text using the persona's system prompt and the configured model.

    Routes through LiteLLM with tenacity retries on transient errors only.
    """
    system_prompt = _load_persona_prompt(persona)
    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    if extra_context:
        messages.append({"role": "system", "content": extra_context})
    messages.append({"role": "user", "content": user_msg})

    model = _route_model(persona, model_hint)
    logger.info("llm.complete persona=%s model=%s", persona, model)
    return _call_litellm(model, messages)


def _main() -> None:
    parser = argparse.ArgumentParser(description="XHS LLM completion (LiteLLM router).")
    parser.add_argument(
        "--persona",
        required=True,
        choices=["villa", "stock_digest", "stock_hottake", "bulk"],
    )
    parser.add_argument("--prompt", required=True, help="User message body.")
    parser.add_argument("--model-hint", default=None, help="Override the routed model id.")
    parser.add_argument("--extra-context", default=None, help="Optional extra system context.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    text = complete(
        args.persona,
        args.prompt,
        model_hint=args.model_hint,
        extra_context=args.extra_context,
    )
    print(text)


if __name__ == "__main__":
    _main()
