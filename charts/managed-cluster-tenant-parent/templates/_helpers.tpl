{{- define "managed-cluster-tenant-parent.name" -}}
{{- required "tenant.name is required" .Values.tenant.name -}}
{{- end -}}

{{- define "managed-cluster-tenant-parent.namespace" -}}
{{- default (include "managed-cluster-tenant-parent.name" .) .Values.tenant.namespace -}}
{{- end -}}

{{- define "managed-cluster-tenant-parent.releaseName" -}}
{{- $parent := default (dict) .Values.parent -}}
{{- default (printf "%s-bootstrap" (include "managed-cluster-tenant-parent.name" .)) $parent.releaseName -}}
{{- end -}}
