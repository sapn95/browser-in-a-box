{{- define "chrome-in-a-box.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "chrome-in-a-box.fullname" -}}
{{- default .Chart.Name .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "chrome-in-a-box.labels" -}}
app.kubernetes.io/name: {{ include "chrome-in-a-box.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}

{{- define "chrome-in-a-box.selectorLabels" -}}
app.kubernetes.io/name: {{ include "chrome-in-a-box.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
