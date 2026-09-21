"""Domain shape checks, redirect following, and hub endpoint validation.

The hardcoded ALLOWED_DOMAINS allowlist and _assert_same_host are removed.
Base domain validation enforces bare hostname syntax (_DOMAIN_RE), and redirects
to off-host HTTPS destinations are safely followed.
"""

from __future__ import annotations

import pytest
import requests_mock as rm

from tahuti.client import ManageBacClient, _validate_school_domain
from tahuti.exceptions import CommandError
from tahuti.notifications import HUB_ENDPOINTS, hub_for_domain

SCHOOL = "myschool"


@pytest.fixture()
def client(tmp_path):
    from tahuti.cache import ResponseCache

    return ManageBacClient(
        SCHOOL,
        domain="managebac.cn",
        cache=ResponseCache(cache_dir=tmp_path / "cache", enabled=False),
        retry=0,
        request_delay=0.0,
    )


# ── Domain shape check ───────────────────────────────────────────────────


class TestDomainIsAShapeCheck:
    @pytest.mark.parametrize(
        "domain",
        [
            "managebac.com",
            "managebac.cn",
            "managebac.sg",
            "managebac.co.jp",
            "managebac.de",
            "school.example.org",
            "mb.internal",
        ],
    )
    def test_a_well_formed_hostname_is_accepted(self, domain):
        assert _validate_school_domain("myschool", domain) == ("myschool", domain)

    def test_an_unlisted_domain_builds_a_working_base_url(self):
        client = ManageBacClient("myschool", domain="managebac.sg", retry=0)
        assert client.base == "https://myschool.managebac.sg"
        assert client.domain == "managebac.sg"

    def test_a_school_spelling_the_whole_domain_is_still_stripped(self):
        assert _validate_school_domain(
            "myschool.managebac.sg", "managebac.sg"
        ) == ("myschool", "managebac.sg")

    @pytest.mark.parametrize(
        "domain",
        [
            "evil.com/x",
            "https://evil.com",
            "evil.com:8080/path",
            "user@evil.com",
            ".managebac.com",
            "managebac.com.",
            "managebac..com",
            "managebac.com/path?q=1",
            "managebac.com ",
            "-managebac.com",
            "managebac.com-",
            "",
            None,
        ],
    )
    def test_a_domain_that_cannot_be_a_bare_hostname_is_rejected(self, domain):
        with pytest.raises(CommandError) as excinfo:
            _validate_school_domain("myschool", domain)
        assert excinfo.value.code == "invalid_domain"

    def test_the_error_message_names_the_domain_and_the_shape_expected(self):
        with pytest.raises(CommandError) as excinfo:
            _validate_school_domain("myschool", "https://evil.com")
        message = excinfo.value.message
        assert "https://evil.com" in message
        assert "Unsupported" not in message

    def test_the_school_subdomain_checks_are_untouched(self):
        for bad in ("evil.com/x", "", "a b", "a/b", "-x"):
            with pytest.raises(CommandError) as excinfo:
                _validate_school_domain(bad, "managebac.cn")
            assert excinfo.value.code == "invalid_school"


# ── Allowlist and host guard symbols are gone ────────────────────────────


class TestTheAllowlistSymbolIsGone:
    def test_allowed_domains_no_longer_exists(self):
        import tahuti.client as client_module

        assert not hasattr(client_module, "ALLOWED_DOMAINS")

    def test_the_cross_host_guard_no_longer_exists(self):
        client = ManageBacClient("myschool", domain="managebac.cn", retry=0)
        assert not hasattr(client, "_assert_same_host")

    def test_no_source_file_mentions_the_allowlist(self):
        from pathlib import Path

        src_root = Path(__file__).resolve().parent.parent / "src"
        hits = []
        for path in sorted(src_root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(errors="replace")
            if "ALLOWED_DOMAINS" in text:
                hits.append(str(path.relative_to(src_root)))
        assert hits == [], hits


# ── Redirect guards ──────────────────────────────────────────────────────


class TestRedirectGuardsAfterTheRemoval:
    def test_a_plaintext_redirect_is_still_refused(self):
        client = ManageBacClient("myschool", domain="managebac.cn", retry=0)
        with rm.Mocker() as m:
            m.get(
                f"{client.base}/student/dashboard",
                status_code=302,
                headers={"Location": "http://myschool.managebac.cn/dashboard"},
            )
            m.get("http://myschool.managebac.cn/dashboard", text="downgraded")
            with pytest.raises(CommandError) as excinfo:
                client._get("/student/dashboard", bypass_cache=True)
        assert excinfo.value.code == "insecure_redirect_blocked"

    def test_a_cross_host_redirect_is_now_followed(self):
        client = ManageBacClient("myschool", domain="managebac.cn", retry=0)
        with rm.Mocker() as m:
            m.get(
                f"{client.base}/student/dashboard",
                status_code=302,
                headers={"Location": "https://cdn.example.test/collect"},
            )
            m.get("https://cdn.example.test/collect", text="followed")
            soup = client._get("/student/dashboard", bypass_cache=True)
        assert "followed" in soup.get_text()

    def test_the_redirect_chain_is_still_bounded(self):
        client = ManageBacClient("myschool", domain="managebac.cn", retry=0)
        with rm.Mocker() as m:
            m.get(
                f"{client.base}/student/dashboard",
                status_code=302,
                headers={"Location": f"{client.base}/loop"},
            )
            m.get(
                f"{client.base}/loop",
                status_code=302,
                headers={"Location": f"{client.base}/loop"},
            )
            with pytest.raises(CommandError) as excinfo:
                client._get("/student/dashboard", bypass_cache=True)
        assert excinfo.value.code == "redirect_loop"


# ── iCal feed ────────────────────────────────────────────────────────────


class TestICalFeedSurvivesRedirects:
    def test_the_feed_is_fetched_from_the_shared_calendar_host(self, client):
        page = (
            '<html><body><a href="webcal://managebac.com/student/events/tok.ics">'
            "Subscribe</a></body></html>"
        )
        with rm.Mocker() as m:
            m.get(f"{client.base}/student/calendar", text=page)
            m.get(
                "https://managebac.com/student/events/tok.ics",
                text="BEGIN:VCALENDAR\nEND:VCALENDAR",
            )
            ical = client.get_ical_feed()
        assert "VCALENDAR" in ical

    def test_the_feed_survives_a_redirect_to_a_host_that_was_never_listed(self, client):
        page = (
            '<html><body><a href="webcal://cdn.example.test/tok.ics">'
            "Subscribe</a></body></html>"
        )
        with rm.Mocker() as m:
            m.get(f"{client.base}/student/calendar", text=page)
            m.get(
                "https://cdn.example.test/tok.ics",
                status_code=302,
                headers={"Location": "https://cdn.example.test/tok-real.ics"},
            )
            m.get(
                "https://cdn.example.test/tok-real.ics",
                text="BEGIN:VCALENDAR\nEND:VCALENDAR",
            )
            ical = client.get_ical_feed()
        assert "VCALENDAR" in ical


# ── Hub endpoint validation ──────────────────────────────────────────────


class TestHubEndpointValidation:
    def test_known_endpoints(self):
        assert hub_for_domain("managebac.com") == "https://mnn-hub.prod.faria.com"
        assert hub_for_domain("managebac.cn") == "https://mnn-hub.prod.faria.cn"

    def test_unknown_domain_defaults_to_com(self):
        assert hub_for_domain("managebac.sg") == "https://mnn-hub.prod.faria.com"
        assert hub_for_domain("school.example.org") == "https://mnn-hub.prod.faria.com"

    def test_normalised_domain_resolves(self):
        assert hub_for_domain("  ManageBac.CN  ") == "https://mnn-hub.prod.faria.cn"
        assert hub_for_domain("managebac.cn.") == "https://mnn-hub.prod.faria.cn"

    def test_scraped_faria_endpoint_is_used(self, client):
        with rm.Mocker() as m:
            m.get(
                f"{client.base}/student/notifications",
                text=(
                    '<a class="js-messages-and-notifications-trigger" '
                    'data-token="tok" '
                    'data-mnn-hub-endpoint="https://mnn-hub.prod.faria.cn"></a>'
                ),
            )
            endpoint, token = client.get_notification_token()
        assert endpoint == "https://mnn-hub.prod.faria.cn"

    def test_hostile_or_insecure_endpoint_falls_back(self, client):
        assert (
            client._validated_hub_endpoint("http://mnn-hub.prod.faria.cn")
            == "https://mnn-hub.prod.faria.cn"
        )
        assert (
            client._validated_hub_endpoint("https://user@evil.test")
            == "https://mnn-hub.prod.faria.cn"
        )
        assert (
            client._validated_hub_endpoint("https://evil.test/hub")
            == "https://mnn-hub.prod.faria.cn"
        )
