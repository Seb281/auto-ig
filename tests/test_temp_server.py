"""Tests for publisher/temp_server.py — temporary HTTP file server."""

import os
import urllib.request

import pytest

from publisher.temp_server import TempImageServer


def _create_test_file(path, content=b"fake image data"):
    with open(path, "wb") as f:
        f.write(content)
    return path


class TestTempImageServerSingleFile:
    def test_serves_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUBLIC_IP", "127.0.0.1")
        file_path = _create_test_file(str(tmp_path / "test.jpg"))

        with TempImageServer(file_path, 19876) as url:
            assert "test.jpg" in url
            response = urllib.request.urlopen(url)
            data = response.read()
            assert data == b"fake image data"

    def test_stops_cleanly(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUBLIC_IP", "127.0.0.1")
        file_path = _create_test_file(str(tmp_path / "test.jpg"))

        server = TempImageServer(file_path, 19877)
        server.__enter__()
        server.__exit__(None, None, None)
        assert server._server is None


class TestTempImageServerMultipleFiles:
    def test_get_url_returns_correct_urls(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUBLIC_IP", "127.0.0.1")
        f1 = _create_test_file(str(tmp_path / "a.jpg"))
        f2 = _create_test_file(str(tmp_path / "b.jpg"))

        server = TempImageServer([f1, f2], 19878)
        with server:
            url_a = server.get_url(f1)
            url_b = server.get_url(f2)
            assert "a.jpg" in url_a
            assert "b.jpg" in url_b

    def test_unknown_file_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PUBLIC_IP", "127.0.0.1")
        f1 = _create_test_file(str(tmp_path / "a.jpg"))

        server = TempImageServer(f1, 19879)
        with pytest.raises(ValueError, match="not being served"):
            server.get_url("/nonexistent/z.jpg")


class TestTempImageServerValidation:
    def test_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            TempImageServer("/nonexistent/file.jpg", 19880)

    def test_different_directories_raises(self, tmp_path):
        dir1 = tmp_path / "dir1"
        dir2 = tmp_path / "dir2"
        dir1.mkdir()
        dir2.mkdir()
        f1 = _create_test_file(str(dir1 / "a.jpg"))
        f2 = _create_test_file(str(dir2 / "b.jpg"))

        with pytest.raises(ValueError, match="same directory"):
            TempImageServer([f1, f2], 19881)

    def test_empty_list_raises(self):
        with pytest.raises(ValueError, match="At least one"):
            TempImageServer([], 19882)
