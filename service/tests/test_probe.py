from __future__ import annotations

import base64

from adaptive_core.models import NodeConfig
from adaptive_service.collector.probe import parse_inventory_output


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _document(*records: str) -> str:
    return "\n".join(
        [
            "__OPENSMI_INVENTORY_BEGIN__",
            *[f"__OPENSMI_INVENTORY__{record}" for record in records],
            "__OPENSMI_INVENTORY_END__",
        ]
    )


def test_parse_inventory_output_reads_tmux():
    node = NodeConfig(alias="n1", address="127.0.0.1", user="u")
    stdout = _document(
        f"meta|hostname|{_b64('n1')}",
        f"meta|tmux_path|{_b64('/usr/bin/tmux')}",
        f"meta|tmux|{_b64('tmux 3.0a')}",
    )

    doc = parse_inventory_output(node, stdout)

    assert doc["software"]["tmux"] == {
        "installed": True,
        "path": "/usr/bin/tmux",
        "version": "tmux 3.0a",
    }


def test_parse_inventory_output_marks_missing_tmux():
    node = NodeConfig(alias="n1", address="127.0.0.1", user="u")
    stdout = _document(f"meta|hostname|{_b64('n1')}")

    doc = parse_inventory_output(node, stdout)

    assert doc["software"]["tmux"] == {
        "installed": False,
        "path": None,
        "version": None,
    }
