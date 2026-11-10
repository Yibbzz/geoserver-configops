"""Render the Helm chart and check the manifests it produces."""

import shutil
import subprocess

import pytest
import yaml

from conftest import ROOT, by_kind, containers, helm_template

GEOSERVER_TAG = "3.0.2.1"
ENABLED_SERVICES = {"gateway", "webui", "rest", "wfs", "wms", "wcs", "gwc"}
DISABLED_SERVICES = {"wps", "acl"}


def geoserver_images(manifests):
    return {
        name: containers(d)[0]["image"]
        for name, d in by_kind(manifests, "Deployment").items()
        if name.startswith("geoserver-gsc-")
    }


def test_helm_lint():
    result = subprocess.run(["helm", "lint", str(ROOT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_default_deploys_expected_services():
    deployments = by_kind(helm_template(), "Deployment")
    names = {n.removeprefix("geoserver-gsc-") for n in deployments}
    assert ENABLED_SERVICES <= names
    assert not DISABLED_SERVICES & names


def test_default_images_come_from_docker_hub_with_one_tag():
    for name, image in geoserver_images(helm_template()).items():
        assert image.startswith("docker.io/geoservercloud/"), name
        assert image.endswith(f":{GEOSERVER_TAG}"), name


def test_local_registry_override_repoints_every_image():
    manifests = helm_template("-f", str(ROOT / "values.yaml"), "-f", str(ROOT / "values-local-registry.yaml"))
    registry = "k3d-k3d-registry.localhost:5000/"
    for name, image in geoserver_images(manifests).items():
        assert image.startswith(registry), name
    cronjob = by_kind(manifests, "CronJob")["gisdata-sync"]
    assert containers(cronjob)[0]["image"].startswith(registry)


def test_sync_cronjob_uses_values():
    manifests = helm_template(
        "--set", "sync.schedule=30 4 * * 0",
        "--set", "sync.image.repository=example.com/gisdata",
        "--set", "sync.image.tag=1.2.3",
    )
    cronjob = by_kind(manifests, "CronJob")["gisdata-sync"]
    assert cronjob["spec"]["schedule"] == "30 4 * * 0"
    container = containers(cronjob)[0]
    assert container["image"] == "example.com/gisdata:1.2.3"
    assert container["args"] == ["sync", "-C", "/project"]


def test_project_files_are_in_a_configmap_at_their_paths():
    manifests = helm_template()
    data = by_kind(manifests, "ConfigMap")["gisdata-project"]["data"]
    pod = by_kind(manifests, "CronJob")["gisdata-sync"]["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    volume = next(v for v in pod["volumes"] if v["name"] == "project")
    paths = {item["path"]: data[item["key"]] for item in volume["configMap"]["items"]}
    project = ROOT / "examples" / "uk-basemap"
    expected = {
        str(f.relative_to(project)) for f in project.rglob("*")
        if f.suffix in (".yaml", ".yml", ".sld", ".mbstyle", ".css") and f.name not in ("secrets.yaml", "secrets.yml")
    }
    assert set(paths) == expected
    assert paths["project.yaml"] == (project / "project.yaml").read_text()
    assert paths["styles/os-road-link.sld"] == (project / "styles" / "os-road-link.sld").read_bytes().decode()
    mounts = {m["name"]: m["mountPath"] for m in containers(by_kind(manifests, "CronJob")["gisdata-sync"])[0]["volumeMounts"]}
    assert mounts["project"] == "/project"


def test_secrets_are_mounted_as_files_and_may_be_missing():
    manifests = helm_template("--set", "sync.secrets={geoserver-pgconfig-secret,os-api}")
    cronjob = by_kind(manifests, "CronJob")["gisdata-sync"]
    pod = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    secrets = {v["secret"]["secretName"]: v["secret"] for v in pod["volumes"] if "secret" in v}
    assert set(secrets) == {"geoserver-pgconfig-secret", "os-api"}
    assert all(s["optional"] for s in secrets.values())
    container = containers(cronjob)[0]
    mounts = {m["mountPath"] for m in container["volumeMounts"]}
    assert {"/etc/gisdata/secrets/geoserver-pgconfig-secret", "/etc/gisdata/secrets/os-api"} <= mounts
    assert {"name": "GISDATA_SECRETS_DIR", "value": "/etc/gisdata/secrets"} in container["env"]


def test_secrets_yaml_never_reaches_the_configmap(tmp_path):
    """A secrets.yaml next to the project stays out, even if .helmignore is changed."""
    secrets = ROOT / "examples" / "uk-basemap" / "secrets.yaml"
    assert not secrets.exists()
    secrets.write_text("os-api: { apiKey: do-not-ship }\n")
    try:
        result = subprocess.run(["helm", "template", "geoserver", str(ROOT)], capture_output=True, text=True, check=True)
    finally:
        secrets.unlink()
    assert "do-not-ship" not in result.stdout


def test_a_project_directory_without_files_fails_clearly():
    if shutil.which("helm") is None:
        pytest.skip("helm is not installed")
    result = subprocess.run(["helm", "template", "geoserver", str(ROOT), "--set", "sync.project=nowhere"],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert "sync.project: no project files found in nowhere" in result.stderr


def test_sync_can_be_disabled():
    manifests = helm_template("--set", "sync.enabled=false")
    assert not by_kind(manifests, "CronJob")
    assert "gisdata-project" not in by_kind(manifests, "ConfigMap")


def test_jndi_config_is_rendered_and_mounted():
    manifests = helm_template()
    assert "geoserver-jndi-config" in by_kind(manifests, "ConfigMap")
    for name, deployment in by_kind(manifests, "Deployment").items():
        if not name.startswith("geoserver-gsc-"):
            continue
        mounts = {m["mountPath"] for m in containers(deployment)[0]["volumeMounts"]}
        assert "/etc/geoserver/jndi.yml" in mounts, name


def test_every_referenced_secret_key_is_in_the_example():
    """Secrets the chart doesn't create itself must be in geoserver-secrets.example.yaml."""
    example = {
        doc["metadata"]["name"]: set(doc.get("stringData") or {})
        for doc in yaml.safe_load_all((ROOT / "geoserver-secrets.example.yaml").read_text())
        if doc
    }
    manifests = helm_template()
    created_by_chart = set(by_kind(manifests, "Secret"))
    missing = set()
    for manifest in manifests:
        if manifest["kind"] not in ("Deployment", "StatefulSet", "CronJob"):
            continue
        if manifest["kind"] == "CronJob":
            pod = manifest["spec"]["jobTemplate"]["spec"]["template"]["spec"]
            for volume in pod.get("volumes", []):
                name = volume.get("secret", {}).get("secretName")
                if name and name not in created_by_chart and name not in example:
                    missing.add(f'{name} (mounted by {manifest["metadata"]["name"]})')
        for container in containers(manifest):
            for env in container.get("env", []):
                ref = env.get("valueFrom", {}).get("secretKeyRef")
                if not ref or ref["name"] in created_by_chart:
                    continue
                if ref["key"] not in example.get(ref["name"], set()):
                    missing.add(f'{ref["name"]}/{ref["key"]} (used by {manifest["metadata"]["name"]})')
    assert not missing, missing


def test_pgconfig_jndi_datasource_sets_its_schema():
    """GeoServer Cloud 3 reads the catalog through this datasource; without a
    schema it looks for its tables in the wrong place and fails to start."""
    jndi = yaml.safe_load(by_kind(helm_template(), "ConfigMap")["geoserver-jndi-config"]["data"]["jndi.yml"])
    pgconfig = jndi["jndi"]["datasources"]["pgconfig"]
    assert pgconfig["schema"] == "${PGCONFIG_SCHEMA}"
