from adaptive_core.state import atomic_write_text


def test_atomic_write_creates_parent_replaces_content_and_leaves_no_temp(tmp_path):
    target = tmp_path / "nested" / "state.json"
    atomic_write_text(target, '{"ok": true}\n')
    assert target.read_text(encoding="utf-8") == '{"ok": true}\n'

    target.write_text("old\n", encoding="utf-8")
    atomic_write_text(target, "new\n")
    assert target.read_text(encoding="utf-8") == "new\n"
    assert list(target.parent.glob("state.json.*")) == []
