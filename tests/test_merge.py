"""Merge-engine tests: build 3-layer images, merge the top two, and prove
that expanding base+merged yields exactly the same tree as base+L1+L2."""

from __future__ import annotations

import tarfile
import tracemalloc
from pathlib import Path

import pytest

from app.merge import LayerMerger, MergeError
from tests.conftest import (
    build_layer_tar,
    dir_entry,
    expand_stack,
    file_entry,
    layer_entry_names,
    opaque_entry,
    whiteout_entry,
)


def merge_layers(tmp_path: Path, l1: Path, l2: Path) -> Path:
    """Run the engine over two layer tars; return the merged tar path."""
    out = tmp_path / "merged.tar"
    with open(l1, "rb") as f1, open(l2, "rb") as f2:
        t1 = tarfile.open(fileobj=f1, mode="r:")
        t2 = tarfile.open(fileobj=f2, mode="r:")
        merger = LayerMerger()
        merger.add_layer(t1, 0)
        merger.add_layer(t2, 1)
        with open(out, "wb") as fout:
            merger.write_merged([t1, t2], fout)
    return out


def check_equivalent(tmp_path: Path, base: Path, l1: Path, l2: Path) -> Path:
    merged = merge_layers(tmp_path, l1, l2)
    old_tree = expand_stack([base, l1, l2])
    new_tree = expand_stack([base, merged])
    new_tree.assert_same_as(old_tree)
    return merged


# ---------------------------------------------------------------------------
# the demo scenario: opaque whiteout masks a directory, then new files are
# added to that same directory in the same layer
# ---------------------------------------------------------------------------


def test_opaque_dir_then_add_files(tmp_path):
    base = tmp_path / "base.tar"
    build_layer_tar(base, [
        dir_entry("app"), dir_entry("app/data"),
        file_entry("app/data/old.log", b"old log\n"),
        file_entry("app/data/base.txt", b"base\n"),
        dir_entry("etc"),
        file_entry("etc/keep.cfg", b"keep=1\n"),
        file_entry("etc/old.cfg", b"old=1\n"),
    ])
    l1 = tmp_path / "l1.tar"
    build_layer_tar(l1, [
        file_entry("app/data/junk.txt", b"junk\n"),
        dir_entry("app/data/tmpdir"),
        file_entry("app/data/tmpdir/scratch.bin", b"scratch"),
        file_entry("app/server.bin", b"binary-v1", mode=0o755),
        file_entry("etc/old.cfg", b"old=2\n"),  # modifies base file
    ])
    l2 = tmp_path / "l2.tar"
    build_layer_tar(l2, [
        opaque_entry("app/data"),                      # mask base+L1 contents
        file_entry("app/data/fresh.txt", b"fresh\n"),  # ...then add new files
        whiteout_entry("etc/old.cfg"),                 # delete modified file
        file_entry("etc/new.cfg", b"new=1\n"),
        file_entry("app/server.bin", b"binary-v2", mode=0o755),
    ])

    merged = check_equivalent(tmp_path, base, l1, l2)

    names = layer_entry_names(merged)
    # masked lower-layer content must not leak into the merged layer
    assert "app/data/junk.txt" not in names
    assert "app/data/tmpdir" not in names
    assert "app/data/old.log" not in names
    # the opaque marker and the new file must survive
    assert "app/data/.wh..wh..opq" in names
    assert "app/data/fresh.txt" in names
    # deletion of the L1-modified file is kept as a whiteout
    assert "etc/.wh.old.cfg" in names
    assert "etc/new.cfg" in names

    tree = expand_stack([base, merged])
    assert sorted(tree.nodes) == [
        "", "app", "app/data", "app/data/fresh.txt", "app/server.bin",
        "etc", "etc/keep.cfg", "etc/new.cfg",
    ]


def test_whiteout_of_file_created_in_lower_layer(tmp_path):
    base = tmp_path / "base.tar"
    build_layer_tar(base, [file_entry("keep.txt", b"k")])
    l1 = tmp_path / "l1.tar"
    build_layer_tar(l1, [file_entry("temp.txt", b"t")])
    l2 = tmp_path / "l2.tar"
    build_layer_tar(l2, [whiteout_entry("temp.txt")])

    merged = check_equivalent(tmp_path, base, l1, l2)
    # conservative: the whiteout stays so the deletion also applies to the
    # base layers (which might contain temp.txt — we cannot know)
    assert ".wh.temp.txt" in layer_entry_names(merged)
    assert "temp.txt" not in layer_entry_names(merged)


def test_whiteout_then_readd_in_upper_layer(tmp_path):
    base = tmp_path / "base.tar"
    build_layer_tar(base, [file_entry("cfg", b"base-cfg")])
    l1 = tmp_path / "l1.tar"
    build_layer_tar(l1, [whiteout_entry("cfg")])
    l2 = tmp_path / "l2.tar"
    build_layer_tar(l2, [file_entry("cfg", b"new-cfg")])

    merged = check_equivalent(tmp_path, base, l1, l2)
    names = layer_entry_names(merged)
    assert "cfg" in names and ".wh.cfg" not in names


def test_opaque_in_lower_layer_is_preserved(tmp_path):
    base = tmp_path / "base.tar"
    build_layer_tar(base, [
        dir_entry("d"), file_entry("d/from-base", b"b"),
    ])
    l1 = tmp_path / "l1.tar"
    build_layer_tar(l1, [opaque_entry("d"), file_entry("d/a", b"a")])
    l2 = tmp_path / "l2.tar"
    build_layer_tar(l2, [file_entry("d/b", b"b2")])

    merged = check_equivalent(tmp_path, base, l1, l2)
    names = layer_entry_names(merged)
    assert "d/.wh..wh..opq" in names
    assert "d/a" in names and "d/b" in names
    assert "d/from-base" not in names


def test_upper_layer_deletes_dir_populated_by_lower(tmp_path):
    base = tmp_path / "base.tar"
    build_layer_tar(base, [dir_entry("d"), file_entry("d/base-file", b"x")])
    l1 = tmp_path / "l1.tar"
    build_layer_tar(l1, [file_entry("d/x", b"1"), file_entry("d/y", b"2")])
    l2 = tmp_path / "l2.tar"
    build_layer_tar(l2, [whiteout_entry("d")])

    merged = check_equivalent(tmp_path, base, l1, l2)
    names = layer_entry_names(merged)
    assert names == [".wh.d"]


def test_metadata_and_ownership_preserved(tmp_path):
    base = tmp_path / "base.tar"
    build_layer_tar(base, [dir_entry("app")])
    l1 = tmp_path / "l1.tar"
    build_layer_tar(l1, [
        file_entry("app/secret", b"s3cret", mode=0o640, uid=1000, gid=2000),
        dir_entry("app/conf", mode=0o750, uid=1000, gid=2000),
        file_entry("app/conf/yaml", b"k: v", mode=0o600, uid=1000, gid=2000),
    ])
    l2 = tmp_path / "l2.tar"
    build_layer_tar(l2, [file_entry("app/readme", b"hi", mode=0o444, uid=65534, gid=65534)])

    merged = check_equivalent(tmp_path, base, l1, l2)
    tree = expand_stack([base, merged])
    assert tree.nodes["app/secret"].mode == 0o640
    assert (tree.nodes["app/secret"].uid, tree.nodes["app/secret"].gid) == (1000, 2000)
    assert tree.nodes["app/conf"].mode == 0o750
    assert tree.nodes["app/readme"].uid == 65534


def test_upper_version_of_file_wins(tmp_path):
    base = tmp_path / "base.tar"
    build_layer_tar(base, [file_entry("f", b"base")])
    l1 = tmp_path / "l1.tar"
    build_layer_tar(l1, [file_entry("f", b"v1")])
    l2 = tmp_path / "l2.tar"
    build_layer_tar(l2, [file_entry("f", b"v2", mode=0o755)])

    merged = check_equivalent(tmp_path, base, l1, l2)
    tree = expand_stack([base, merged])
    import hashlib

    assert tree.nodes["f"].content == (hashlib.sha256(b"v2").hexdigest(), 2)
    assert tree.nodes["f"].mode == 0o755


def test_file_replaces_dir_subtree(tmp_path):
    base = tmp_path / "base.tar"
    build_layer_tar(base, [file_entry("other", b"o")])
    l1 = tmp_path / "l1.tar"
    build_layer_tar(l1, [dir_entry("d"), file_entry("d/x", b"1")])
    l2 = tmp_path / "l2.tar"
    build_layer_tar(l2, [file_entry("d", b"now-a-file")])

    merged = check_equivalent(tmp_path, base, l1, l2)
    names = layer_entry_names(merged)
    assert "d" in names and "d/x" not in names


def test_duplicate_entries_within_one_layer_last_wins(tmp_path):
    base = tmp_path / "base.tar"
    build_layer_tar(base, [file_entry("f", b"base")])
    l1 = tmp_path / "l1.tar"
    build_layer_tar(l1, [file_entry("f", b"first"), file_entry("f", b"second")])
    l2 = tmp_path / "l2.tar"
    build_layer_tar(l2, [file_entry("g", b"g")])

    merged = check_equivalent(tmp_path, base, l1, l2)
    import hashlib

    tree = expand_stack([base, merged])
    assert tree.nodes["f"].content == (hashlib.sha256(b"second").hexdigest(), 6)


def test_empty_upper_layer(tmp_path):
    base = tmp_path / "base.tar"
    build_layer_tar(base, [file_entry("a", b"a")])
    l1 = tmp_path / "l1.tar"
    build_layer_tar(l1, [file_entry("b", b"b")])
    l2 = tmp_path / "l2.tar"
    build_layer_tar(l2, [])  # valid tar, zero entries

    merged = check_equivalent(tmp_path, base, l1, l2)
    assert layer_entry_names(merged) == ["b"]


def test_deep_nesting_and_multiple_whiteouts(tmp_path):
    base = tmp_path / "base.tar"
    build_layer_tar(base, [
        dir_entry("a"), dir_entry("a/b"), dir_entry("a/b/c"),
        file_entry("a/b/c/deep.txt", b"deep"),
        file_entry("a/b/c/keep.txt", b"keep"),
        file_entry("a/b/gone.txt", b"gone"),
    ])
    l1 = tmp_path / "l1.tar"
    build_layer_tar(l1, [
        file_entry("a/b/c/deep.txt", b"deep-v1"),
        dir_entry("a/b/c/sub"), file_entry("a/b/c/sub/leaf", b"leaf"),
    ])
    l2 = tmp_path / "l2.tar"
    build_layer_tar(l2, [
        whiteout_entry("a/b/c/sub"),       # delete L1's new subtree
        whiteout_entry("a/b/gone.txt"),    # delete a base file
        file_entry("a/b/c/deep.txt", b"deep-v2"),
    ])

    merged = check_equivalent(tmp_path, base, l1, l2)
    names = layer_entry_names(merged)
    assert "a/b/.wh.gone.txt" in names
    assert "a/b/c/.wh.sub" in names
    assert "a/b/c/sub/leaf" not in names


def test_unsupported_entry_type_rejected(tmp_path):
    l1 = tmp_path / "l1.tar"
    build_layer_tar(l1, [file_entry("ok", b"ok")])
    l2 = tmp_path / "l2.tar"
    # hand-craft a layer containing a symlink
    import io

    with tarfile.open(l2, "w") as tf:
        ti = tarfile.TarInfo("link")
        ti.type = tarfile.SYMTYPE
        ti.linkname = "/etc/passwd"
        tf.addfile(ti)

    with open(l1, "rb") as f1, open(l2, "rb") as f2:
        t1 = tarfile.open(fileobj=f1, mode="r:")
        t2 = tarfile.open(fileobj=f2, mode="r:")
        merger = LayerMerger()
        merger.add_layer(t1, 0)
        with pytest.raises(MergeError, match="unsupported tar entry type"):
            merger.add_layer(t2, 1)


def test_path_traversal_rejected(tmp_path):
    bad = tmp_path / "bad.tar"
    import io

    with tarfile.open(bad, "w") as tf:
        ti = tarfile.TarInfo("../escape")
        ti.size = 1
        tf.addfile(ti, io.BytesIO(b"x"))

    merger = LayerMerger()
    with open(bad, "rb") as f:
        tf = tarfile.open(fileobj=f, mode="r:")
        with pytest.raises(MergeError, match="escapes root"):
            merger.add_layer(tf, 0)


def merge_layers_full(tmp_path: Path, l1: Path, l2: Path, synthesize: bool) -> Path:
    out = tmp_path / f"merged-{synthesize}.tar"
    with open(l1, "rb") as f1, open(l2, "rb") as f2:
        t1 = tarfile.open(fileobj=f1, mode="r:")
        t2 = tarfile.open(fileobj=f2, mode="r:")
        merger = LayerMerger(synthesize_implicit_dirs=synthesize)
        merger.add_layer(t1, 0)
        merger.add_layer(t2, 1)
        with open(out, "wb") as fout:
            merger.write_merged([t1, t2], fout)
    return out


def test_implicit_dirs_synthesized_when_no_base(tmp_path):
    """Two-layer image: a dir created only implicitly whose contents are all
    deleted again must still exist after merging (it did before)."""
    l1 = tmp_path / "l1.tar"
    build_layer_tar(l1, [file_entry("etc/old.cfg", b"x")])  # no explicit etc/ entry
    l2 = tmp_path / "l2.tar"
    build_layer_tar(l2, [whiteout_entry("etc/old.cfg")])

    merged = merge_layers_full(tmp_path, l1, l2, synthesize=True)
    names = layer_entry_names(merged)
    assert "etc" in names and "etc/.wh.old.cfg" in names
    expand_stack([merged]).assert_same_as(expand_stack([l1, l2]))


def test_implicit_dirs_not_synthesized_with_base(tmp_path):
    """With base layers the merged layer must not gain dir entries that could
    clobber base metadata; the base provides the directories."""
    base = tmp_path / "base.tar"
    build_layer_tar(base, [dir_entry("etc", mode=0o700, uid=5, gid=6)])
    l1 = tmp_path / "l1.tar"
    build_layer_tar(l1, [file_entry("etc/old.cfg", b"x")])
    l2 = tmp_path / "l2.tar"
    build_layer_tar(l2, [whiteout_entry("etc/old.cfg")])

    merged = merge_layers_full(tmp_path, l1, l2, synthesize=False)
    assert layer_entry_names(merged) == ["etc/.wh.old.cfg"]
    tree = expand_stack([base, merged])
    assert tree.nodes["etc"].mode == 0o700  # base metadata untouched
    expand_stack([base, merged]).assert_same_as(expand_stack([base, l1, l2]))


def test_memory_usage_independent_of_file_size(tmp_path):
    """A 32 MiB file must be merged with bounded memory (streaming)."""
    import os

    big = os.urandom(32 * 1024 * 1024)
    base = tmp_path / "base.tar"
    build_layer_tar(base, [file_entry("seed", b"s")])
    l1 = tmp_path / "l1.tar"
    build_layer_tar(l1, [file_entry("big.bin", big)])
    l2 = tmp_path / "l2.tar"
    build_layer_tar(l2, [file_entry("small.txt", b"small")])

    del big  # only the on-disk tars should matter now
    tracemalloc.start()
    try:
        merged = merge_layers(tmp_path, l1, l2)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 16 * 1024 * 1024, f"peak memory {peak / 2**20:.1f} MiB too high"

    check_equivalent(tmp_path, base, l1, l2)
    # merged layer carries the big file exactly once
    assert (tmp_path / "merged.tar").stat().st_size > 32 * 1024 * 1024
