from pathlib import Path

from app.path_filters import is_ignored_media_path


def test_appledouble_files_are_ignored():
    assert is_ignored_media_path(Path("/media/Filme/Test/._Movie.mkv"))
    assert is_ignored_media_path(Path("/media/Serien/Test/Season 1/._S01E01.mp4"))


def test_common_system_files_and_directories_are_ignored():
    assert is_ignored_media_path(Path("/media/.DS_Store"))
    assert is_ignored_media_path(Path("/media/.Spotlight-V100/index"))
    assert is_ignored_media_path(Path("/media/@eaDir/Movie.mkv"))
    assert is_ignored_media_path(Path("/media/Filme/Test/Thumbs.db"))


def test_real_media_files_are_not_ignored():
    assert not is_ignored_media_path(Path("/media/Filme/After Earth (2013)/After Earth (2013).mp4"))
    assert not is_ignored_media_path(Path("/media/Serien/Ranma ½ (1989)/Season 1/Ranma ½ - S01E01.mkv"))


def test_root_relative_hidden_paths_are_ignored():
    root = Path("/media/Filme")
    assert is_ignored_media_path(root / "Movie" / "._Movie.mkv", root=root)
    assert not is_ignored_media_path(root / "Movie" / "Movie.mkv", root=root)
