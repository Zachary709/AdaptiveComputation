from __future__ import annotations

from adaptive_core.models import NodeConfig
from adaptive_service.collector.probe import _merge_devices


def device_document(
    alias: str,
    mac: str | None,
    *,
    hostname: str = "host",
    reachable: bool = True,
    address: str = "100.64.0.10",
) -> dict:
    return {
        "alias": alias,
        "mac": mac,
        "hostname": hostname,
        "reachable": reachable,
        "stale": False,
        "errors": [] if reachable else ["connection refused"],
        "ssh": {"address": address, "user": "u", "port": 22},
        "hardware": {},
        "software": {},
        "configured": {},
    }


def candidate(alias: str, address: str, port: int = 22) -> NodeConfig:
    return NodeConfig(alias=alias, address=address, user="u", port=port)


def test_devices_with_same_mac_merge_and_prefer_tailscale():
    nodes = [
        candidate("gpu-a-frp", "203.0.113.10", 10022),
        candidate("gpu-a", "100.64.0.10"),
    ]
    documents = [
        device_document("gpu-a-frp", "aa:bb:cc:dd:ee:ff", address="203.0.113.10"),
        device_document("gpu-a", "aa:bb:cc:dd:ee:ff"),
    ]

    merged = _merge_devices(nodes, documents)

    assert len(merged) == 1
    assert merged[0]["alias"] == "gpu-a"
    assert merged[0]["channel"] == "tailscale"
    assert merged[0]["mac"] == "aa:bb:cc:dd:ee:ff"
    assert merged[0]["connection"]["address"] == "100.64.0.10"
    assert [item["channel"] for item in merged[0]["candidates"]] == ["tailscale", "frp"]


def test_devices_use_frp_when_tailscale_is_unreachable():
    nodes = [
        candidate("gpu-a-frp", "203.0.113.10", 10022),
        candidate("gpu-a", "100.64.0.10"),
    ]
    documents = [
        device_document("gpu-a-frp", "aa:bb:cc:dd:ee:ff", address="203.0.113.10"),
        device_document("gpu-a", "aa:bb:cc:dd:ee:ff", reachable=False),
    ]

    merged = _merge_devices(nodes, documents)

    assert len(merged) == 1
    assert merged[0]["alias"] == "gpu-a"
    assert merged[0]["channel"] == "frp"
    assert merged[0]["connection"]["address"] == "203.0.113.10"
    assert merged[0]["connection"]["port"] == 10022


def test_devices_without_mac_use_hostname():
    nodes = [
        candidate("gpu-a", "100.64.0.11"),
        candidate("gpu-a-frp", "203.0.113.10", 10023),
        candidate("gpu-b", "100.64.0.12"),
    ]
    documents = [
        device_document("gpu-a", None, hostname="host-a"),
        device_document("gpu-a-frp", None, hostname="host-a", address="203.0.113.10"),
        device_document("gpu-b", None, hostname="host-b"),
    ]

    merged = _merge_devices(nodes, documents)

    assert len(merged) == 2
    assert {item["alias"] for item in merged} == {"gpu-a", "gpu-b"}


def test_unreachable_aliases_with_same_endpoint_merge():
    nodes = [
        candidate("desktop", "100.64.0.20", 8022),
        candidate("desktop-alt", "100.64.0.20", 8022),
        candidate("laptop", "100.64.0.20", 8022),
    ]
    documents = [
        device_document("desktop", None, hostname="", reachable=False),
        device_document("desktop-alt", None, hostname="", reachable=False),
        device_document("laptop", None, hostname="", reachable=False),
    ]

    merged = _merge_devices(nodes, documents)

    assert len(merged) == 1
    assert merged[0]["alias"] == "desktop"
    assert len(merged[0]["candidates"]) == 3


def test_renamed_alias_replaces_stale_known_alias():
    nodes = [candidate("gpu-b", "100.64.0.13")]
    documents = [device_document("gpu-b", "aa:bb:cc:dd:ee:01")]

    merged = _merge_devices(
        nodes,
        documents,
        {"aa:bb:cc:dd:ee:01": "big"},
    )

    assert merged[0]["alias"] == "gpu-b"


def test_recorded_frp_alias_does_not_replace_the_tailscale_alias():
    nodes = [
        candidate("gpu-a", "100.64.0.10"),
        candidate("gpu-a-frp", "203.0.113.10", 10022),
    ]
    documents = [
        device_document("gpu-a", "aa:bb:cc:dd:ee:02"),
        device_document("gpu-a-frp", "aa:bb:cc:dd:ee:02", address="203.0.113.10"),
    ]

    merged = _merge_devices(
        nodes,
        documents,
        {"aa:bb:cc:dd:ee:02": "gpu-a-frp"},
    )

    assert len(merged) == 1
    assert merged[0]["alias"] == "gpu-a"


def test_failed_probe_still_matches_the_recorded_mac():
    nodes = [
        candidate("gpu-a", "100.64.0.10"),
        candidate("gpu-a-frp", "203.0.113.10", 10022),
    ]
    documents = [
        device_document("gpu-a", None, reachable=False),
        device_document("gpu-a-frp", "aa:bb:cc:dd:ee:03", address="203.0.113.10"),
    ]

    merged = _merge_devices(
        nodes,
        documents,
        known_macs={"gpu-a": "aa:bb:cc:dd:ee:03"},
    )

    assert len(merged) == 1
    assert merged[0]["alias"] == "gpu-a"
    assert merged[0]["channel"] == "frp"
    assert merged[0]["connection"]["address"] == "203.0.113.10"
