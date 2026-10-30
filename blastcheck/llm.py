"""The only place blastcheck talks to an LLM.

blastcheck makes exactly two LLM calls per run (`intent` and `author`). Both go
through an `LLM` object, which is one of:

- `AnthropicLLM`: calls the Anthropic API. With `record_dir`, it saves each
  response so the run can be replayed later, and reuses any recording there that
  still matches its prompt (so re-recording only pays for steps that changed).
- `ReplayLLM`: returns saved responses instead of calling the API. Used by the
  README demo and the test suite, so they're reproducible and free.

A recording stores a hash of the exact prompt it answered. Replaying it against a
different prompt (because the module, the diff or a prompt template changed) is an
error, not a silent stale answer.
"""

import hashlib
import json
from pathlib import Path
from typing import Any, Protocol

import anthropic

DEFAULT_MODEL = "claude-opus-5-5"

# Models that support server-side refusal fallbacks (`fallbacks: "default"`).
# On a safety decline the API re-runs the request on a suitable model in the same call.
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(Exception):
    """The LLM call failed, was refused, or a recording doesn't match."""


class LLM(Protocol):
    def complete(self, step: str, system: str, prompt: str, schema: dict | None = None) -> str:
        """Return the model's text answer. With `schema`, the answer is JSON matching it."""
        ...


def prompt_hash(system: str, prompt: str, schema: dict | None) -> str:
    payload = json.dumps({"system": system, "prompt": prompt, "schema": schema}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


class AnthropicLLM:
    def __init__(self, model: str = DEFAULT_MODEL, record_dir: Path | None = None):
        self.model = model
        self.record_dir = record_dir
        self.client = anthropic.Anthropic()

    def complete(self, step: str, system: str, prompt: str, schema: dict | None = None) -> str:
        if self.record_dir is not None:
            try:
                return ReplayLLM(self.record_dir).complete(step, system, prompt, schema)
            except LLMError:
                pass  # no matching recording yet: ask the API

        response = self.client.beta.messages.create(**self._request(system, prompt, schema))

        if response.stop_reason == "refusal":
            raise LLMError(f"{step}: the model declined the request ({response.stop_details})")
        if response.stop_reason == "max_tokens":
            raise LLMError(f"{step}: the answer was cut off at max_tokens")

        text = "".join(block.text for block in response.content if block.type == "text")
        if self.record_dir is not None:
            self._record(step, system, prompt, schema, text)
        return text

    def _request(self, system: str, prompt: str, schema: dict | None) -> dict[str, Any]:
        output_config: dict[str, Any] = {"effort": "high"}
        if schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 16000,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": output_config,
        }
        if self.model in _FALLBACK_MODELS:
            request["betas"] = [_FALLBACK_BETA]
            request["fallbacks"] = "default"
        return request

    def _record(self, step: str, system: str, prompt: str, schema: dict | None, text: str) -> None:
        self.record_dir.mkdir(parents=True, exist_ok=True)
        recording = {
            "step": step,
            "model": self.model,
            "prompt_sha256": prompt_hash(system, prompt, schema),
            "response": text,
        }
        (self.record_dir / f"{step}.json").write_text(json.dumps(recording, indent=2) + "\n")


class ReplayLLM:
    def __init__(self, replay_dir: Path):
        self.replay_dir = replay_dir

    def complete(self, step: str, system: str, prompt: str, schema: dict | None = None) -> str:
        path = self.replay_dir / f"{step}.json"
        if not path.exists():
            raise LLMError(f"{step}: no recording at {path}")
        recording = json.loads(path.read_text())
        if recording["prompt_sha256"] != prompt_hash(system, prompt, schema):
            raise LLMError(
                f"{step}: recording at {path} was made for a different prompt. "
                "Re-record it with --record."
            )
        return recording["response"]
