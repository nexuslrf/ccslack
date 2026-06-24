from ccslack import fleet_state
from ccslack.config import config
from ccslack.router import Router


def test_standalone_defaults(monkeypatch):
    monkeypatch.setattr(config, "host_name", "solo")
    assert fleet_state.is_fleet() is False
    assert fleet_state.hosts() == ["solo"]
    assert fleet_state.remote_channels() == {}


def test_fleet_hosts_and_remote_channels(monkeypatch):
    monkeypatch.setattr(config, "host_name", "r0")
    r = Router(local_host="r0")
    r.set_host_channels("gpu1", ["C1", "C2"])
    r.set_host_channels("gpu2", [])  # connected, no sessions
    r.bind("r0", "C_LOCAL")  # the router's own session
    fleet_state.install_router(r)

    assert fleet_state.is_fleet() is True
    assert fleet_state.hosts() == ["gpu1", "gpu2", "r0"]
    # Only other hosts' channels are "remote"; the router's own is excluded.
    assert fleet_state.remote_channels() == {"C1": "gpu1", "C2": "gpu1"}


def test_fleet_status_rows(monkeypatch):
    from ccslack.thread_router import thread_router

    monkeypatch.setattr(config, "host_name", "r0")
    r = Router(local_host="r0")
    r.set_host_channels("gpu1", ["C1", "C2"])  # connected, 2 sessions
    # gpu2 configured but never connected (not in registry).
    fleet_state.install_router(r)
    fleet_state.set_workers([("gpu1", "user@gpu1"), ("gpu2", "gpu2-alias")])
    thread_router.bind_channel("C_LOCAL", "@1")  # router's own session

    rows = {row["host"]: row for row in fleet_state.fleet_status()}
    assert rows["r0"]["role"] == "router" and rows["r0"]["sessions"] == 1
    assert rows["gpu1"]["connected"] is True and rows["gpu1"]["sessions"] == 2
    assert rows["gpu2"]["connected"] is False and rows["gpu2"]["sessions"] == 0
    assert rows["gpu2"]["ssh"] == "gpu2-alias"


def test_fleet_status_empty_when_not_router():
    assert fleet_state.fleet_status() == []
