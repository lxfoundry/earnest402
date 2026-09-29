"""The built browser client, served under `/app/` by this same process.

The bundle's asset base is `/app/`, and a buyer's recovery handle is a deep
link to `/app/pool/<id>`, which exists only in the client's router -- so a
path with no file behind it must still answer with `index.html`. A missing
*asset* must not: a script tag handed an HTML page fails in a way that is far
harder to read than a 404.

None of it may reach the payment middleware. `/app/index` is a client page
whose last segment happens to spell a paid route, and it must be served as a
page rather than folded onto anything.
"""

import pytest

from api.config import ConfigError, verify_startup

INDEX_HTML = "<!doctype html><title>client under test</title>"
ASSET = "console.log('bundle')"
ASSET_PATH = "/app/assets/index-3f9a1c.js"
SECRET = "outside the bundle"


class FakeAlgod:
    def account_info(self, address):
        return {
            "amount": 1_000_000,
            "assets": [{"asset-id": 10458941, "amount": 0, "is-frozen": False}],
        }


@pytest.fixture
def bundle(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (dist / "assets" / "index-3f9a1c.js").write_text(ASSET, encoding="utf-8")
    (dist / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text(SECRET, encoding="utf-8")
    return dist


@pytest.fixture
def web(client_for, bundle):
    return client_for(web_dist_dir=str(bundle), index_agreement_id=0)


def _is_the_client_page(response) -> bool:
    return (
        response.status_code == 200
        and response.text == INDEX_HTML
        and response.headers["content-type"].startswith("text/html")
        and response.headers["cache-control"] == "no-cache"
    )


# --- redirects ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "location"),
    [
        ("/", "/app/"),
        ("/?pool=26&from=link", "/app/?pool=26&from=link"),
        ("/app", "/app/"),
        ("/app?pool=26&from=link", "/app/?pool=26&from=link"),
    ],
)
def test_the_root_and_the_bare_base_redirect_keeping_the_query(web, path, location):
    response = web.get(path, follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == location


# --- pages and assets -----------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/app/", "/app/pool/1", "/app/pool/26", "/app/index", "/app/index.html"]
)
def test_client_paths_are_served_the_client_page(web, facilitator, deps, path):
    response = web.get(path)

    assert _is_the_client_page(response), (response.status_code, response.text)
    assert "payment-required" not in response.headers
    assert facilitator.verified == []
    assert deps.escrow.created == 0


def test_a_hashed_asset_is_served_as_immutable(web):
    response = web.get(ASSET_PATH)

    assert response.status_code == 200
    assert response.text == ASSET
    cache = response.headers["cache-control"]
    assert "immutable" in cache
    assert "max-age=31536000" in cache


def test_a_page_reached_through_the_assets_directory_is_still_revalidated(web):
    """Immutability belongs to the file served, not to the spelling of the
    path that reached it: `index.html` cached for a year pins a buyer to
    asset hashes a later deploy no longer has."""
    response = web.get("/app/assets/%2e%2e/index.html")

    assert _is_the_client_page(response), (response.status_code, response.headers)


def test_an_unhashed_file_is_revalidated(web):
    response = web.get("/app/favicon.svg")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"


@pytest.mark.parametrize("path", ["/app/assets/index-000000.js", "/app/logo.png"])
def test_a_missing_file_with_an_extension_is_a_404_not_the_page(web, path):
    response = web.get(path)

    assert response.status_code == 404
    assert response.text != INDEX_HTML


def test_a_path_escaping_the_bundle_is_never_served(web):
    response = web.get("/app/%2e%2e/secret.txt")

    assert response.status_code == 404
    assert SECRET not in response.text


def test_head_is_answered_like_get_without_a_body(web):
    response = web.head("/app/pool/26")

    assert response.status_code == 200
    assert response.content == b""


@pytest.mark.parametrize("path", ["/", "/app", "/app/", "/app/index", "/app/pool/1"])
def test_a_write_under_the_client_is_refused_before_any_route_sees_it(
    web, facilitator, path
):
    """A POST to `/app/index` answers 405 -- not the pool route's 503, and
    never a 402."""
    response = web.post(path)

    assert response.status_code == 405
    assert "payment-required" not in response.headers
    assert facilitator.verified == []


# --- no bundle ----------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/app", "/app/", "/app/pool/1"])
def test_nothing_is_served_under_the_client_without_a_bundle(client, path):
    """`settings_factory` points at a directory that does not exist, which is
    the state of a checkout that never ran the web build."""
    assert client.get(path, follow_redirects=False).status_code == 404


def test_a_bundle_directory_without_its_page_refuses_to_boot(
    settings_factory, tmp_path
):
    """A half-copied build would serve every asset and 404 every page."""
    (tmp_path / "assets").mkdir()
    settings = settings_factory(web_dist_dir=str(tmp_path))

    with pytest.raises(ConfigError, match="index.html"):
        verify_startup(settings, {}, FakeAlgod())


def test_a_complete_bundle_passes_the_startup_guards(settings_factory, bundle):
    verify_startup(settings_factory(web_dist_dir=str(bundle)), {}, FakeAlgod())
