{{- define "aap.labels" -}}
app.kubernetes.io/part-of: ai-agent-platform
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end }}

{{- define "aap.secretName" -}}
{{- required "secrets.existingSecret is required" .Values.secrets.existingSecret -}}
{{- end }}
