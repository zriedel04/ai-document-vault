import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vault import main as main_module
from vault.core.config import Settings
from vault.main import create_app
from vault.openapi import main as openapi_main
from vault.openapi import schema_text

INDEX_HTML = "<!doctype html><html><body><div id='root'></div></body></html>"


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    folder = tmp_path / "dist"
    (folder / "assets").mkdir(parents=True)
    (folder / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (folder / "assets" / "app-123.js").write_text("console.log(1);", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside the dist folder", encoding="utf-8")
    return folder


@pytest.fixture
def client(dist: Path) -> TestClient:
    settings = Settings.model_validate({"frontend": {"dist": str(dist)}})
    return TestClient(create_app(settings))


def test_root_serves_the_built_frontend(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.text == INDEX_HTML
    assert response.headers["content-type"].startswith("text/html")


def test_assets_are_served_as_files(client: TestClient) -> None:
    response = client.get("/assets/app-123.js")
    assert response.status_code == 200
    assert response.text == "console.log(1);"


@pytest.mark.parametrize("path", ["/documents", "/search", "/documents/nested/route"])
def test_client_side_routes_return_the_app(client: TestClient, path: str) -> None:
    assert client.get(path).text == INDEX_HTML


def test_api_still_works_next_to_the_frontend(client: TestClient) -> None:
    assert client.get("/api/health").json()["status"] == "ok"
    assert client.get("/api/openapi.json").status_code == 200


@pytest.mark.parametrize("path", ["/api/nothing", "/api/", "/api/health/extra", "/api"])
def test_unknown_api_paths_are_404_not_the_app(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 404
    assert "root" not in response.text


@pytest.mark.parametrize(
    "method", ["post", "put", "patch", "delete", "options", "head", "trace", "connect", "FOO"]
)
@pytest.mark.parametrize("path", ["/api/nothing", "/api/", "/api/health/extra", "/api"])
def test_unknown_api_paths_are_404_for_every_method(
    client: TestClient, method: str, path: str
) -> None:
    response = client.request(method, path)
    assert response.status_code == 404
    assert "root" not in response.text


def test_a_wrong_method_on_a_real_api_route_is_still_405(client: TestClient) -> None:
    response = client.post("/api/health")
    assert response.status_code == 405
    assert "GET" in response.headers["allow"]
    assert client.delete("/api/openapi.json").status_code == 405


@pytest.mark.parametrize("path", ["/missing.js", "/assets/missing.css", "/favicon.ico"])
def test_missing_files_are_404_not_the_app(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/../secret.txt",
        "/%2e%2e/secret.txt",
        "/..%2fsecret.txt",
        "/assets/../../secret.txt",
        "/assets/%2e%2e/%2e%2e/secret.txt",
        "/%2fetc%2fpasswd",
        "//etc/passwd",
        "/..%5csecret.txt",
        "/C:/Windows/win.ini",
        "/%00",
        "/secret.txt%00.js",
    ],
)
def test_paths_cannot_escape_the_dist_folder(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert "outside the dist folder" not in response.text
    assert "root:" not in response.text


def test_a_symlink_pointing_outside_dist_is_not_followed(client: TestClient, dist: Path) -> None:
    try:
        (dist / "assets" / "link.js").symlink_to(dist.parent / "secret.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")
    response = client.get("/assets/link.js")
    assert "outside the dist folder" not in response.text


def test_an_index_that_is_a_symlink_outside_dist_is_never_served(
    tmp_path: Path, dist: Path
) -> None:
    index = dist / "index.html"
    index.unlink()
    try:
        index.symlink_to(dist.parent / "secret.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")
    settings = Settings.model_validate({"frontend": {"dist": str(dist)}})
    client = TestClient(create_app(settings))  # refuses to serve the frontend at all
    for path in ("/", "/documents"):
        response = client.get(path)
        assert response.status_code == 404
        assert "outside the dist folder" not in response.text


def test_an_index_swapped_for_a_symlink_after_start_up_is_not_served(
    client: TestClient, dist: Path
) -> None:
    assert client.get("/documents").text == INDEX_HTML
    index = dist / "index.html"
    index.unlink()
    try:
        index.symlink_to(dist.parent / "secret.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")
    response = client.get("/documents")
    assert response.status_code == 404
    assert "outside the dist folder" not in response.text


def test_only_get_is_served_for_the_frontend(client: TestClient) -> None:
    assert client.post("/documents").status_code == 405
    assert client.head("/").status_code == 200


def test_no_frontend_routes_without_a_build(tmp_path: Path) -> None:
    settings = Settings.model_validate({"frontend": {"dist": str(tmp_path / "missing")}})
    client = TestClient(create_app(settings))
    assert client.get("/").status_code == 404
    assert client.get("/api/health").status_code == 200


def test_default_dist_location_is_used_when_unset(
    dist: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_module, "DEFAULT_FRONTEND_DIST", dist)
    assert TestClient(create_app(Settings())).get("/").text == INDEX_HTML


def test_the_schema_export_leaves_the_frontend_out(dist: Path) -> None:
    settings = Settings.model_validate({"frontend": {"dist": str(dist)}})
    app = create_app(settings, serve_frontend=False)
    assert TestClient(app).get("/").status_code == 404


def test_schema_export_is_stable_and_describes_health(tmp_path: Path) -> None:
    assert schema_text() == schema_text()
    schema = json.loads(schema_text())
    assert "/api/health" in schema["paths"]
    assert "/{request_path}" not in json.dumps(schema)
    target = tmp_path / "openapi.json"
    assert openapi_main([str(target)]) == 0
    assert target.read_text(encoding="utf-8") == schema_text()


def test_schema_export_needs_exactly_one_argument(capsys: pytest.CaptureFixture[str]) -> None:
    assert openapi_main([]) == 2
    assert "usage:" in capsys.readouterr().err


def test_committed_frontend_schema_is_current() -> None:
    committed = Path(__file__).resolve().parents[3] / "frontend" / "openapi.json"
    assert committed.read_text(encoding="utf-8") == schema_text(), (
        "frontend/openapi.json is stale: in backend/ run "
        "`uv run python -m vault.openapi ../frontend/openapi.json`, then in frontend/ run "
        "`npm run api:generate`"
    )
