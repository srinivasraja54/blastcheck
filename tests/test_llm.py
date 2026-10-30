import json
import os

import pytest

from blastcheck.cli import load_dotenv
from blastcheck.llm import AnthropicLLM, LLMError, ReplayLLM, prompt_hash


def _save_recording(directory, step, system, prompt, schema, response):
    directory.mkdir(parents=True, exist_ok=True)
    recording = {
        "step": step,
        "model": "test",
        "prompt_sha256": prompt_hash(system, prompt, schema),
        "response": response,
    }
    (directory / f"{step}.json").write_text(json.dumps(recording))


def test_replay_returns_the_recorded_answer(tmp_path):
    _save_recording(tmp_path, "intent", "sys", "prompt", None, '{"ok": true}')

    assert ReplayLLM(tmp_path).complete("intent", "sys", "prompt") == '{"ok": true}'


def test_replay_refuses_a_recording_made_for_a_different_prompt(tmp_path):
    _save_recording(tmp_path, "intent", "sys", "old prompt", None, "{}")

    with pytest.raises(LLMError, match="different prompt"):
        ReplayLLM(tmp_path).complete("intent", "sys", "new prompt")


def test_replay_without_a_recording_fails_clearly(tmp_path):
    with pytest.raises(LLMError, match="no recording"):
        ReplayLLM(tmp_path).complete("author", "sys", "prompt")


def test_request_uses_structured_output_and_fallbacks(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    schema = {"type": "object"}

    request = AnthropicLLM("claude-opus-5-5")._request("sys", "prompt", schema)

    assert request["output_config"]["format"] == {"type": "json_schema", "schema": schema}
    assert request["fallbacks"] == "default"


def test_request_skips_fallbacks_for_models_without_them(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    request = AnthropicLLM("claude-haiku-5-5")._request("sys", "prompt", None)

    assert "fallbacks" not in request
    assert "format" not in request["output_config"]


def test_dotenv_loads_keys_but_never_overrides_the_shell(tmp_path, monkeypatch):
    monkeypatch.setenv("ALREADY_SET", "from-shell")
    monkeypatch.delenv("FROM_FILE", raising=False)
    monkeypatch.delenv("QUOTED", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\nFROM_FILE=abc\nexport QUOTED=\"x y\"\nALREADY_SET=from-file\n")

    load_dotenv(env_file)

    assert os.environ["FROM_FILE"] == "abc"
    assert os.environ["QUOTED"] == "x y"
    assert os.environ["ALREADY_SET"] == "from-shell"
