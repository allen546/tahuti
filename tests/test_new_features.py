import json
from datetime import datetime
from unittest.mock import MagicMock, patch
from tahuti.client import parse_due_date, ManageBacClient
from tahuti.cache import ResponseCache


def test_parse_due_date_wrapping():
    # Mock current datetime to Dec 28, 2026
    fixed_now = datetime(2026, 12, 28, 12, 0, 0)
    with patch("tahuti.client.datetime") as mock_datetime:
        mock_datetime.now.return_value = fixed_now
        mock_datetime.strptime = datetime.strptime

        # Naive parse of "Jan 4" would yield Jan 4, 2026.
        # But Jan 4 is in the future relative to Dec 28, 2026.
        # It should adjust to Jan 4, 2027.
        dt = parse_due_date("Jan 4, 9:30 PM")
        assert dt is not None
        assert dt.year == 2027
        assert dt.month == 1
        assert dt.day == 4
        assert dt.hour == 21
        assert dt.minute == 30

    # Mock current datetime to Jan 4, 2027
    fixed_now = datetime(2027, 1, 4, 12, 0, 0)
    with patch("tahuti.client.datetime") as mock_datetime:
        mock_datetime.now.return_value = fixed_now
        mock_datetime.strptime = datetime.strptime

        # Naive parse of "Dec 28" would yield Dec 28, 2027.
        # But Dec 28 is in the past relative to Jan 4, 2027.
        # It should adjust to Dec 28, 2026.
        dt = parse_due_date("Dec 28, 6:00 PM")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 12
        assert dt.day == 28
        assert dt.hour == 18
        assert dt.minute == 0


def test_stale_cache_fallback(tmp_path):
    # Setup cache
    cache = ResponseCache(cache_dir=tmp_path, enabled=True)
    cache.put("https://myschool.managebac.cn/test-fallback", "old cached body", 200)
    cache.invalidate("https://myschool.managebac.cn/test-fallback")

    # The client
    client = ManageBacClient("myschool", domain="managebac.cn", cache=cache)

    # Mock the request call to raise a 404 HTTPError (as if deleted by MB)
    import requests
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.raise_for_status.side_effect = requests.HTTPError("404 Not Found", response=mock_resp)

    with patch.object(client.session, "request", return_value=mock_resp):
        # Even though request failed with 404, it should fall back to the invalidated/stale cache!
        soup = client._get("/test-fallback")
        assert soup.get_text() == "old cached body"


def test_view_submissions():
    from tahuti.formatters import render_pretty
    from tahuti.formatters import ok

    payload = ok(
        "view",
        "default",
        {
            "task": {
                "id": "123",
                "title": "Submissions Test Task",
                "class_name": "Math HL",
                "due_date": "May 10",
                "link": "http://x",
            },
            "detail": {
                "description": "Do homework 5",
                "submission": "Submitted: 2 files",
                "attachments": [
                    {
                        "name": "submitted_essay.pdf",
                        "url": "http://x/submitted_essay.pdf",
                        "source": "submission",
                    },
                    {
                        "name": "resource_guide.pdf",
                        "url": "http://x/resource_guide.pdf",
                        "source": "description",
                    }
                ],
            },
        },
    )
    output = render_pretty(payload)
    assert "[submissions]" in output
    assert "Submitted: 2 files" in output
    assert "submitted_essay.pdf" in output
    assert "[attachments]" in output
    assert "resource_guide.pdf" in output

