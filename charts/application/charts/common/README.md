## Common functions

This offer all the function from a default Helm chart and in addition:
`common.podConfig`, `common.containerConfig`

The signature of `common.name`, `common.fullname`, `common.labels`, `common.selectorLabels` and
`common.serviceAccountName` is changed to add the `serviceName`.

Then you can quickly define a deployment like this:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "common.fullname" ( dict "root" . "service" .Values ) }}
  {{- include "common.metadata" ( dict "root" . "service" .Values ) | nindent 2 }}
spec:
  replicas: {{ .Values.replicaCount }}
  strategy:
    type: RollingUpdate
  selector:
    matchLabels: {{- include "common.selectorLabels" ( dict "root" . "service" .Values ) | nindent 6 }}
  template:
    metadata: {{- include "common.podMetadata" ( dict "root" . "service" .Values ) | nindent 6 }}
    spec: {{- include "common.podConfig" ( dict "root" . "service" .Values ) | nindent 6 }}
      containers:
        - name: "main"
          {{- include "common.containerConfig" ( dict "root" . "container" .Values ) | nindent 10 }}
```

In general it do the same thing than the default Helm chart, the following chapter describe the specific things.

[Good example of using most of the functions](https://github.com/camptocamp/helm-custom-pod/blob/master/templates/deployment.yaml).

### Service name

### `common.servicenamePostfix`

Expand the name of the chart.

Parameters:

- `service`: the service object.
- `serviceName`: the service name default value (optional).

Used values:
[`serviceName`](https://github.com/camptocamp/helm-common/blob/master/values.md#definitions/serviceName).

### `common.nameNoTrunc`

Expand the name of the chart.

Parameters:

- `root`: the root object, should be `$`.
- `service`: the service object.

Used values:
[`nameOverride`](https://github.com/camptocamp/helm-common/blob/master/values.md#definitions/nameOverride).

### `common.name`

Expand the name of the chart.

Parameters:

- `root`: the root object, should be `$`.
- `service`: the service object.

Used values:
[`nameOverride`](https://github.com/camptocamp/helm-common/blob/master/values.md#definitions/nameOverride),
[`nameTrunc`](https://github.com/camptocamp/helm-common/blob/master/values.md#definitions/nameTrunc).

Used functions:

- `common.nameNoTrunc`

### `common.releaseNameNoTrunc`

Expand the name of the release.

Parameters:

- `root`: the root object, should be `$`.
- `service`: the service object.

Used values:
[`releaseNameOverride`](https://github.com/camptocamp/helm-common/blob/master/values.md#definitions/releaseNameOverride).

### `common.releaseName`

Expand the name of the release.

Parameters:

- `root`: the root object, should be `$`.
- `service`: the service object.

Used values:
[`releaseNameOverride`](https://github.com/camptocamp/helm-common/blob/master/values.md#definitions/releaseNameOverride),
[`releaseTrunc`](https://github.com/camptocamp/helm-common/blob/master/values.md#definitions/releaseTrunc).

Used functions:

- `common.releaseNameNoTrunc`

### `common.fullname`

Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.

The full name is the concatenation of a prefix, and a service name `<prefix>-<service-name>`.

Where the prefix is the release name or the chart name (also just named name) of the chart `<release-name>-<chart-name>`.

Parameters:

- `root`: the root object, should be `$`.
- `service`: the service object.

Used values:
[`releaseNameOverride`](https://github.com/camptocamp/helm-common/blob/master/values.md#definitions/releaseNameOverride),
[`nameOverride`](https://github.com/camptocamp/helm-common/blob/master/values.md#definitions/nameOverride),
[`fullnameOverride`](https://github.com/camptocamp/helm-common/blob/master/values.md#definitions/fullnameOverride),
[`releaseTrunc`](https://github.com/camptocamp/helm-common/blob/master/values.md#definitions/releaseTrunc),
[`nameTrunc`](https://github.com/camptocamp/helm-common/blob/master/values.md#definitions/nameTrunc),
[`prefixTrunc`](https://github.com/camptocamp/helm-common/blob/master/values.md#definitions/prefixTrunc).

Used functions:

- `common.releaseName`
- `common.name`
- `common.servicenamePostfix`

### `common.chart`

Create chart name and version as used by the chart label.

The root (`$`) should be directly passed as parameter.

### `common.labels`

Create the labels for the service.

Parameters:

- `root`: the root object, should be `$`.

Used values:

Used functions:

- `common.chart`
- `common.selectorLabels`

### `common.selectorLabels`

Create the selector labels for the service.

Parameters:

- `root`: the root object, should be `$`.
- `service`: the service object.
- `serviceName`: the service name default value (optional).

Used values:

- [`serviceName`](https://github.com/camptocamp/helm-common/blob/master/values.md#definitions/serviceName) from the service object.

Used functions:

- `common.name`

### `common.dictToList`

Convert a dictionary to a list.

Used to pass a dictionary to Kubernetes but configure a dict in the values to ve able to override them.

Example, by default this:

```yaml
name_1:
  key: value
name_2:
  key: value
```

is transformed into this:

```yaml
- name: name_1
  key: value
- name: name_2
  key: value
```

Parameters:

- `keyName`: the name of the key to use in the list (default is 'name').

### `common.metadata`

Create the metadata for the Kubernetes object.

Parameters:

- `service`: the service object.

Used values:

- [`labels`](https://github.com/camptocamp/helm-common/blob/master/values.md#definitions/labels) from the service object.
- [`annotations`](https://github.com/camptocamp/helm-common/blob/master/values.md#definitions/annotations) from the service object.

Used functions:

- `common.labels`

## Schema documentation

Documentation based on the schema defined in [values.md](./values.md).

## Pre commit hooks

[pre-commit](https://pre-commit.com/) hook used to generate and check your expected template.

### Adding to your `.pre-commit-config.yaml`

```yaml
ci:
  skip:
    - heml-template-gen

repos:
  - repo: https://github.com/camptocamp/helm-common
    rev: <version> # Use the ref you want to point at
    hooks:
      - id: helm-template-gen
        files: |-
          (?x)(
            ^templates/.*$
            |^values\.yaml$
            |^Chart\.yaml$
            |tests/values\.yaml$
          )
        args:
          - --values=tests/values.yaml
          - release-name
          - .
          - tests/expected.yaml
```

## Contributing

Install the pre-commit hooks:

```bash
pip install pre-commit
pip install -e .
pre-commit install --allow-missing-config
```
