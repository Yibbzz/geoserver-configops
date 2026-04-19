{{/*
Expand the name of the chart.
*/}}
{{- define "common.servicenamePostfix" -}}
{{- if .service.serviceName -}}
{{ printf .service.serviceName }}
{{- else if hasKey . "serviceName" -}}
{{ printf .serviceName }}
{{- end }}
{{- end }}

{{/*
Expand the name of the chart.
*/}}
{{- define "common.nameNoTrunc" -}}
{{- $valuesMerged := merge ( deepCopy .service ) .root.Values }}
{{- $valuesMerged.nameOverride | default .root.Chart.Name }}
{{- end }}

{{/*
Expand and trunc the name of the chart.
*/}}
{{- define "common.name" -}}
{{- $globalMerged := merge ( deepCopy .service ) ( deepCopy .root.Values ) ( .root.Values.globals | default ( dict ) ) }}
{{- $nameTrunc := ( int $globalMerged.nameTrunc ) | default 63 }}
{{- include "common.nameNoTrunc" . | trunc $nameTrunc | trimSuffix "-" }}
{{- end }}

{{/*
Expand the release name.
*/}}
{{- define "common.releaseNameNoTrunc" -}}
{{- $globalMerged := merge ( deepCopy .service ) ( deepCopy .root.Values ) ( .root.Values.globals | default ( dict ) ) }}
{{- $globalMerged.releaseNameOverride | default .root.Release.Name }}
{{- end }}

{{/*
Expand and trunk release name.
*/}}
{{- define "common.releaseName" -}}
{{- $globalMerged := merge ( deepCopy .service ) ( deepCopy .root.Values ) ( .root.Values.globals | default ( dict ) ) }}
{{- $releaseTrunc := ( int $globalMerged.releaseTrunc ) | default 20 }}
{{- include "common.releaseNameNoTrunc" . | trunc $releaseTrunc | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "common.fullname" -}}
{{- $valuesMerged := merge ( deepCopy .service ) .root.Values }}
{{- $globalMerged := merge ( deepCopy .service ) ( deepCopy .root.Values ) ( .root.Values.globals | default ( dict ) ) }}
{{- $prefixTrunc := ( int $globalMerged.prefixTrunc ) | default 40 }}
{{- $fullnameOverride := $valuesMerged.fullnameOverride }}
{{- if $fullnameOverride }}
{{- printf "%s-%s" ( $fullnameOverride | trunc $prefixTrunc | trimSuffix "-" ) ( include "common.servicenamePostfix" . ) | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := include "common.name" . }}
{{- $releaseName := include "common.releaseName" . }}
{{- printf "%s-%s" ( printf "%s-%s" $releaseName $name | trunc $prefixTrunc | trimSuffix "-" ) ( include "common.servicenamePostfix" . )  | trunc 63 | trimSuffix "-"  }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "common.chart" -}}
{{- .Chart.Name | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "common.labels" -}}
helm.sh/chart: {{ include "common.chart" .root }}
{{- if .root.Chart.AppVersion }}
app.kubernetes.io/version: {{ .root.Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
{{ include "common.selectorLabels" . }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "common.selectorLabels" -}}
app.kubernetes.io/name: {{ include "common.nameNoTrunc" . }}
app.kubernetes.io/instance: {{ include "common.releaseNameNoTrunc" . }}
{{- if hasKey .service "serviceName" }}
app.kubernetes.io/component: {{ .service.serviceName }}
{{- else if hasKey . "serviceName" }}
app.kubernetes.io/component: {{ .serviceName }}
{{- else }}
app.kubernetes.io/component: main
{{- end }}
{{- end }}

{{- define "common.dictToList" -}}
{{ $keyName := (get . "keyName" | default "name")}}
{{- range $key, $value := .contents -}}
- {{ $keyName }}: {{ $key }}
{{- $value | toYaml | nindent 2 }}
{{ end -}}
{{- end -}}

{{- define "common.metadata" -}}
labels: {{ include "common.labels" . | nindent 2 }}
{{- with .service.labels }}
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .service.annotations }}
annotations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end }}
