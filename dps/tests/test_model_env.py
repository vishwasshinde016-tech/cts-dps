"""The AZURE_* / OPENAI_* lines of .env reach the model library (process environment), without overriding."""

from __future__ import annotations

from dps.settings import export_model_env


def test_model_lines_of_env_reach_the_process_environment(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comment\nDPS_LLM_ENABLED=true\nAZURE_API_BASE=https://bank.openai.azure.com/\n"
                   "AZURE_API_VERSION=2025-04-01-preview\nAZURE_API_KEY=\nOPENAI_API_KEY='from-file'\n",
                   encoding="utf-8")
    for name in ("AZURE_API_BASE", "AZURE_API_VERSION", "AZURE_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AZURE_API_VERSION", "set-by-the-shell")
    copied = export_model_env(env)
    import os

    assert copied == ["AZURE_API_BASE", "OPENAI_API_KEY"]  # an empty key is skipped; DPS_* is pydantic's job
    assert os.environ["AZURE_API_BASE"] == "https://bank.openai.azure.com/"
    assert os.environ["AZURE_API_VERSION"] == "set-by-the-shell"  # never overridden
    assert os.environ["OPENAI_API_KEY"] == "from-file"
    assert "AZURE_API_KEY" not in os.environ


def test_no_env_file_changes_nothing(tmp_path):
    assert export_model_env(tmp_path / "missing.env") == []
