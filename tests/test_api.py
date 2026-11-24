"""End-to-end API tests against a real OCI layout in a temp directory."""

from __future__ import annotations

import gzip
import hashlib
import json
import tarfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import (
    MT_LAYER,
    build_layer_tar,
    dir_entry,
    expand_stack,
    file_entry,
    make_layout,
    opaque_entry,
    whiteout_entry,
)

SRC = "demo:1.0.1"
DST = "demo:1.0.1-squashed"

STARTUP = {
    "Cmd": ["/app/server.bin", "--serve"],
    "Env": ["MODE=demo", "PORT=8080"],
    "WorkingDir": "/app",
    "User": "1000",
    "ExposedPorts": {"8080/tcp": {}},
    "Labels": {"app": "demo"},
}


@pytest.fixture()
def layout(tmp_path, monkeypatch):
    """A 3-layer demo image in a fresh OCI layout; returns (layout_dir, tars)."""
    root = tmp_path / "oci-layout"
    tars = tmp_path / "tars"
    tars.mkdir()
    base = tars / "base.tar"
    build_layer_tar(base, [
        dir_entry("app"), dir_entry("app/data"),
        file_entry("app/data/old.log", b"old log\n"),
        dir_entry("etc"), file_entry("etc/keep.cfg", b"keep=1\n"),
        file_entry("etc/old.cfg", b"old=1\n"),
    ])
    l1 = tars / "l1.tar"
    build_layer_tar(l1, [
        file_entry("app/data/junk.txt", b"junk\n"),
        file_entry("app/server.bin", b"v1", mode=0o755),
        file_entry("etc/old.cfg", b"old=2\n"),
    ])
    l2 = tars / "l2.tar"
    build_layer_tar(l2, [
        opaque_entry("app/data"),
        file_entry("app/data/fresh.txt", b"fresh\n"),
        whiteout_entry("etc/old.cfg"),
        file_entry("app/server.bin", b"v2", mode=0o755),
    ])
    make_layout(root, SRC, [base, l1, l2], startup_config=STARTUP)
    monkeypatch.setenv("OCI_LAYOUT_DIR", str(root))
    return root, [base, l1, l2]


@pytest.fixture()
def client():
    return TestClient(app)


def _read_json_blob(root: Path, digest: str) -> dict:
    return json.loads((root / "blobs" / "sha256" / digest.split(":", 1)[1]).read_bytes())


def _layer_tar_path(root: Path, digest: str) -> Path:
    return root / "blobs" / "sha256" / digest.split(":", 1)[1]


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_merge_happy_path(client, layout):
    root, (base, l1, l2) = layout
    resp = client.post("/merge", json={"source_tag": SRC, "target_tag": DST})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["target_tag"] == DST
    assert body["base_layers_kept"] == 1
    assert body["layers_merged"] == 2
    assert len(body["layers"]) == 2  # base + merged

    # both tags are now listed
    assert set(client.get("/tags").json()["tags"]) == {SRC, DST}

    # --- new manifest & config are consistent with the generated layer ----
    index = json.loads((root / "index.json").read_text())
    descs = {d["annotations"]["org.opencontainers.image.ref.name"]: d for d in index["manifests"]}
    new_manifest = _read_json_blob(root, descs[DST]["digest"])
    old_manifest = _read_json_blob(root, descs[SRC]["digest"])

    # base layer descriptor is reused byte-for-byte (same digest)
    assert new_manifest["layers"][0] == old_manifest["layers"][0]
    assert new_manifest["layers"][1]["digest"] == body["merged_layer"]["digest"]
    assert new_manifest["layers"][1]["size"] == body["merged_layer"]["size"]

    # blob really contains what the digest claims
    merged_blob = _layer_tar_path(root, body["merged_layer"]["digest"]).read_bytes()
    assert hashlib.sha256(merged_blob).hexdigest() == body["merged_layer"]["digest"].split(":")[1]

    new_config = _read_json_blob(root, new_manifest["config"]["digest"])
    old_config = _read_json_blob(root, old_manifest["config"]["digest"])
    # diff_ids: base diff_id kept, last two replaced by the merged one
    assert new_config["rootfs"]["diff_ids"] == old_config["rootfs"]["diff_ids"][:-2] + [
        body["merged_layer"]["diff_id"]
    ]
    # startup config preserved exactly
    assert new_config["config"] == STARTUP == old_config["config"]
    # history stays aligned with the layer list
    non_empty = [h for h in new_config["history"] if not h.get("empty_layer")]
    assert len(non_empty) == len(new_config["rootfs"]["diff_ids"])

    # --- expanding old and new images yields identical trees --------------
    merged_tar = _layer_tar_path(root, body["merged_layer"]["digest"])
    expand_stack([base, merged_tar]).assert_same_as(expand_stack([base, l1, l2]))


def test_merge_source_tag_untouched_and_remergeable(client, layout):
    root, _ = layout
    before = (root / "index.json").read_text()
    client.post("/merge", json={"source_tag": SRC, "target_tag": DST})
    # source manifest descriptor unchanged in the index
    index = json.loads((root / "index.json").read_text())
    src = [d for d in index["manifests"] if d["annotations"]["org.opencontainers.image.ref.name"] == SRC]
    assert len(src) == 1 and src[0]["digest"] in before
    # and the source image can be merged again under another name
    resp = client.post("/merge", json={"source_tag": SRC, "target_tag": "demo:again"})
    assert resp.status_code == 200


def test_merge_unknown_source_404(client, layout):
    resp = client.post("/merge", json={"source_tag": "nope:0", "target_tag": DST})
    assert resp.status_code == 404


def test_merge_existing_target_409(client, layout):
    assert client.post("/merge", json={"source_tag": SRC, "target_tag": DST}).status_code == 200
    resp = client.post("/merge", json={"source_tag": SRC, "target_tag": DST})
    assert resp.status_code == 409


def test_merge_same_tag_400(client, layout):
    resp = client.post("/merge", json={"source_tag": SRC, "target_tag": SRC})
    assert resp.status_code == 400


def test_merge_compressed_layer_422_and_no_partial_publish(client, layout, tmp_path):
    root, (base, l1, l2) = layout
    # rebuild the image with a gzip-compressed top layer (lying descriptor)
    raw = l2.read_bytes()
    gz = tmp_path / "l2.tar.gz"
    gz.write_bytes(gzip.compress(raw))
    make_layout(root, "demo:gz", [base, l1, gz], startup_config=STARTUP)

    index_before = (root / "index.json").read_text()
    resp = client.post("/merge", json={"source_tag": "demo:gz", "target_tag": "demo:gz-out"})
    assert resp.status_code == 422
    # failure leaves the layout as it was: source usable, target absent
    assert (root / "index.json").read_text() == index_before
    assert "demo:gz-out" not in client.get("/tags").json()["tags"]
    assert "demo:gz" in client.get("/tags").json()["tags"]


def test_merge_gzip_media_type_422(client, layout, tmp_path):
    root, (base, l1, l2) = layout
    make_layout(
        root, "demo:gzmt", [base, l1, l2], startup_config=STARTUP,
        layer_media_type="application/vnd.oci.image.layer.v1.tar+gzip",
    )
    resp = client.post("/merge", json={"source_tag": "demo:gzmt", "target_tag": "demo:x"})
    assert resp.status_code == 422
    assert "demo:x" not in client.get("/tags").json()["tags"]


def test_merge_multiplatform_index_422(client, layout):
    root, _ = layout
    index = json.loads((root / "index.json").read_text())
    index["manifests"].append(
        {
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "digest": "sha256:" + "0" * 64,
            "size": 100,
            "annotations": {"org.opencontainers.image.ref.name": "demo:multi"},
        }
    )
    (root / "index.json").write_text(json.dumps(index))
    resp = client.post("/merge", json={"source_tag": "demo:multi", "target_tag": "demo:y"})
    assert resp.status_code == 422
    assert "single-platform" in resp.json()["detail"]


def test_merge_single_layer_image_422(client, layout, tmp_path):
    root, (base, l1, l2) = layout
    make_layout(root, "demo:thin", [base], startup_config=STARTUP)
    resp = client.post("/merge", json={"source_tag": "demo:thin", "target_tag": "demo:z"})
    assert resp.status_code == 422


def test_merge_two_layer_image_no_base(client, layout, tmp_path):
    """With exactly two layers the merged image has a single layer."""
    root, (base, l1, l2) = layout
    make_layout(root, "demo:two", [l1, l2], startup_config=STARTUP)
    resp = client.post("/merge", json={"source_tag": "demo:two", "target_tag": "demo:two-out"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["base_layers_kept"] == 0 and len(body["layers"]) == 1
    merged_tar = _layer_tar_path(root, body["merged_layer"]["digest"])
    expand_stack([merged_tar]).assert_same_as(expand_stack([l1, l2]))


def test_failed_merge_cleans_temp_files(client, layout):
    root, _ = layout
    blobs = root / "blobs" / "sha256"
    client.post("/merge", json={"source_tag": "nope:0", "target_tag": "demo:tmp"})
    assert list(blobs.glob(".tmp-*")) == []
