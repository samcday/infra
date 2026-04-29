{{- define "managed-cluster-tenant.name" -}}
{{- required "tenant.name is required" .Values.tenant.name -}}
{{- end -}}

{{- define "managed-cluster-tenant.namespace" -}}
{{- default (include "managed-cluster-tenant.name" .) .Values.tenant.namespace -}}
{{- end -}}

{{- define "managed-cluster-tenant.sourceName" -}}
{{- $source := default (dict) .Values.source -}}
{{- default (include "managed-cluster-tenant.name" .) $source.name -}}
{{- end -}}

{{- define "managed-cluster-tenant.kustomizationName" -}}
{{- $kustomization := default (dict) .Values.kustomization -}}
{{- default (include "managed-cluster-tenant.name" .) $kustomization.name -}}
{{- end -}}

{{- define "managed-cluster-tenant.sopsSecretName" -}}
{{- $sops := default (dict) .Values.sops -}}
{{- default (printf "%s-age-key" (include "managed-cluster-tenant.name" .)) $sops.secretName -}}
{{- end -}}
