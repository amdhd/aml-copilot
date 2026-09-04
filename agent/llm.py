"""OpenAI-compatible LLM access. The provider is a base_url plus a model name,
so Gemini, DeepSeek, Groq and a local vLLM server are all a config change.

Prompts are built [system][case data], in that order, never interleaved, so the
prefix stays stable and provider-side prompt caching can hit it.
"""

import json
import os

from openai import OpenAI
from pydantic import BaseModel, ValidationError

BASE_URL = os.environ.get(
    "AML_LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
MODEL = os.environ.get("AML_LLM_MODEL", "gemini-flash-latest")
_client = None


def client() -> OpenAI:
    global _client
    if _client is None:
        key = os.environ.get("AML_LLM_API_KEY") or os.environ.get("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("set GEMINI_API_KEY (or AML_LLM_API_KEY)")
        # Free tiers rate-limit hard. The SDK retries 429/5xx with exponential
        # backoff; this is transport-level and separate from the schema retry.
        _client = OpenAI(base_url=BASE_URL, api_key=key, max_retries=5)
    return _client


def _usage(response) -> dict:
    u = response.usage
    cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0)
    return {"prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens,
            "cached_tokens": cached or 0}


def complete_json(system: str, case_data: str, schema: type[BaseModel]):
    """One retry on invalid JSON. A second failure raises -- no silent fallback."""
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": case_data}]
    for attempt in range(2):
        response = client().chat.completions.create(
            model=MODEL, messages=messages, temperature=0,
            response_format={"type": "json_object"})
        raw = response.choices[0].message.content
        try:
            return schema.model_validate_json(raw), _usage(response)
        except ValidationError as error:
            if attempt == 1:
                raise RuntimeError(f"{MODEL} returned invalid JSON twice: {error}") from error
            messages += [
                {"role": "assistant", "content": raw},
                {"role": "user", "content":
                 f"That did not validate: {error}. Return only JSON matching "
                 f"this schema: {json.dumps(schema.model_json_schema())}"}]
