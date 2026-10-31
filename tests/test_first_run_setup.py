"""First-run setup persists on the server without exposing stored API keys."""
import pytest

from whale_core.configuration import SettingsController, atomic_json
from whale_core.memory.store import InMemoryStore
from whale_core.profiles import assemble_runtime


def controller(tmp_path):
    runtime = assemble_runtime(
        "local", store=InMemoryStore(), config_dir=str(tmp_path), env={},
    )
    return SettingsController(tmp_path, runtime, env={})


def test_fresh_install_shows_setup_and_saves_completion(tmp_path):
    settings = controller(tmp_path)
    first = settings.view()
    assert first["setup_completed"] is False
    assert first["model"]["api_key_configured"] is False
    settings.update({
        "setup_completed": True,
        "profile": "cloud",
        "model": {
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-chat",
            "api_key": "test-secret-not-to-display",
        },
        "intent_model_enabled": True,
    })
    value = settings.view()
    assert value["setup_completed"] is True
    assert value["model"]["api_key_configured"] is True
    assert "test-secret-not-to-display" not in repr(value)
    assert "test-secret-not-to-display" not in (
        tmp_path / "config.json"
    ).read_text(encoding="utf-8")
    restored = controller(tmp_path)
    # The local runner is configured from disk but the wizard completion
    # flag remains server-side regardless of browser and active profile.
    assert restored.view()["setup_completed"] is True


def test_setup_skip_is_idempotent_and_reopenable(tmp_path):
    settings = controller(tmp_path)
    settings.update({"setup_completed": True})
    assert settings.view()["setup_completed"] is True
    settings.update({"setup_completed": True})
    assert settings.view()["setup_completed"] is True
    settings.update({"setup_completed": False})
    assert settings.view()["setup_completed"] is False


def test_legacy_explicit_settings_do_not_interrupt_owners(tmp_path):
    atomic_json(tmp_path / "config.json", {
        "profile": "local",
        "model": {"base_url": "http://127.0.0.1:11434/v1", "model": "llama3"},
    })
    assert controller(tmp_path).view()["setup_completed"] is True


def test_invalid_setup_state_rejected_without_persisting(tmp_path):
    settings = controller(tmp_path)
    with pytest.raises(ValueError, match="setup_completed"):
        settings.update({"setup_completed": "yes"})
    assert settings.view()["setup_completed"] is False
