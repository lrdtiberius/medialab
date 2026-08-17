from __future__ import annotations

from fastapi.testclient import TestClient


def test_routes_render_branding_and_library(monkeypatch, tmp_path) -> None:
    media = tmp_path / "video"
    data = tmp_path / "data"
    monkeypatch.setenv("MEDIA_ROOT", str(media))
    monkeypatch.setenv("DATA_ROOT", str(data))
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("FFPROBE_ENABLED", "false")
    monkeypatch.delenv("WEB_USERNAME", raising=False)
    monkeypatch.delenv("WEB_PASSWORD", raising=False)

    movie = media / "Filme" / "Testfilm (2020)"
    movie.mkdir(parents=True)
    (media / "New").mkdir(parents=True)
    (media / "Serien").mkdir(parents=True)
    (media / "Animes").mkdir(parents=True)
    (movie / "movie.nfo").write_text(
        "<movie><title>Testfilm</title><year>2020</year></movie>",
        encoding="utf-8",
    )

    # Import after environment setup; create_app reads configuration eagerly.
    from app.main import create_app

    with TestClient(create_app()) as client:
        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert "by <strong>Lrd.Tiberius</strong>" in dashboard.text
        assert "https://www.paypal.com/paypalme/SebastianM207" in dashboard.text
        assert "Buy me a coffee" in dashboard.text
        assert "MediaLab" in dashboard.text
        assert ">Log<" in dashboard.text

        library = client.get("/library")
        assert library.status_code == 200
        assert "Technische Medienanalyse" in library.text
        assert "Testfilm" in library.text

        health = client.get("/health")
        assert health.status_code == 200
        payload = health.json()
        assert payload["version"] == "0.4.0"
        assert payload["build_id"]
        assert payload["features"]["library"] is True
        assert "ffprobe_available" in payload["checks"]
        assert payload["features"]["copy_completion_guard"] is True
        assert payload["features"]["probe_error_browser"] is True
        assert payload["features"]["log_view"] is True

        build_info = client.get("/build-info")
        assert build_info.status_code == 200
        assert build_info.json()["version"] == "0.4.0"
        assert build_info.json()["author"] == "Lrd.Tiberius"

        log_page = client.get("/log")
        assert log_page.status_code == 200
        assert "Kurzes Log" in log_page.text

        errors_page = client.get("/library/errors")
        assert errors_page.status_code == 200
        assert "Fehler der Technikanalyse" in errors_page.text


def test_dashboard_prunes_manually_renamed_inbox_entry(monkeypatch, tmp_path) -> None:
    media = tmp_path / "video-rename"
    data = tmp_path / "data-rename"
    monkeypatch.setenv("MEDIA_ROOT", str(media))
    monkeypatch.setenv("DATA_ROOT", str(data))
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("FFPROBE_ENABLED", "false")
    monkeypatch.delenv("WEB_USERNAME", raising=False)
    monkeypatch.delenv("WEB_PASSWORD", raising=False)

    source = media / "New" / "Alter Name (2020).mkv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"video")
    (media / "Filme").mkdir(parents=True)
    (media / "Serien").mkdir(parents=True)
    (media / "Animes").mkdir(parents=True)

    from app.main import create_app

    app = create_app()
    with TestClient(app) as client:
        app.state.scanner._discover_sync()
        old_job = app.state.database.get_job_by_path(source.resolve())
        assert old_job is not None
        app.state.database.update_job(old_job["id"], status="ignored")

        renamed = source.with_name("Neuer Name (2020).mkv")
        source.rename(renamed)
        dashboard = client.get("/")

        assert dashboard.status_code == 200
        assert "Alter Name (2020).mkv" not in dashboard.text
        assert app.state.database.get_job_by_path(source.resolve()) is None


def test_startup_prunes_stale_error_rows(monkeypatch, tmp_path) -> None:
    media = tmp_path / "video-startup-prune"
    data = tmp_path / "data-startup-prune"
    monkeypatch.setenv("MEDIA_ROOT", str(media))
    monkeypatch.setenv("DATA_ROOT", str(data))
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("FFPROBE_ENABLED", "false")
    monkeypatch.delenv("WEB_USERNAME", raising=False)
    monkeypatch.delenv("WEB_PASSWORD", raising=False)

    source = media / "New" / "Verschwundener Film (2020).mkv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"video")
    (media / "Filme").mkdir(parents=True)
    (media / "Serien").mkdir(parents=True)
    (media / "Animes").mkdir(parents=True)

    from app.config import load_settings
    from app.db import Database
    from app.parser import parse_media_filename

    settings = load_settings()
    database = Database(settings.database_path)
    database.initialize()
    stat = source.stat()
    job = database.upsert_discovered(
        source.resolve(), stat.st_size, stat.st_mtime, parse_media_filename(source), None
    )
    database.update_job(job["id"], status="error", message="Quelle fehlt")
    source.unlink()

    from app.main import create_app

    app = create_app()
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "Verschwundener Film" not in response.text
        assert app.state.database.get_job(job["id"]) is None
