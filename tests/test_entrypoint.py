"""Tests for the non-root entrypoint verification API/engine.

These encode the semantics requested for deployment preparation:

* resolve the default command entry (Entrypoint[0] else Cmd[0]);
* absolute / slash-relative / bare-on-PATH candidate resolution;
* component-by-component directory search + regular-file execute checks;
* exactly one of owner/group/other permission classes;
* overlaid metadata, whiteouts, opaques and implicit 0755 root:root dirs;
* first-blocker diagnostics for the working dir and every candidate.
"""

from __future__ import annotations

import tracemalloc
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.entrypoint import (
    TargetOverlay,
    build_candidates,
    component_keys,
    normalize_key,
    verify_entrypoint,
)
from app.main import app
from tests.conftest import (
    build_layer_tar,
    dir_entry,
    file_entry,
    make_layout,
    opaque_entry,
    whiteout_entry,
)

TAG = "ep:1"


@pytest.fixture()
def layout(tmp_path, monkeypatch):
    root = tmp_path / "oci-layout"
    tars = tmp_path / "tars"
    tars.mkdir()

    def publish(tag: str, entries, config: dict, *, name: str | None = None):
        tar_path = tars / (name or tag.replace(":", "_") + ".tar")
        build_layer_tar(tar_path, entries)
        make_layout(root, tag, [tar_path], startup_config=config)
        return root

    monkeypatch.setenv("OCI_LAYOUT_DIR", str(root))
    return root, tars, publish


@pytest.fixture()
def client():
    return TestClient(app)


def publish_layers(root: Path, tag: str, layer_entries, config: dict, tmp_path: Path):
    """Publish an image with several layers (one entries-list per layer)."""
    paths = []
    tdir = tmp_path / f"layers-{tag.replace(':', '_')}"
    tdir.mkdir(exist_ok=True)
    for i, entries in enumerate(layer_entries):
        p = tdir / f"l{i}.tar"
        build_layer_tar(p, entries)
        paths.append(p)
    make_layout(root, tag, paths, startup_config=config)
    return paths


# ---------------------------------------------------------------------------
# pure path resolution
# ---------------------------------------------------------------------------


def test_normalize_key_rejects_dotdot():
    assert normalize_key("/a/./b/") == "a/b"
    with pytest.raises(Exception, match="\\.\\."):
        normalize_key("/a/../b")


def test_component_keys():
    assert component_keys("") == [""]
    assert component_keys("a/b") == ["", "a", "a/b"]


def test_build_candidates_shapes():
    c = build_candidates("/abs/tool", "wd", "/bin:/usr/bin")
    assert [(x.kind, x.key) for x in c] == [("absolute", "abs/tool")]

    c = build_candidates("sub/tool", "wd", "/bin")
    assert [(x.kind, x.key) for x in c] == [("relative", "wd/sub/tool")]

    c = build_candidates("tool", "wd", "/bin::usr/local/bin:")
    keys = [x.key for x in c]
    # empty segments -> working dir; trailing colon is another empty segment
    assert keys == ["bin/tool", "wd/tool", "wd/usr/local/bin/tool", "wd/tool"]
    assert [x.path_entry for x in c] == ["/bin", "", "usr/local/bin", ""]


# ---------------------------------------------------------------------------
# command selection + working dir defaults
# ---------------------------------------------------------------------------


def test_entrypoint_first_item_preferred(layout):
    root, _, publish = layout
    publish(TAG, [file_entry("ep", b"x", 0o755), file_entry("cmd", b"x", 0o755)],
            {"Entrypoint": ["/ep", "arg"], "Cmd": ["/cmd"]})
    r = verify_entrypoint(str(root), TAG, 1000, 1000)
    assert r["ok"] and r["entrypoint"] == "/ep"
    assert r["command_source"] == "entrypoint"
    assert r["resolved_command"] == ["/ep", "arg"]


def test_empty_entrypoint_falls_back_to_cmd(layout):
    root, _, publish = layout
    publish(TAG, [file_entry("cmd", b"x", 0o755)],
            {"Entrypoint": [], "Cmd": ["/cmd"]})
    r = verify_entrypoint(str(root), TAG, 1000, 1000)
    assert r["ok"] and r["entrypoint"] == "/cmd"
    assert r["command_source"] == "cmd"


def test_missing_both_command_reported(layout):
    root, _, publish = layout
    # NB: conftest.make_layout treats a falsey config as "use defaults", so
    # pass a truthy config that simply has neither Entrypoint nor Cmd.
    publish(TAG, [file_entry("x", b"x")], {"User": "1000"})
    r = verify_entrypoint(str(root), TAG, 1000, 1000)
    assert r["ok"] is False and r["entrypoint"] is None
    assert r["status"] == "no_command"


def test_empty_working_dir_is_root(layout):
    root, _, publish = layout
    publish(TAG, [file_entry("tool", b"x", 0o755)],
            {"WorkingDir": "", "Cmd": ["./tool"]})
    r = verify_entrypoint(str(root), TAG, 1000, 1000)
    assert r["working_dir"]["path"] == "/"
    assert r["ok"] and r["entrypoint"] == "/tool"


def test_relative_working_dir_anchored_at_root(layout):
    root, _, publish = layout
    publish(TAG, [dir_entry("work"), file_entry("work/t", b"x", 0o755)],
            {"WorkingDir": "work", "Cmd": ["./t"]})
    r = verify_entrypoint(str(root), TAG, 1000, 1000)
    assert r["ok"] and r["entrypoint"] == "/work/t"


# ---------------------------------------------------------------------------
# candidate resolution against the overlaid image
# ---------------------------------------------------------------------------


def test_absolute_candidate(layout):
    root, _, publish = layout
    publish(TAG, [dir_entry("opt"), file_entry("opt/x", b"x", 0o755)],
            {"Cmd": ["/opt/x"]})
    r = verify_entrypoint(str(root), TAG, 1000, 1000)
    assert r["ok"] and r["entrypoint"] == "/opt/x"
    assert r["candidates"][0]["resolution"] == "absolute"


def test_slash_relative_candidate_uses_workdir(layout):
    root, _, publish = layout
    publish(TAG, [dir_entry("app"), file_entry("app/bin-run", b"x", 0o755)],
            {"WorkingDir": "/app", "Cmd": ["bin-run"]})  # bare, PATH unset default
    # make it slash-relative explicitly
    r = verify_entrypoint(str(root), TAG, 1000, 1000)
    # bare command with fallback PATH will not find /app/bin-run unless wd is
    # in PATH; assert the slash form separately:
    publish("rel:1", [dir_entry("app"), file_entry("app/sub/run", b"x", 0o755),
                      dir_entry("app/sub")],
            {"WorkingDir": "/app", "Cmd": ["sub/run"]}, name="rel.tar")
    r = verify_entrypoint(str(root), "rel:1", 1000, 1000)
    assert r["ok"] and r["entrypoint"] == "/app/sub/run"
    assert r["candidates"][0]["resolution"] == "relative"


def test_bare_command_path_order_first_wins(layout):
    root, _, publish = layout
    publish(TAG, [
        dir_entry("b1"), dir_entry("b2"),
        file_entry("b1/tool", b"1", 0o755),
        file_entry("b2/tool", b"2", 0o755),
    ], {"Cmd": ["tool"], "Env": ["PATH=/b1:/b2"]})
    r = verify_entrypoint(str(root), TAG, 1000, 1000)
    assert r["ok"] and r["entrypoint"] == "/b1/tool"
    assert [c["path"] for c in r["candidates"]] == ["/b1/tool", "/b2/tool"]


def test_empty_path_segment_means_workdir(layout):
    root, _, publish = layout
    publish(TAG, [dir_entry("work"), file_entry("work/tool", b"x", 0o755)],
            {"WorkingDir": "/work", "Cmd": ["tool"], "Env": ["PATH=/no/bin::/bin"]})
    r = verify_entrypoint(str(root), TAG, 1000, 1000)
    assert [c["path"] for c in r["candidates"]] == [
        "/no/bin/tool", "/work/tool", "/bin/tool"
    ]
    assert r["ok"] and r["entrypoint"] == "/work/tool"
    assert r["candidates"][1]["path_entry"] == ""


# ---------------------------------------------------------------------------
# permission classes: owner / group / other, exactly one class
# ---------------------------------------------------------------------------


def test_owner_class_used_exclusively(layout):
    root, _, publish = layout
    # owner has NO x bits; group/other have rwx. uid matches owner -> denied.
    publish(TAG, [file_entry("x", b"x", 0o077, uid=1000, gid=1000)],
            {"Cmd": ["/x"]})
    r = verify_entrypoint(str(root), TAG, 1000, 1000)
    c = r["candidates"][0]
    assert r["ok"] is False
    assert c["code"] == "permission_denied" and c["blocked_at"] == "/x"
    assert c["trace"][-1]["matched_class"] == "owner"


def test_owner_class_allows(layout):
    root, _, publish = layout
    publish(TAG, [file_entry("x", b"x", 0o700, uid=1000, gid=0)], {"Cmd": ["/x"]})
    r = verify_entrypoint(str(root), TAG, 1000, 1000)
    assert r["ok"] and r["entrypoint"] == "/x"
    assert r["candidates"][0]["matched_class"] == "owner"


def test_group_class_used_exclusively(layout):
    root, _, publish = layout
    # group has no x; owner and other do. gid matches -> denied via group only.
    publish(TAG, [file_entry("g", b"x", 0o707, uid=0, gid=50)], {"Cmd": ["/g"]})
    r = verify_entrypoint(str(root), TAG, 1000, 50)
    c = r["candidates"][0]
    assert r["ok"] is False and c["code"] == "permission_denied"
    assert c["trace"][-1]["matched_class"] == "group"


def test_group_class_allows(layout):
    root, _, publish = layout
    publish(TAG, [file_entry("g", b"x", 0o750, uid=0, gid=50)], {"Cmd": ["/g"]})
    r = verify_entrypoint(str(root), TAG, 1000, 50)
    assert r["ok"] and r["entrypoint"] == "/g"
    assert r["candidates"][0]["matched_class"] == "group"


def test_other_class(layout):
    root, _, publish = layout
    publish(TAG, [file_entry("o", b"x", 0o750, uid=0, gid=0)], {"Cmd": ["/o"]})
    r = verify_entrypoint(str(root), TAG, 1000, 1000)  # uid/gid match neither
    c = r["candidates"][0]
    assert c["trace"][-1]["matched_class"] == "other"
    assert r["ok"] is False and c["code"] == "permission_denied"


def test_other_class_allows(layout):
    root, _, publish = layout
    publish(TAG, [file_entry("o", b"x", 0o755)], {"Cmd": ["/o"]})
    r = verify_entrypoint(str(root), TAG, 1000, 1000)
    assert r["ok"] and r["entrypoint"] == "/o"


# ---------------------------------------------------------------------------
# traversal and first-blocker diagnostics
# ---------------------------------------------------------------------------


def test_directory_without_search_blocks_at_directory(layout):
    root, _, publish = layout
    publish(TAG, [
        dir_entry("locked", 0o700, uid=0, gid=0),
        file_entry("locked/tool", b"x", 0o755),
    ], {"Cmd": ["/locked/tool"]})
    r = verify_entrypoint(str(root), TAG, 1000, 1000)
    c = r["candidates"][0]
    assert r["ok"] is False
    assert c["blocked_at"] == "/locked" and c["code"] == "permission_denied"
    # the file itself is never reached as the blocker
    assert [s["path"] for s in c["trace"]] == ["/", "/locked"]


def test_missing_intermediate_blocks_at_first_missing(layout):
    root, _, publish = layout
    publish(TAG, [file_entry("z", b"x", 0o755)], {"Cmd": ["/no/such/tool"]})
    r = verify_entrypoint(str(root), TAG, 1000, 1000)
    c = r["candidates"][0]
    assert c["code"] == "not_found" and c["blocked_at"] == "/no"


def test_missing_final_blocks_at_candidate(layout):
    root, _, publish = layout
    publish(TAG, [dir_entry("a", 0o755)], {"Cmd": ["/a/missing"]})
    r = verify_entrypoint(str(root), TAG, 1000, 1000)
    c = r["candidates"][0]
    assert c["code"] == "not_found" and c["blocked_at"] == "/a/missing"


def test_intermediate_file_is_not_a_directory(layout):
    root, _, publish = layout
    publish(TAG, [file_entry("a", b"x", 0o755)], {"Cmd": ["/a/b/tool"]})
    r = verify_entrypoint(str(root), TAG, 1000, 1000)
    c = r["candidates"][0]
    assert c["code"] == "not_a_directory" and c["blocked_at"] == "/a"


def test_directory_candidate_is_not_regular_file(layout):
    root, _, publish = layout
    publish(TAG, [dir_entry("ad", 0o755)], {"Cmd": ["/ad"]})
    r = verify_entrypoint(str(root), TAG, 1000, 1000)
    assert r["candidates"][0]["code"] == "not_a_regular_file"
    assert r["candidates"][0]["blocked_at"] == "/ad"


def test_non_executable_regular_file_denied(layout):
    root, _, publish = layout
    publish(TAG, [file_entry("f", b"x", 0o644)], {"Cmd": ["/f"]})
    r = verify_entrypoint(str(root), TAG, 1000, 1000)
    c = r["candidates"][0]
    assert c["code"] == "permission_denied" and c["blocked_at"] == "/f"


def test_working_dir_checked_first_and_gates_absolute_candidate(layout, tmp_path):
    root, _, _ = layout
    publish_layers(root, TAG, [[
        dir_entry("secret", 0o700, 0, 0),
        file_entry("secret/run", b"x", 0o755),
        file_entry("abs-tool", b"x", 0o755),
    ]], {"WorkingDir": "/secret", "Cmd": ["/abs-tool"]}, tmp_path)
    r = verify_entrypoint(str(root), TAG, 1000, 1000)
    assert r["working_dir"]["ok"] is False
    assert r["working_dir"]["blocked_at"] == "/secret"
    # absolute candidate is individually runnable ...
    assert r["candidates"][0]["ok"] is True
    # ... but the overall verdict still fails because chdir would fail
    assert r["ok"] is False and r["entrypoint"] is None
    assert r["first_block"]["scope"] == "working_dir"
    assert r["blocked_at"] == "/secret"


def test_missing_working_dir(layout):
    root, _, publish = layout
    publish(TAG, [file_entry("x", b"x", 0o755)],
            {"WorkingDir": "/nope", "Cmd": ["/x"]})
    r = verify_entrypoint(str(root), TAG, 1000, 1000)
    assert r["working_dir"]["ok"] is False
    assert r["working_dir"]["code"] == "not_found"
    assert r["working_dir"]["blocked_at"] == "/nope"
    assert r["ok"] is False


# ---------------------------------------------------------------------------
# PATH fallback / re-selection
# ---------------------------------------------------------------------------


def test_blocked_first_path_entry_falls_through(layout):
    root, _, publish = layout
    publish(TAG, [
        dir_entry("b1", 0o700, 0, 0),
        file_entry("b1/tool", b"1", 0o755),
        dir_entry("b2", 0o755),
        file_entry("b2/tool", b"2", 0o755),
    ], {"Cmd": ["tool"], "Env": ["PATH=/b1:/b2"]})
    r = verify_entrypoint(str(root), TAG, 1000, 1000)
    assert r["candidates"][0]["ok"] is False
    assert r["candidates"][0]["blocked_at"] == "/b1"
    assert r["candidates"][1]["ok"] is True
    assert r["entrypoint"] == "/b2/tool" and r["ok"] is True


def test_missing_first_candidate_falls_through(layout):
    root, _, publish = layout
    publish(TAG, [dir_entry("b2"), file_entry("b2/t", b"x", 0o755)],
            {"Cmd": ["t"], "Env": ["PATH=/b1:/b2"]})
    r = verify_entrypoint(str(root), TAG, 1000, 1000)
    assert r["entrypoint"] == "/b2/t"
    assert r["candidates"][0]["code"] == "not_found"


# ---------------------------------------------------------------------------
# overlay semantics across layers
# ---------------------------------------------------------------------------


def test_upper_layer_directory_chmod_reselects_entry(layout, tmp_path):
    """The headline demo: an upper layer chmods the first bin dir, so the
    checker re-selects the second PATH entry."""
    root, _, _ = layout
    config = {"Cmd": ["serve"], "Env": ["PATH=/b1:/b2"], "WorkingDir": "/"}
    publish_layers(root, "before:1", [[
        dir_entry("b1", 0o755), dir_entry("b2", 0o755),
        file_entry("b1/serve", b"1", 0o755),
        file_entry("b2/serve", b"2", 0o755),
    ]], config, tmp_path)
    before = verify_entrypoint(str(root), "before:1", 1000, 1000)
    assert before["entrypoint"] == "/b1/serve"

    # NB: the single-tag make_layout helper rewrites index.json, so publishing
    # "after:1" drops "before:1" from the index; blobs persist and the "after"
    # check below only needs its own tag.
    publish_layers(root, "after:1", [
        [
            dir_entry("b1", 0o755), dir_entry("b2", 0o755),
            file_entry("b1/serve", b"1", 0o755),
            file_entry("b2/serve", b"2", 0o755),
        ],
        [dir_entry("b1", 0o700, 0, 0)],  # upper layer changes dir rights
    ], config, tmp_path)
    after = verify_entrypoint(str(root), "after:1", 1000, 1000)
    assert after["candidates"][0]["blocked_at"] == "/b1"
    assert after["entrypoint"] == "/b2/serve"


def test_upper_layer_chown_changes_permission_class(layout, tmp_path):
    root, _, _ = layout
    publish_layers(root, TAG, [
        [file_entry("f", b"x", 0o700, uid=0, gid=0)],
        [file_entry("f", b"x", 0o700, uid=1000, gid=0)],  # chown to the user
    ], {"Cmd": ["/f"]}, tmp_path)
    r = verify_entrypoint(str(root), TAG, 1000, 1000)
    assert r["ok"] is True
    assert r["candidates"][0]["trace"][-1]["matched_class"] == "owner"
    assert r["candidates"][0]["uid"] == 1000


def test_whiteout_candidate_shadows_lower(layout, tmp_path):
    root, _, _ = layout
    publish_layers(root, TAG, [
        [file_entry("gone", b"x", 0o755), file_entry("keep", b"x", 0o755)],
        [whiteout_entry("gone")],
    ], {"Cmd": ["/gone"]}, tmp_path)
    r = verify_entrypoint(str(root), TAG, 1000, 1000)
    assert r["ok"] is False and r["candidates"][0]["code"] == "not_found"
    assert r["candidates"][0]["blocked_at"] == "/gone"


def test_whiteout_then_recreate_in_higher_layer(layout, tmp_path):
    root, _, _ = layout
    publish_layers(root, TAG, [
        [file_entry("tool", b"old", 0o755)],
        [whiteout_entry("tool")],
        [file_entry("tool", b"new", 0o755, uid=1000)],
    ], {"Cmd": ["/tool"]}, tmp_path)
    r = verify_entrypoint(str(root), TAG, 1000, 1000)
    assert r["ok"] is True and r["entrypoint"] == "/tool"
    assert r["candidates"][0]["uid"] == 1000


def test_opaque_whiteout_hides_lower_candidate(layout, tmp_path):
    root, _, _ = layout
    publish_layers(root, TAG, [
        [dir_entry("d", 0o755), file_entry("d/tool", b"x", 0o755)],
        [opaque_entry("d")],
    ], {"Cmd": ["/d/tool"]}, tmp_path)
    r = verify_entrypoint(str(root), TAG, 1000, 1000)
    # opaque hides the lower tool; d itself remains as a directory
    assert r["candidates"][0]["code"] == "not_found"
    assert r["candidates"][0]["blocked_at"] == "/d/tool"
    assert r["working_dir"]["path"] == "/" and r["working_dir"]["ok"]


def test_implicit_directories_get_defaults(layout):
    root, _, publish = layout
    publish(TAG, [file_entry("a/b/c/tool", b"x", 0o755)],
            {"Cmd": ["/a/b/c/tool"]})  # no explicit dir entries at all
    r = verify_entrypoint(str(root), TAG, 1000, 1000)
    assert r["ok"] is True
    steps = r["candidates"][0]["trace"]
    for step in steps[1:-1]:
        assert step["implicit"] is True
        assert step["mode"] == 0o755 and (step["uid"], step["gid"]) == (0, 0)


def test_whiteout_dir_then_deep_entry_reimports_implicit_ancestors(layout, tmp_path):
    root, _, _ = layout
    publish_layers(root, TAG, [
        [file_entry("d/sub/old", b"x", 0o755)],   # implicit d, d/sub
        [whiteout_entry("d")],                     # remove the whole dir
        [file_entry("d/sub/tool", b"x", 0o755)],   # re-create d/d-sub implicitly
    ], {"Cmd": ["/d/sub/tool"]}, tmp_path)
    r = verify_entrypoint(str(root), TAG, 1000, 1000)
    assert r["ok"] is True and r["entrypoint"] == "/d/sub/tool"
    by_path = {s["path"]: s for c in r["candidates"] for s in c["trace"]}
    assert by_path["/d"]["implicit"] is True
    assert by_path["/d/sub"]["implicit"] is True


def test_whiteout_only_child_keeps_implicit_parent(layout, tmp_path):
    root, _, _ = layout
    publish_layers(root, TAG, [
        [file_entry("d/tool", b"x", 0o755)],  # d is implicit
        [whiteout_entry("d/tool")],           # removes file, parent d persists
    ], {"Cmd": ["/d/other"]}, tmp_path)
    r = verify_entrypoint(str(root), TAG, 1000, 1000)
    c = r["candidates"][0]
    # the parent /d still exists as an implicit dir, so the first block is the
    # missing child, not /d
    assert c["code"] == "not_found" and c["blocked_at"] == "/d/other"


# ---------------------------------------------------------------------------
# resource contract: targeted metadata retention + body skipping
# ---------------------------------------------------------------------------


def test_overlay_retains_only_interesting_keys():
    interest = {"", "wd", "wd/tool"}
    ov = TargetOverlay(interest)

    import io
    import tarfile
    # build a real tar with many unrelated entries and one interesting entry
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for i in range(50):
            data = b"unrelated" * 100
            ti = tarfile.TarInfo(f"many/unrelated/{i}.dat")
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        ti = tarfile.TarInfo("wd/tool")
        ti.mode = 0o755
        ti.size = 1
        tf.addfile(ti, io.BytesIO(b"x"))
    buf.seek(0)
    with tarfile.open(fileobj=buf, mode="r:") as tf:
        ov.scan_layer(tf, 0)

    assert set(ov.states) <= interest | {""}
    assert ov.states["wd/tool"].kind == "file"
    # unrelated paths never got state
    assert not any(k.startswith("many") for k in ov.states)
    # but their prefixes are not recorded as seen either (not interesting)
    assert "many" not in ov.seen


def test_scan_does_not_load_file_bodies(layout):
    root, _, publish = layout
    big = b"0" * (32 * 1024 * 1024)
    tars = root.parent / "tars"
    tars.mkdir(exist_ok=True)
    tar_path = tars / "big.tar"
    build_layer_tar(tar_path, [
        file_entry("big.bin", big),
        file_entry("bin/tool", b"x", 0o755),
        dir_entry("bin"),
    ])
    make_layout(root, "big:1", [tar_path], startup_config={"Cmd": ["/bin/tool"]})

    tracemalloc.start()
    try:
        r = verify_entrypoint(str(root), "big:1", 1000, 1000)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert r["entrypoint"] == "/bin/tool"
    assert peak < 8 * 1024 * 1024, f"peak {peak / 2**20:.1f} MiB too high"


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------


def test_api_verify_200(client, layout):
    root, _, publish = layout
    publish(TAG, [dir_entry("bin"), file_entry("bin/app", b"x", 0o755)],
            {"Cmd": ["app"], "Env": ["PATH=/bin"]})
    resp = client.post("/verify-entrypoint", json={"tag": TAG, "uid": 1000, "gid": 1000})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True and body["entrypoint"] == "/bin/app"


def test_api_negative_verdict_is_200_with_diagnostics(client, layout):
    root, _, publish = layout
    publish(TAG, [dir_entry("bin", 0o700), file_entry("bin/app", b"x", 0o755)],
            {"Cmd": ["app"], "Env": ["PATH=/bin"]})
    resp = client.post("/verify-entrypoint", json={"tag": TAG, "uid": 1000, "gid": 1000})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["candidates"][0]["blocked_at"] == "/bin"
    assert body["failures"]


def test_api_accepts_field_aliases_and_get(client, layout):
    root, _, publish = layout
    publish(TAG, [file_entry("app", b"x", 0o755)], {"Cmd": ["/app"]})
    resp = client.post("/entrypoint", json={"image": TAG, "user": {"uid": "1000", "gid": 1000}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["entrypoint"] == "/app"

    resp = client.get("/verify-entrypoint", params={"tag": TAG, "uid": 1000, "gid": 1000})
    assert resp.status_code == 200 and resp.json()["entrypoint"] == "/app"


def test_api_unknown_tag_404(client, layout):
    root, _, publish = layout
    publish(TAG, [file_entry("app", b"x", 0o755)], {"Cmd": ["/app"]})
    resp = client.post("/verify-entrypoint", json={"tag": "nope:0", "uid": 1000, "gid": 1000})
    assert resp.status_code == 404


def test_api_uid_zero_400(client, layout):
    root, _, publish = layout
    publish(TAG, [file_entry("app", b"x", 0o755)], {"Cmd": ["/app"]})
    resp = client.post("/verify-entrypoint", json={"tag": TAG, "uid": 0, "gid": 0})
    assert resp.status_code == 400


def test_api_missing_fields_422(client):
    resp = client.post("/verify-entrypoint", json={"tag": TAG})
    assert resp.status_code == 422


def test_api_gid_must_be_scalar(client):
    resp = client.post("/verify-entrypoint",
                       json={"tag": TAG, "uid": 1000, "gid": [1000, 2000]})
    assert resp.status_code == 422
