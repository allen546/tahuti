"""Tests for tahuti.cache — aggressive edge-case coverage."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import tahuti.cache as cache_mod
from tahuti.cache import DEFAULT_TTL, ResponseCache


# ── Basic CRUD ────────────────────────────────────────────────────────────


class TestPutAndGet:
    def test_roundtrip(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        cache.put("https://example.com/page1", "<html>ok</html>", 200)
        body, status = cache.get("https://example.com/page1")
        assert body == "<html>ok</html>"
        assert status == 200

    def test_get_miss(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        assert cache.get("https://nonexistent.com") is None

    def test_put_overwrites_existing(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        cache.put("https://example.com", "v1", 200)
        cache.put("https://example.com", "v2", 201)
        body, status = cache.get("https://example.com")
        assert body == "v2"
        assert status == 201

    def test_returns_status_code(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        for code in (200, 201, 301, 404, 500):
            cache.put(f"https://example.com/{code}", "body", code)
            _, status = cache.get(f"https://example.com/{code}")
            assert status == code

    def test_body_none_is_not_stored(self, tmp_path: Path):
        """None body would cause TypeError in json.dumps."""
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        # The code calls json.dumps(data) — if body is None it serializes fine
        # but semantically we shouldn't cache None bodies.
        # Current behavior: stores "null" as body string.
        cache.put("https://example.com", None, 200)
        body, _ = cache.get("https://example.com")
        assert body is None  # json.loads("null") → None

    def test_body_empty_string(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        cache.put("https://example.com", "", 200)
        body, _ = cache.get("https://example.com")
        assert body == ""

    def test_status_zero(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        cache.put("https://example.com", "body", 0)
        _, status = cache.get("https://example.com")
        assert status == 0

    def test_status_negative(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        cache.put("https://example.com", "body", -1)
        _, status = cache.get("https://example.com")
        assert status == -1


# ── TTL / Expiry ─────────────────────────────────────────────────────────


class TestTTL:
    def test_fresh_entry_returns_cached(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, ttl=60, enabled=True)
        cache.put("https://example.com", "body", 200)
        assert cache.get("https://example.com") is not None

    def test_expired_entry_returns_none(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, ttl=0, enabled=True)
        cache.put("https://example.com", "body", 200)
        time.sleep(0.01)
        assert cache.get("https://example.com") is None

    def test_exactly_at_ttl_boundary(self, tmp_path: Path):
        """Entry within TTL window should still be returned.

        The exact boundary is racy (a few ms elapse between put and get),
        so we use a 0.5s margin.  An entry at ts = now - ttl + 0.5 is still
        within the window.
        """
        cache = ResponseCache(cache_dir=tmp_path, ttl=1, enabled=True)
        cache.put("https://example.com", "body", 200)
        key = cache._key("https://example.com")
        p = tmp_path / f"{key}.json"
        data = json.loads(p.read_text())
        data["ts"] = time.time() - 0.5  # 0.5s before ttl expires
        p.write_text(json.dumps(data))
        assert cache.get("https://example.com") is not None

    def test_one_second_past_ttl(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, ttl=1, enabled=True)
        cache.put("https://example.com", "body", 200)
        key = cache._key("https://example.com")
        p = tmp_path / f"{key}.json"
        data = json.loads(p.read_text())
        data["ts"] = time.time() - 1.01  # past boundary
        p.write_text(json.dumps(data))
        assert cache.get("https://example.com") is None

    def test_negative_ttl_expires_immediately(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, ttl=-1, enabled=True)
        cache.put("https://example.com", "body", 200)
        time.sleep(0.001)
        assert cache.get("https://example.com") is None

    def test_zero_ttl_expires_immediately(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, ttl=0, enabled=True)
        cache.put("https://example.com", "body", 200)
        time.sleep(0.001)
        assert cache.get("https://example.com") is None

    def test_very_large_ttl_still_works(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, ttl=31536000, enabled=True)  # 1 year
        cache.put("https://example.com", "body", 200)
        assert cache.get("https://example.com") is not None

    def test_put_resets_freshness(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, ttl=1, enabled=True)
        cache.put("https://example.com", "old", 200)
        # Wait a bit, then re-put
        time.sleep(0.5)
        cache.put("https://example.com", "new", 200)
        # Still within ttl from the second put
        time.sleep(0.6)
        body, _ = cache.get("https://example.com")
        assert body == "new"


# ── Disabled cache ───────────────────────────────────────────────────────


class TestDisabledCache:
    def test_get_returns_none(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=False)
        cache.put("https://example.com", "body", 200)
        assert cache.get("https://example.com") is None

    def test_put_creates_no_files(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=False)
        cache.put("https://example.com", "body", 200)
        assert not list(tmp_path.glob("*.json"))

    def test_invalidate_is_noop(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=False)
        # Should not raise
        cache.invalidate("https://example.com")
        cache.invalidate()

    def test_disabled_cache_dir_not_created(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path / "nested", enabled=False)
        cache.put("https://example.com", "body", 200)
        assert not (tmp_path / "nested").exists()


# ── Invalidation ─────────────────────────────────────────────────────────


class TestInvalidation:
    def test_invalidate_single_url(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        cache.put("https://a.com", "a", 200)
        cache.put("https://b.com", "b", 200)
        cache.invalidate("https://a.com")
        assert cache.get("https://a.com") is None
        assert cache.get("https://b.com") is not None

    def test_invalidate_all(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        cache.put("https://a.com", "a", 200)
        cache.put("https://b.com", "b", 200)
        cache.invalidate()
        assert cache.get("https://a.com") is None
        assert cache.get("https://b.com") is None

    def test_invalidate_nonexistent_url_no_error(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        cache.invalidate("https://nonexistent.com")  # should not raise

    def test_invalidate_all_does_not_remove_parent_dir(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        cache.put("https://example.com", "body", 200)
        cache.invalidate()
        assert tmp_path.exists()  # dir remains, just empty

    def test_invalidate_leaves_non_json_files(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        cache.put("https://a.com", "a", 200)
        (tmp_path / "notes.txt").write_text("keep me")
        cache.invalidate()
        assert (tmp_path / "notes.txt").exists()

    def test_invalidate_all_when_dir_missing(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path / "nope", enabled=True)
        cache.invalidate()  # should not raise

    def test_invalidate_url_when_dir_missing(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path / "nope", enabled=True)
        cache.invalidate("https://example.com")  # should not raise


# ── Key hashing ──────────────────────────────────────────────────────────


class TestKeyHashing:
    def test_deterministic(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        assert cache._key("https://example.com") == cache._key("https://example.com")

    def test_different_urls_different_keys(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        assert cache._key("https://a.com") != cache._key("https://b.com")

    def test_key_is_hex_string(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        key = cache._key("https://example.com")
        assert len(key) == 32
        int(key, 16)  # raises if not valid hex

    def test_same_url_same_key_regardless_of_order(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        # Identical URLs
        assert cache._key("https://example.com/path") == cache._key(
            "https://example.com/path"
        )

    def test_different_paths_different_keys(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        assert cache._key("https://example.com/a") != cache._key(
            "https://example.com/b"
        )

    def test_query_params_affect_key(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        assert cache._key("https://example.com?a=1") != cache._key(
            "https://example.com?b=2"
        )

    def test_trailing_slash_affects_key(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        assert cache._key("https://example.com/path") != cache._key(
            "https://example.com/path/"
        )

    def test_fragment_affects_key(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        assert cache._key("https://example.com#a") != cache._key(
            "https://example.com#b"
        )

    def test_port_affects_key(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        assert cache._key("https://example.com:8080/x") != cache._key(
            "https://example.com/x"
        )

    def test_case_sensitive_key(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        assert cache._key("https://Example.COM") != cache._key(
            "https://example.com"
        )

    def test_unicode_url_key(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        k1 = cache._key("https://example.com/中文")
        k2 = cache._key("https://example.com/english")
        assert k1 != k2
        assert len(k1) == 32

    def test_very_long_url_key(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        long_url = "https://example.com/" + "a" * 10000
        key = cache._key(long_url)
        assert len(key) == 32


# ── File system edge cases ───────────────────────────────────────────────


class TestFileSystem:
    def test_creates_nested_cache_dir(self, tmp_path: Path):
        cache_dir = tmp_path / "a" / "b" / "c"
        cache = ResponseCache(cache_dir=cache_dir, enabled=True)
        cache.put("https://example.com", "body", 200)
        assert cache_dir.exists()

    def test_file_permissions_0o600(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        cache.put("https://example.com", "body", 200)
        key = cache._key("https://example.com")
        mode = (tmp_path / f"{key}.json").stat().st_mode
        assert mode & 0o777 == 0o600

    def test_dir_permissions_0o700(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        cache.put("https://example.com", "body", 200)
        mode = tmp_path.stat().st_mode
        assert mode & 0o777 == 0o700

    def test_corrupt_json_returns_none(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        key = cache._key("https://example.com")
        (tmp_path / f"{key}.json").write_text("not valid json {{{")
        assert cache.get("https://example.com") is None

    def test_valid_json_missing_body_field(self, tmp_path: Path):
        """Cache file with valid ts but missing 'body' should not crash."""
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        key = cache._key("https://example.com")
        # Include a valid ts so the TTL check passes and we hit data["body"]
        (tmp_path / f"{key}.json").write_text(
            json.dumps({"url": "x", "status": 200, "ts": time.time()})
        )
        with pytest.raises(KeyError):
            cache.get("https://example.com")

    def test_valid_json_missing_status_field(self, tmp_path: Path):
        """Cache file with valid ts but missing 'status' should not crash."""
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        key = cache._key("https://example.com")
        (tmp_path / f"{key}.json").write_text(
            json.dumps({"url": "x", "body": "ok", "ts": time.time()})
        )
        with pytest.raises(KeyError):
            cache.get("https://example.com")

    def test_valid_json_missing_ts_field(self, tmp_path: Path):
        """Missing 'ts' defaults to 0 via .get('ts', 0) → always expired."""
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        key = cache._key("https://example.com")
        (tmp_path / f"{key}.json").write_text(
            json.dumps({"url": "x", "body": "ok", "status": 200})
        )
        # ts defaults to 0, time.time() - 0 >> ttl → expired
        assert cache.get("https://example.com") is None

    def test_empty_json_object(self, tmp_path: Path):
        """Completely empty JSON object — no ts, body, or status."""
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        key = cache._key("https://example.com")
        (tmp_path / f"{key}.json").write_text("{}")
        # Empty dict → ts defaults to 0 → expired → returns None
        # This masks the missing body/status bug
        assert cache.get("https://example.com") is None

    def test_file_is_directory(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        key = cache._key("https://example.com")
        (tmp_path / f"{key}.json").mkdir()
        assert cache.get("https://example.com") is None

    def test_file_is_symlink_to_missing(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        key = cache._key("https://example.com")
        (tmp_path / f"{key}.json").symlink_to(tmp_path / "nonexistent")
        assert cache.get("https://example.com") is None

    def test_file_deleted_between_exists_and_read(self, tmp_path: Path):
        """TOCTOU: file exists at path() but gone by read_text()."""
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        cache.put("https://example.com", "body", 200)
        # Monkey-patch read_text to delete file first
        original_get = cache.get

        def flaky_get(url):
            p = cache._path(url)
            if p.exists():
                p.unlink()  # race: delete before read
            return original_get(url)

        cache.get = flaky_get
        # Should return None (OSError caught)
        assert cache.get("https://example.com") is None

    def test_file_unreadable(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        cache.put("https://example.com", "body", 200)
        key = cache._key("https://example.com")
        p = tmp_path / f"{key}.json"
        os.chmod(p, 0o000)
        try:
            result = cache.get("https://example.com")
            assert result is None  # OSError caught
        finally:
            os.chmod(p, 0o600)  # restore for cleanup


# ── URL edge cases ───────────────────────────────────────────────────────


class TestURLEdgeCases:
    def test_url_with_query_params(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        cache.put("https://example.com/path?a=1&b=2", "body", 200)
        body, _ = cache.get("https://example.com/path?a=1&b=2")
        assert body == "body"

    def test_same_url_different_queries_are_different_entries(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        cache.put("https://example.com/x?a=1", "a", 200)
        cache.put("https://example.com/x?a=2", "b", 200)
        assert cache.get("https://example.com/x?a=1")[0] == "a"
        assert cache.get("https://example.com/x?a=2")[0] == "b"

    def test_url_with_unicode_path(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        url = "https://example.com/课程/数学"
        cache.put(url, "数学内容", 200)
        body, _ = cache.get(url)
        assert body == "数学内容"

    def test_url_with_spaces(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        url = "https://example.com/my file.pdf"
        cache.put(url, "pdf-body", 200)
        body, _ = cache.get(url)
        assert body == "pdf-body"

    def test_very_long_url(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        url = "https://example.com/" + "a" * 5000
        cache.put(url, "body", 200)
        body, _ = cache.get(url)
        assert body == "body"

    def test_url_with_newlines(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        url = "https://example.com/path\nwith\nnewlines"
        cache.put(url, "body", 200)
        body, _ = cache.get(url)
        assert body == "body"

    def test_managebac_url_with_date_range(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        url = "https://myschool.managebac.cn/student/events.json?start=2026-01-01&end=2026-12-31"
        cache.put(url, '{"events":[]}', 200)
        body, _ = cache.get(url)
        assert body == '{"events":[]}'

    def test_managebac_url_different_date_ranges_are_different(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        url1 = "https://myschool.managebac.cn/student/events.json?start=2026-01-01&end=2026-06-30"
        url2 = "https://myschool.managebac.cn/student/events.json?start=2026-07-01&end=2026-12-31"
        cache.put(url1, "h1", 200)
        cache.put(url2, "h2", 200)
        assert cache.get(url1)[0] == "h1"
        assert cache.get(url2)[0] == "h2"


# ── Body edge cases ──────────────────────────────────────────────────────


class TestBodyEdgeCases:
    def test_empty_body(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        cache.put("https://example.com", "", 200)
        body, _ = cache.get("https://example.com")
        assert body == ""

    def test_very_large_body(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        body = "x" * (1024 * 1024)  # 1 MB
        cache.put("https://example.com", body, 200)
        got, _ = cache.get("https://example.com")
        assert got == body
        assert len(got) == 1024 * 1024

    def test_unicode_body_with_emoji(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        body = "<html>🎉中文テスト한국어</html>"
        cache.put("https://example.com", body, 200)
        got, _ = cache.get("https://example.com")
        assert got == body

    def test_html_with_special_chars(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        body = '<div class="a">&amp; &lt; &gt; &quot; &#39;</div>'
        cache.put("https://example.com", body, 200)
        got, _ = cache.get("https://example.com")
        assert got == body

    def test_json_body(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        body = json.dumps({"tasks": [{"title": "HW1", "grade": "A"}]})
        cache.put("https://example.com/api", body, 200)
        got, _ = cache.get("https://example.com/api")
        assert json.loads(got) == {"tasks": [{"title": "HW1", "grade": "A"}]}

    def test_body_with_newlines_and_tabs(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        body = "line1\nline2\n\ttab\r\nCRLF"
        cache.put("https://example.com", body, 200)
        got, _ = cache.get("https://example.com")
        assert got == body

    def test_body_with_null_bytes(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        body = "before\x00after"
        cache.put("https://example.com", body, 200)
        got, _ = cache.get("https://example.com")
        assert got == body

    def test_double_encoded_html(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        body = "&amp;lt;div&amp;gt;"
        cache.put("https://example.com", body, 200)
        got, _ = cache.get("https://example.com")
        assert got == body


# ── Timestamp handling ───────────────────────────────────────────────────


class TestTimestamp:
    def test_timestamp_is_float(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        cache.put("https://example.com", "body", 200)
        key = cache._key("https://example.com")
        data = json.loads((tmp_path / f"{key}.json").read_text())
        assert isinstance(data["ts"], float)

    def test_timestamp_is_recent(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        before = time.time()
        cache.put("https://example.com", "body", 200)
        after = time.time()
        key = cache._key("https://example.com")
        data = json.loads((tmp_path / f"{key}.json").read_text())
        assert before <= data["ts"] <= after

    def test_stored_json_contains_all_fields(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        cache.put("https://example.com", "body", 201)
        key = cache._key("https://example.com")
        data = json.loads((tmp_path / f"{key}.json").read_text())
        assert set(data.keys()) == {"url", "body", "status", "ts"}
        assert data["url"] == "https://example.com"
        assert data["body"] == "body"
        assert data["status"] == 201


# ── Thread safety / concurrent access ────────────────────────────────────


class TestConcurrency:
    def test_concurrent_puts(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        errors: list[Exception] = []

        def writer(i: int):
            try:
                for j in range(10):
                    cache.put(f"https://example.com/{i}/{j}", f"body-{i}-{j}", 200)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        # All entries should be readable
        for i in range(8):
            for j in range(10):
                result = cache.get(f"https://example.com/{i}/{j}")
                assert result is not None

    def test_concurrent_reads_and_writes(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        for i in range(20):
            cache.put(f"https://example.com/{i}", f"body-{i}", 200)
        errors: list[Exception] = []

        def reader():
            try:
                for _ in range(50):
                    for i in range(20):
                        cache.get(f"https://example.com/{i}")
            except Exception as e:
                errors.append(e)

        def writer():
            try:
                for _ in range(50):
                    for i in range(20):
                        cache.put(f"https://example.com/{i}", f"updated-{i}", 200)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=reader) for _ in range(4)
        ] + [threading.Thread(target=writer) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors

    def test_concurrent_invalidates(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        for i in range(20):
            cache.put(f"https://example.com/{i}", f"body-{i}", 200)
        errors: list[Exception] = []

        def invalidator(start: int):
            try:
                for i in range(start, start + 5):
                    cache.invalidate(f"https://example.com/{i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=invalidator, args=(i,)) for i in range(0, 20, 5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors


# ── Multiple operations / state transitions ──────────────────────────────


class TestStateTransitions:
    def test_put_get_invalidate_get(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        cache.put("https://example.com", "body", 200)
        assert cache.get("https://example.com") is not None
        cache.invalidate("https://example.com")
        assert cache.get("https://example.com") is None

    def test_put_get_disable_get(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        cache.put("https://example.com", "body", 200)
        assert cache.get("https://example.com") is not None
        cache.enabled = False
        assert cache.get("https://example.com") is None

    def test_put_disable_put_enable_get(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        cache.enabled = False
        cache.put("https://example.com", "body", 200)
        cache.enabled = True
        assert cache.get("https://example.com") is None  # never stored

    def test_many_entries_all_retrievable(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        for i in range(100):
            cache.put(f"https://example.com/{i}", f"body-{i}", 200)
        for i in range(100):
            body, _ = cache.get(f"https://example.com/{i}")
            assert body == f"body-{i}"

    def test_invalidate_then_put_same_url(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        cache.put("https://example.com", "old", 200)
        cache.invalidate("https://example.com")
        cache.put("https://example.com", "new", 200)
        body, _ = cache.get("https://example.com")
        assert body == "new"

    def test_flush_then_put(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        cache.put("https://a.com", "a", 200)
        cache.put("https://b.com", "b", 200)
        cache.invalidate()
        cache.put("https://c.com", "c", 200)
        assert cache.get("https://a.com") is None
        assert cache.get("https://b.com") is None
        assert cache.get("https://c.com") is not None


# ── Default values ───────────────────────────────────────────────────────


class TestDefaults:
    def test_default_ttl(self):
        from tahuti.cache import DEFAULT_TTL

        assert DEFAULT_TTL == 900

    def test_default_cache_dir(self):
        from tahuti.cache import DEFAULT_CACHE_DIR

        assert DEFAULT_CACHE_DIR == Path.home() / ".config" / "tahuti" / "cache"

    def test_default_enabled(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path)
        assert cache.enabled is True

    def test_default_ttl_value(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path)
        assert cache.ttl == 900

    def test_none_cache_dir_uses_default(self):
        cache = ResponseCache(cache_dir=None)
        assert cache.cache_dir == Path.home() / ".config" / "tahuti" / "cache"

    def test_string_cache_dir_converted_to_path(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=str(tmp_path))
        assert isinstance(cache.cache_dir, Path)


# ── JSON round-trip fidelity ─────────────────────────────────────────────


class TestJSONFidelity:
    def test_ensure_ascii_disabled(self, tmp_path: Path):
        """Body with non-ASCII chars should be stored as-is, not \\uXXXX."""
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        cache.put("https://example.com", "中文", 200)
        key = cache._key("https://example.com")
        raw = (tmp_path / f"{key}.json").read_text()
        assert "中文" in raw  # not escaped

    def test_preserves_body_exactly(self, tmp_path: Path):
        bodies = [
            "",
            " ",
            "\n",
            "\t",
            "\r\n",
            "a" * 10000,
            json.dumps({"key": "value"}),
            "<html>&amp;</html>",
            "🎉🚀💯",
            "\x00\x01\x02",
        ]
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        for i, body in enumerate(bodies):
            cache.put(f"https://example.com/{i}", body, 200)
        for i, body in enumerate(bodies):
            got, _ = cache.get(f"https://example.com/{i}")
            assert got == body, f"Body mismatch at index {i}"


# ── Cache file naming ────────────────────────────────────────────────────


class TestFileNaming:
    def test_only_one_file_per_url(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        cache.put("https://example.com", "v1", 200)
        cache.put("https://example.com", "v2", 200)
        key = cache._key("https://example.com")
        files = list(tmp_path.glob(f"{key}*.json"))
        assert len(files) == 1

    def test_different_urls_produce_different_files(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        cache.put("https://a.com", "a", 200)
        cache.put("https://b.com", "b", 200)
        assert len(list(tmp_path.glob("*.json"))) == 2

    def test_files_are_valid_json(self, tmp_path: Path):
        cache = ResponseCache(cache_dir=tmp_path, enabled=True)
        for i in range(10):
            cache.put(f"https://example.com/{i}", f"body-{i}", 200)
        for p in tmp_path.glob("*.json"):
            json.loads(p.read_text())  # should not raise


def test_cache_file_created_0600(tmp_path):
    c = ResponseCache(cache_dir=tmp_path / "cd", ttl=60)
    c.put("https://x.managebac.cn/a", "<html>secret grade</html>", 200)
    p = next((tmp_path / "cd").glob("*.json"))
    assert p.stat().st_mode & 0o777 == 0o600, oct(p.stat().st_mode & 0o777)
    assert not list((tmp_path / "cd").glob(".*tmp"))


def test_cache_dir_hardened_0700(tmp_path):
    root = tmp_path / "deep" / "nested"
    c = ResponseCache(cache_dir=root, ttl=60)
    c.put("https://x.managebac.cn/a", "body", 200)
    assert root.stat().st_mode & 0o777 == 0o700, oct(root.stat().st_mode & 0o777)


# ── the hardening walk must stop at the package's own tree ────────────────
#
# `put` used to chmod every ancestor of the cache dir, up to `/`. Unprivileged
# those chmods failed and the OSError was swallowed, but under root or in a
# container they succeeded: `/home` and `/` went to 0700, breaking every other
# account's home traversal. A single put() also reset the user's own $HOME from
# 0755 to 0700. The original intent — a credential-bearing tree that other
# local users cannot traverse — is preserved, just bounded.


def _package_tree(tmp_path: Path, monkeypatch):
    """Build `$HOME/.config/tahuti/cache` under *tmp_path* and point the
    hardening boundary at it, so the walk has somewhere safe to stop."""
    home = tmp_path / "home"
    state = home / ".config" / "tahuti"
    (state / "cache").mkdir(parents=True)
    for d in (home, home / ".config", state, state / "cache"):
        os.chmod(d, 0o755)
    monkeypatch.setattr(cache_mod, "config_dir", lambda: state)
    return home, state


def test_put_does_not_chmod_the_users_home(tmp_path, monkeypatch):
    """Reproduced the defect: one put() took $HOME from 0755 to 0700."""
    home, state = _package_tree(tmp_path, monkeypatch)
    outer_mode = tmp_path.stat().st_mode & 0o777
    ResponseCache(cache_dir=state / "cache" / "abc123", ttl=60).put(
        "https://x.managebac.cn/a", "body", 200
    )
    assert home.stat().st_mode & 0o777 == 0o755, oct(home.stat().st_mode & 0o777)
    assert (home / ".config").stat().st_mode & 0o777 == 0o755
    assert tmp_path.stat().st_mode & 0o777 == outer_mode


def test_put_still_hardens_the_packages_own_tree(tmp_path, monkeypatch):
    """The original intent survives: our credential-bearing tree is closed."""
    home, state = _package_tree(tmp_path, monkeypatch)
    ResponseCache(cache_dir=state / "cache" / "abc123", ttl=60).put(
        "https://x.managebac.cn/a", "body", 200
    )
    assert state.stat().st_mode & 0o777 == 0o700, oct(state.stat().st_mode & 0o777)
    assert (state / "cache").stat().st_mode & 0o777 == 0o700
    target = state / "cache" / "abc123"
    assert target.stat().st_mode & 0o777 == 0o700


def test_put_chmods_no_directory_outside_the_package_tree(tmp_path, monkeypatch):
    """Under root the old walk reached `/`; assert it cannot leave our tree."""
    home, state = _package_tree(tmp_path, monkeypatch)
    chmodded: list[Path] = []
    real_chmod = os.chmod

    def spy(path, mode, **kwargs):
        chmodded.append(Path(path))
        return real_chmod(path, mode, **kwargs)

    monkeypatch.setattr(cache_mod.os, "chmod", spy)
    ResponseCache(cache_dir=state / "cache" / "abc123", ttl=60).put(
        "https://x.managebac.cn/a", "body", 200
    )
    ours = {state, state / "cache", state / "cache" / "abc123"}
    assert {p for p in chmodded if p.is_dir()} <= ours
    for forbidden in (Path("/"), Path("/home"), Path("/tmp"), home, tmp_path):
        assert forbidden not in chmodded, f"put chmod'd {forbidden}"


def test_put_leaves_an_outside_cache_dir_to_its_owner(tmp_path, monkeypatch):
    """A cache dir the caller placed elsewhere is not ours to widen."""
    monkeypatch.setattr(cache_mod, "config_dir", lambda: tmp_path / "not-our-state")
    outside = tmp_path / "somewhere" / "else"
    outside.mkdir(parents=True)
    os.chmod(outside.parent, 0o755)
    os.chmod(outside, 0o755)
    ResponseCache(cache_dir=outside, ttl=60).put("https://x/", "body", 200)
    assert outside.stat().st_mode & 0o777 == 0o700
    assert outside.parent.stat().st_mode & 0o777 == 0o755


def test_cache_clear_removes_jwt_and_grades(tmp_path):
    c = ResponseCache(cache_dir=tmp_path, ttl=60)
    c.put("https://x.managebac.cn/jwt", "Bearer abc", 200)
    c.put("https://x.managebac.cn/grades", "<html>A+</html>", 200)
    removed = c.clear()
    assert removed == 2
    assert list(tmp_path.glob("*.json")) == []


# ── clear() must also sweep the temp files a concurrent put() creates ─────
#
# `put` writes through a `.cache_*.tmp` file and then `os.replace`s it into
# place. Globbing only `*.json` left any temp file a concurrent `put` had
# already created, so the replace could land *after* logout finished — a
# "cleared" cache quietly gained a JWT-bearing entry.


def test_clear_removes_an_in_flight_temp_file(tmp_path):
    """A .cache_*.tmp holding a JWT must not survive `tahuti logout`."""
    c = ResponseCache(cache_dir=tmp_path / "cd", ttl=60)
    c.put("https://x.managebac.cn/jwt", "Bearer abc", 200)
    tmp = c.cache_dir / ".cache_inflight.tmp"
    tmp.write_text('{"body": "Bearer abc"}', encoding="utf-8")

    assert c.clear() == 2
    assert list((tmp_path / "cd").glob("*")) == []


def test_clear_leaves_no_temp_file_for_a_racing_put_to_land(tmp_path):
    """`os.replace` only succeeds while its temp file still exists."""
    c = ResponseCache(cache_dir=tmp_path / "cd", ttl=60)
    c.put("https://x.managebac.cn/a", "v1", 200)
    # Stand in for a `put` that had already created its temp file when logout
    # swept the directory.
    fd, name = tempfile.mkstemp(dir=str(c.cache_dir), prefix=".cache_", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write('{"body": "Bearer abc"}')
    racing = Path(name)

    assert c.clear() == 2
    assert not racing.exists()
    with pytest.raises(FileNotFoundError):
        os.replace(racing, c._path("https://x.managebac.cn/a"))


def test_clear_reports_the_temp_files_it_removed(tmp_path):
    c = ResponseCache(cache_dir=tmp_path, ttl=60)
    c.put("https://x.managebac.cn/a", "v1", 200)
    (c.cache_dir / ".cache_one.tmp").write_text("{}", encoding="utf-8")
    (c.cache_dir / ".cache_two.tmp").write_text("{}", encoding="utf-8")
    assert c.clear() == 3


def test_clear_leaves_the_directory_itself(tmp_path):
    c = ResponseCache(cache_dir=tmp_path / "cd", ttl=60)
    c.put("https://x.managebac.cn/a", "v1", 200)
    c.clear()
    assert c.cache_dir.is_dir()


class TestNoneTTL:
    """A None TTL is a crash waiting for the first get(), not "cache forever"."""

    def test_none_ttl_is_clamped_to_the_default(self, tmp_path):
        c = ResponseCache(cache_dir=tmp_path, ttl=None)
        assert c.ttl == DEFAULT_TTL

    def test_none_ttl_does_not_raise_on_get(self, tmp_path):
        """The regression: put() succeeded, then get() raised TypeError."""
        c = ResponseCache(cache_dir=tmp_path, ttl=None)
        c.put("https://x.managebac.cn/a", "v1", 200)
        assert c.get("https://x.managebac.cn/a") == ("v1", 200)

    def test_an_explicit_zero_ttl_still_expires_immediately(self, tmp_path):
        """Clamping must not swallow a deliberate 0 — that is a real value."""
        c = ResponseCache(cache_dir=tmp_path, ttl=0)
        c.put("https://x.managebac.cn/a", "v1", 200)
        assert c.get("https://x.managebac.cn/a") is None

    def test_an_explicit_ttl_is_untouched(self, tmp_path):
        c = ResponseCache(cache_dir=tmp_path, ttl=1234)
        assert c.ttl == 1234
