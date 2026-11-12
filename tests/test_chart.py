"""Render the Helm chart and check the manifests it produces."""

import base64
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


def test_sync_cronjob_uses_values():
    manifests = helm_template(
        "--set", "configops.sync.schedule=30 4 * * 0",
        "--set", "configops.image.repository=example.com/configops",
        "--set", "configops.image.tag=1.2.3",
    )
    cronjob = by_kind(manifests, "CronJob")["configops-sync"]
    assert cronjob["spec"]["schedule"] == "30 4 * * 0"
    container = containers(cronjob)[0]
    assert container["image"] == "example.com/configops:1.2.3"
    assert container["args"] == ["sync", "-C", "/project"]


def test_project_files_are_in_a_configmap_at_their_paths():
    manifests = helm_template()
    data = by_kind(manifests, "ConfigMap")["configops-project"]["data"]
    pod = by_kind(manifests, "CronJob")["configops-sync"]["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    volume = next(v for v in pod["volumes"] if v["name"] == "project")
    paths = {item["path"]: data[item["key"]] for item in volume["configMap"]["items"]}
    project = ROOT / "examples" / "uk-basemap"
    expected = {
        str(f.relative_to(project)) for f in project.rglob("*")
        if f.suffix in (".yaml", ".yml", ".sld", ".mbstyle", ".css", ".ftl", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".ttf", ".otf")
        and f.name not in ("secrets.yaml", "secrets.yml")
    }
    assert set(paths) == expected
    assert paths["project.yaml"] == (project / "project.yaml").read_text()
    assert paths["styles/os-road-link.sld"] == (project / "styles" / "os-road-link.sld").read_bytes().decode()
    mounts = {m["name"]: m["mountPath"] for m in containers(by_kind(manifests, "CronJob")["configops-sync"])[0]["volumeMounts"]}
    assert mounts["project"] == "/project"


def test_images_fonts_and_templates_are_in_the_configmap_images_as_binary():
    manifests = helm_template("--set", "configops.project=examples/smoke")
    configmap = by_kind(manifests, "ConfigMap")["configops-project"]
    pod = by_kind(manifests, "CronJob")["configops-sync"]["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    items = {i["path"]: i["key"] for i in next(v for v in pod["volumes"] if v["name"] == "project")["configMap"]["items"]}
    project = ROOT / "examples" / "smoke"
    assert set(items) == {"project.yaml", "styles/squares.sld", "styles/marks/dot.png", "templates/squares/content.ftl"}
    assert base64.b64decode(configmap["binaryData"][items["styles/marks/dot.png"]]) == (project / "styles/marks/dot.png").read_bytes()
    assert configmap["data"][items["templates/squares/content.ftl"]] == (project / "templates/squares/content.ftl").read_text()


def test_secrets_are_mounted_as_files_and_may_be_missing():
    manifests = helm_template("--set", "configops.secrets={geoserver-pgconfig-secret,os-api}")
    cronjob = by_kind(manifests, "CronJob")["configops-sync"]
    pod = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    secrets = {v["secret"]["secretName"]: v["secret"] for v in pod["volumes"] if "secret" in v}
    assert set(secrets) == {"geoserver-pgconfig-secret", "os-api"}
    assert all(s["optional"] for s in secrets.values())
    container = containers(cronjob)[0]
    mounts = {m["mountPath"] for m in container["volumeMounts"]}
    assert {"/etc/configops/secrets/geoserver-pgconfig-secret", "/etc/configops/secrets/os-api"} <= mounts
    assert {"name": "CONFIGOPS_SECRETS_DIR", "value": "/etc/configops/secrets"} in container["env"]


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
    result = subprocess.run(["helm", "template", "geoserver", str(ROOT), "--set", "configops.project=nowhere"],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert "configops.project: no project files found in nowhere" in result.stderr


def test_apply_job_runs_each_release_and_waits_for_geoserver():
    manifests = helm_template("--set", "configops.apply.waitSeconds=60")
    [job] = by_kind(manifests, "Job").values()
    assert job["metadata"]["name"] == "configops-apply-1"  # the release revision: a new Job each upgrade
    assert containers(job)[0]["args"] == ["apply", "-C", "/project", "--wait", "60"]
    pod = job["spec"]["template"]["spec"]
    assert pod["restartPolicy"] == "Never" and pod["securityContext"]["runAsNonRoot"] is True
    assert "helm.sh/hook" not in job["metadata"].get("annotations", {})  # Helm doesn't block on it
    pruning = by_kind(helm_template("--set", "configops.apply.prune=true"), "Job")
    assert containers(next(iter(pruning.values())))[0]["args"][-1] == "--prune"


def test_apply_and_sync_share_one_pod():
    manifests = helm_template()
    [job] = by_kind(manifests, "Job").values()
    cron = by_kind(manifests, "CronJob")["configops-sync"]
    apply_pod = job["spec"]["template"]["spec"]
    sync_pod = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    for pod in (apply_pod, sync_pod):
        pod["containers"][0].pop("args")
    assert apply_pod == sync_pod


def test_drift_check_runs_plan_and_passes_only_waiting_layers():
    manifests = helm_template("--set", "configops.drift.schedule=0 * * * *")
    cron = by_kind(manifests, "CronJob")["configops-drift"]
    assert cron["spec"]["schedule"] == "0 * * * *"
    assert cron["spec"]["jobTemplate"]["spec"]["backoffLimit"] == 0
    container = containers(cron)[0]
    assert container["command"] == ["sh", "-c"]
    [script] = container["args"]
    assert script.startswith("configops plan -C /project;") and '"$rc" -eq 3' in script
    drift_pod = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    sync_pod = by_kind(manifests, "CronJob")["configops-sync"]["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    for pod in (drift_pod, sync_pod):
        pod["containers"][0].pop("args")
        pod["containers"][0].pop("command", None)
    assert drift_pod == sync_pod  # same image, project and secrets


def test_drift_script_exit_codes():
    """Run the CronJob's script with a stand-in configops: drift and errors fail it, waiting layers don't."""
    [script] = containers(by_kind(helm_template(), "CronJob")["configops-drift"])[0]["args"]
    for plan_code, job_code in [(0, 0), (1, 1), (2, 2), (3, 0)]:
        stand_in = f"configops() {{ return {plan_code}; }}; "
        assert subprocess.run(["sh", "-c", stand_in + script]).returncode == job_code, plan_code


def test_apply_sync_and_drift_can_be_disabled():
    only_apply = helm_template("--set", "configops.sync.enabled=false", "--set", "configops.drift.enabled=false")
    assert not by_kind(only_apply, "CronJob") and by_kind(only_apply, "Job")
    no_drift = helm_template("--set", "configops.drift.enabled=false")
    assert set(by_kind(no_drift, "CronJob")) == {"configops-sync"}
    none = helm_template("--set", "configops.sync.enabled=false", "--set", "configops.apply.enabled=false",
                         "--set", "configops.drift.enabled=false")
    assert not by_kind(none, "CronJob") and not by_kind(none, "Job")
    assert "configops-project" not in by_kind(none, "ConfigMap")


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
        if manifest["kind"] not in ("Deployment", "StatefulSet", "CronJob", "Job"):
            continue
        spec = manifest["spec"]["jobTemplate"]["spec"] if manifest["kind"] == "CronJob" else manifest["spec"]
        for volume in spec["template"]["spec"].get("volumes", []):
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


def test_no_data_volume_unless_asked():
    manifests = helm_template()
    assert not by_kind(manifests, "PersistentVolumeClaim")
    for name, deployment in by_kind(manifests, "Deployment").items():
        assert "/data" not in {m["mountPath"] for m in containers(deployment)[0].get("volumeMounts", [])}, name


def test_data_volume_is_mounted_in_every_service_and_configops():
    manifests = helm_template("-f", str(ROOT / "values-data-volume.yaml"), "--set", "dataVolume.size=5Gi")
    claim = by_kind(manifests, "PersistentVolumeClaim")["geoserver-data"]
    assert claim["spec"]["resources"]["requests"]["storage"] == "5Gi"
    assert claim["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
    pods = {n: d["spec"]["template"]["spec"] for n, d in by_kind(manifests, "Deployment").items() if n.startswith("geoserver-gsc-")}
    [job] = by_kind(manifests, "Job").values()
    pods["apply"] = job["spec"]["template"]["spec"]
    for n, cron in by_kind(manifests, "CronJob").items():
        pods[n] = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert len(pods) == 7 + 3  # seven GeoServer services, the apply Job, sync and drift
    for name, pod in pods.items():
        claims = {v["name"]: v["persistentVolumeClaim"]["claimName"] for v in pod["volumes"] if "persistentVolumeClaim" in v}
        mounted = {m["mountPath"]: m["name"] for m in pod["containers"][0]["volumeMounts"]}
        assert claims.get(mounted.get("/data")) == "geoserver-data", name


def test_app_schema_is_off_unless_turned_on():
    def switched_on(manifests) -> set[str]:
        return {n for n, d in by_kind(manifests, "Deployment").items()
                if any(e["name"] == "GEOSERVER_EXTENSION_APPSCHEMA_ENABLED" and e.get("value") == "true"
                       for e in containers(d)[0].get("env", []))}
    assert switched_on(helm_template()) == set()
    on = switched_on(helm_template("-f", str(ROOT / "values-app-schema.yaml")))
    assert {"geoserver-gsc-rest", "geoserver-gsc-wms", "geoserver-gsc-wfs", "geoserver-gsc-webui"} <= on
