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
