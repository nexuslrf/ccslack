from ccslack.bot import create_app
from ccslack.config import config


def test_no_proxy_by_default(monkeypatch):
    monkeypatch.setattr(config, "proxy", "")
    app = create_app()
    assert getattr(app.client, "proxy", None) in (None, "")


def test_proxy_threads_into_web_client(monkeypatch):
    monkeypatch.setattr(config, "proxy", "http://127.0.0.1:7897")
    app = create_app()
    assert app.client.proxy == "http://127.0.0.1:7897"
