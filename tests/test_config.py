from __future__ import annotations

from pathlib import Path

from app.config import load_settings


_CREDENTIAL_ENV_NAMES = (
    "TMDB_READ_TOKEN",
    "TMDB_API_KEY",
    "TMDB_CREDENTIALS_FILE",
)


def _base_env(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    media = tmp_path / "video"
    data = tmp_path / "data"
    monkeypatch.setenv("MEDIA_ROOT", str(media))
    monkeypatch.setenv("DATA_ROOT", str(data))
    for name in _CREDENTIAL_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    return media, data


def test_loads_tmdb_credentials_from_default_env_file(monkeypatch, tmp_path: Path):
    media, _ = _base_env(monkeypatch, tmp_path)
    credentials = media / "_config" / "tmdb.env"
    credentials.parent.mkdir(parents=True)
    credentials.write_text(
        "# TMDB\n"
        "TMDB_READ_TOKEN='read-token-from-file'\n"
        'TMDB_API_KEY="api-key-from-file"\n',
        encoding="utf-8",
    )

    settings = load_settings()

    assert settings.tmdb_read_token == "read-token-from-file"
    assert settings.tmdb_api_key == "api-key-from-file"
    assert settings.tmdb_credentials_file == credentials.resolve()
    assert settings.tmdb_configured is True
    assert "read-token-from-file" not in repr(settings)
    assert "api-key-from-file" not in repr(settings)


def test_falls_back_to_tmdb_txt(monkeypatch, tmp_path: Path):
    media, _ = _base_env(monkeypatch, tmp_path)
    credentials = media / "_config" / "tmdb.txt"
    credentials.parent.mkdir(parents=True)
    credentials.write_text("TMDB_API_KEY=txt-key\n", encoding="utf-8")

    settings = load_settings()

    assert settings.tmdb_read_token == ""
    assert settings.tmdb_api_key == "txt-key"
    assert settings.tmdb_credentials_file == credentials.resolve()


def test_credentials_file_takes_precedence_over_old_environment_values(
    monkeypatch, tmp_path: Path
):
    media, _ = _base_env(monkeypatch, tmp_path)
    credentials = media / "_config" / "tmdb.env"
    credentials.parent.mkdir(parents=True)
    credentials.write_text("TMDB_READ_TOKEN=new-file-token\n", encoding="utf-8")
    monkeypatch.setenv("TMDB_READ_TOKEN", "old-portainer-token")
    monkeypatch.setenv("TMDB_API_KEY", "environment-fallback-key")

    settings = load_settings()

    assert settings.tmdb_read_token == "new-file-token"
    assert settings.tmdb_api_key == "environment-fallback-key"


def test_custom_relative_credentials_path(monkeypatch, tmp_path: Path):
    media, _ = _base_env(monkeypatch, tmp_path)
    credentials = media / "private" / "tmdb.txt"
    credentials.parent.mkdir(parents=True)
    credentials.write_text("export TMDB_API_KEY=custom-key\n", encoding="utf-8")
    monkeypatch.setenv("TMDB_CREDENTIALS_FILE", "private/tmdb.txt")

    settings = load_settings()

    assert settings.tmdb_api_key == "custom-key"
    assert settings.tmdb_credentials_file == credentials.resolve()


def test_environment_variables_remain_a_backward_compatible_fallback(
    monkeypatch, tmp_path: Path
):
    media, _ = _base_env(monkeypatch, tmp_path)
    monkeypatch.setenv("TMDB_READ_TOKEN", "legacy-token")

    settings = load_settings()

    assert settings.tmdb_read_token == "legacy-token"
    assert settings.tmdb_api_key == ""
    assert settings.tmdb_credentials_file == (
        media / "_config" / "tmdb.env"
    ).resolve()


def test_legacy_new_config_location_remains_supported(monkeypatch, tmp_path: Path):
    media, _ = _base_env(monkeypatch, tmp_path)
    credentials = media / "New" / "_config" / "tmdb.env"
    credentials.parent.mkdir(parents=True)
    credentials.write_text("TMDB_READ_TOKEN=legacy-new-token\n", encoding="utf-8")

    settings = load_settings()

    assert settings.tmdb_read_token == "legacy-new-token"
    assert settings.tmdb_credentials_file == credentials.resolve()


def test_empty_root_file_does_not_hide_valid_legacy_file(monkeypatch, tmp_path: Path):
    media, _ = _base_env(monkeypatch, tmp_path)
    root_credentials = media / "_config" / "tmdb.env"
    root_credentials.parent.mkdir(parents=True)
    root_credentials.write_text("# intentionally empty\n", encoding="utf-8")
    legacy_credentials = media / "New" / "_config" / "tmdb.env"
    legacy_credentials.parent.mkdir(parents=True)
    legacy_credentials.write_text("TMDB_API_KEY=legacy-valid-key\n", encoding="utf-8")

    settings = load_settings()

    assert settings.tmdb_api_key == "legacy-valid-key"
    assert settings.tmdb_credentials_file == legacy_credentials.resolve()


def test_ffprobe_and_footer_defaults(monkeypatch, tmp_path: Path):
    _base_env(monkeypatch, tmp_path)
    for name in (
        "FFPROBE_ENABLED",
        "FFPROBE_PATH",
        "FFPROBE_TIMEOUT_SECONDS",
        "LIBRARY_PROBE_CONCURRENCY",
        "LIBRARY_PROBE_AUTO_START",
        "AUTHOR_NAME",
        "SUPPORT_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = load_settings()

    assert settings.app_version == "0.4.0"
    assert settings.ffprobe_enabled is True
    assert settings.ffprobe_path == "ffprobe"
    assert settings.ffprobe_timeout_seconds == 90
    assert settings.library_probe_concurrency == 2
    assert settings.library_probe_auto_start is False
    assert settings.author_name == "Lrd.Tiberius"
    assert settings.support_url == "https://www.paypal.com/paypalme/SebastianM207"


def test_branding_defaults_include_author_and_paypal_support_link(monkeypatch, tmp_path: Path):
    _base_env(monkeypatch, tmp_path)

    settings = load_settings()

    assert settings.app_version == "0.4.0"
    assert settings.author_name == "Lrd.Tiberius"
    assert settings.support_url == "https://www.paypal.com/paypalme/SebastianM207"
    assert settings.ffprobe_enabled is True


def test_app_version_cannot_be_overridden_by_environment(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path / "media"))
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("APP_VERSION", "99.99.99")
    from app.config import load_settings

    assert load_settings().app_version == "0.4.0"
