"""Workspace resources, layer group details and global styles: validation and naming."""

from configops.loader import load
from configops.validate import validate

PROJECT = """\
apiVersion: configops/v1alpha1
kind: Project
metadata: { name: t }
spec:
  geoserver: { url: http://gs:8080/geoserver-cloud, credentials: { secret: gs } }
  database: { jndi: java:comp/env/jdbc/x, connection: { secret: pg } }
  workspace: uk
"""


def errors(tmp_path, extra):
    (tmp_path / "styles").mkdir(exist_ok=True)
    (tmp_path / "styles" / "g.sld").write_text("<sld/>")
    (tmp_path / "project.yaml").write_text(PROJECT + "---\n" + extra)
    return [p.message for p in validate(load(tmp_path)) if p.severity == "error"]


def test_valid(tmp_path):
    assert errors(tmp_path, """\
apiVersion: configops/v1alpha1
kind: Workspace
metadata: { name: uk }
spec: { namespace: "https://example.org/uk", isolated: true, default: true }
---
apiVersion: configops/v1alpha1
kind: Style
metadata: { name: green }
spec: { file: styles/g.sld, global: true }
---
apiVersion: configops/v1alpha1
kind: Layer
metadata: { name: a }
spec: { table: { schema: s, name: t }, style: green }
---
apiVersion: configops/v1alpha1
kind: LayerGroup
metadata: { name: g }
spec: { layers: [a], enabled: false, advertised: false, metadataLinks: [ { url: "https://example.org/g.xml" } ] }
""") == []


def test_one_default_workspace_and_global_style_rules(tmp_path):
    messages = errors(tmp_path, """\
apiVersion: configops/v1alpha1
kind: Workspace
metadata: { name: a }
spec: { default: true }
---
apiVersion: configops/v1alpha1
kind: Workspace
metadata: { name: b }
spec: { default: true }
---
apiVersion: configops/v1alpha1
kind: Style
metadata: { name: polygon }
spec: { file: styles/g.sld, global: true }
---
apiVersion: configops/v1alpha1
kind: Style
metadata: { name: other }
spec: { file: styles/g.sld, global: true, workspace: uk }
""")
    assert "spec.default: Workspace a is already the default" in messages
    assert "polygon is one of GeoServer's own styles: use it without defining it" in messages
    assert "spec.workspace: a global style is in no workspace" in messages
