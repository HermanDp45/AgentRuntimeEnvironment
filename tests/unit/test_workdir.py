import hashlib
import os

import httpx2
import pytest

from client.workdir import Workdir

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor-based exporter")


@pytest.fixture
def export(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    (root / "note.txt").write_text("original", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("private", encoding="utf-8")
    (root / ".env").write_text("private", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.write_text("outside", encoding="utf-8")
    (root / "link").symlink_to(outside)
    os.link(outside, root / "hardlink")
    server = Workdir(root)
    with httpx2.Client(
        base_url=f"http://127.0.0.1:{server.server_port}",
        headers={"Authorization": "Bearer " + server.token},
        trust_env=False,
    ) as client:
        yield client, root, outside
    server.close()


def test_export_paths_and_auth_are_enforced(export):
    client, _, outside = export
    assert client.get("/note.txt", headers={"Authorization": "Bearer wrong"}).status_code == 401
    listing = client.request("PROPFIND", "/", headers={"Depth": "1"})
    assert listing.status_code == 207
    assert "note.txt" in listing.text
    for hidden in (".git", ".env", "link", "hardlink"):
        assert hidden not in listing.text
    for path in ("/.git/config", "/.GIT/config", "/.env", "/link", "/hardlink", "/%2e%2e/outside"):
        assert client.get(path).status_code in (403, 404)
        reply = client.put(path, content=b"changed", headers={"X-Workdir-Sha256": "missing"})
        assert reply.status_code in (403, 404, 412)
    assert outside.read_text() == "outside"


def test_export_write_is_atomic_and_checks_expected_version(export):
    client, root, _ = export
    sha = hashlib.sha256(b"original").hexdigest()
    assert client.put("/note.txt", content=b"bad").status_code == 428
    assert (
        client.put("/note.txt", content=b"bad", headers={"X-Workdir-Sha256": "missing"}).status_code
        == 412
    )
    assert (root / "note.txt").read_text() == "original"
    reply = client.put("/note.txt", content=b"changed", headers={"X-Workdir-Sha256": sha})
    assert reply.status_code == 200
    assert (root / "note.txt").read_text() == "changed"
    assert not list(root.glob("._are_*"))
    assert (
        client.put("/note.txt", content=b"stale", headers={"X-Workdir-Sha256": sha}).status_code
        == 412
    )
    assert (
        client.put(
            "/new/note.txt", content=b"new", headers={"X-Workdir-Sha256": "missing"}
        ).status_code
        == 200
    )
    assert (root / "new/note.txt").read_text() == "new"


def test_webdav_read_supports_ranges(export):
    client, _, _ = export
    result = client.get("/note.txt", headers={"Range": "bytes=1-3"})
    assert result.status_code == 206 and result.content == b"rig"
    assert client.get("/note.txt", headers={"Range": "bytes=50-"}).status_code == 416
