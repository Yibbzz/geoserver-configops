"""Where configops finds a secret: the environment, secrets.yaml, then a mounted Kubernetes Secret."""

from configops import secrets


def test_secret_from_environment_then_secrets_file(tmp_path, monkeypatch):
    assert secrets.env_name("geoserver-admin", "password") == "CONFIGOPS_GEOSERVER_ADMIN_PASSWORD"
    (tmp_path / "secrets.yaml").write_text("geoserver-admin: { password: from-file }\n")
    monkeypatch.delenv("CONFIGOPS_GEOSERVER_ADMIN_PASSWORD", raising=False)
    assert secrets.lookup("geoserver-admin", "password", tmp_path) == "from-file"
    monkeypatch.setenv("CONFIGOPS_GEOSERVER_ADMIN_PASSWORD", "from-env")
    assert secrets.lookup("geoserver-admin", "password", tmp_path) == "from-env"
    assert secrets.lookup("other", "password", tmp_path) is None


def test_secret_from_a_mounted_kubernetes_secret(tmp_path, monkeypatch):
    (tmp_path / "database").mkdir()
    (tmp_path / "database" / "password").write_text("s3cret\n")
    monkeypatch.setenv("CONFIGOPS_SECRETS_DIR", str(tmp_path))
    monkeypatch.delenv("CONFIGOPS_DATABASE_PASSWORD", raising=False)
    assert secrets.lookup("database", "password") == "s3cret"
    assert secrets.lookup("database", "host") is None
    monkeypatch.setenv("CONFIGOPS_DATABASE_PASSWORD", "override")
    assert secrets.lookup("database", "password") == "override"
