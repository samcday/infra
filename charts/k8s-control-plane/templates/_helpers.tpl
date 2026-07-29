{{- define "k8s-control-plane.kubectl-image" -}}
{{- required "missing utilities.image" $.Values.utilities.image -}}
{{- end }}

{{- define "k8s-control-plane.component-image" -}}
{{- $image := .image -}}
{{- $root := .root -}}
{{- printf "%s:%s" (required "missing image.repository" $image.repository) (tpl (required "missing image.tag" $image.tag) $root) -}}
{{- with $image.digest -}}@{{ . }}{{- end -}}
{{- end }}

{{- define "k8s-control-plane.stable-labels" -}}
app.kubernetes.io/name: {{ .root.Chart.Name | quote }}
app.kubernetes.io/instance: {{ .root.Release.Name | quote }}
app.kubernetes.io/component: {{ .component | quote }}
{{- end }}

{{- define "k8s-control-plane.container-security-context" -}}
{{- $securityContext := dict -}}
{{- if .root.Values.parentWorkloads.enabled -}}
{{- $securityContext = deepCopy (default (dict) .root.Values.parentWorkloads.containerSecurityContext) -}}
{{- end -}}
{{- $securityContext = mustMergeOverwrite $securityContext (default (dict) .securityContext) -}}
{{- with $securityContext }}
securityContext:
  {{- toYaml . | nindent 2 }}
{{- end -}}
{{- end }}

{{- define "k8s-control-plane.parent-pod-spec" -}}
{{- $root := .root -}}
{{- if $root.Values.parentWorkloads.enabled -}}
{{- $parent := $root.Values.parentWorkloads -}}
{{- $isDeployment := default false .deployment -}}
{{- if default false .disableServiceAccountToken }}
automountServiceAccountToken: false
{{- end }}
{{- with $parent.podSecurityContext }}
securityContext:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with $parent.placement.nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with $parent.placement.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- $affinity := deepCopy (default (dict) $parent.placement.affinity) -}}
{{- if and $isDeployment $parent.deployment.requiredPodAntiAffinityTopologyKey -}}
{{- $podAntiAffinity := deepCopy (default (dict) (get $affinity "podAntiAffinity")) -}}
{{- $required := default (list) (get $podAntiAffinity "requiredDuringSchedulingIgnoredDuringExecution") -}}
{{- $term := dict "labelSelector" (dict "matchLabels" (dict "app.kubernetes.io/name" $root.Chart.Name "app.kubernetes.io/instance" $root.Release.Name "app.kubernetes.io/component" .component)) "topologyKey" $parent.deployment.requiredPodAntiAffinityTopologyKey -}}
{{- $_ := set $podAntiAffinity "requiredDuringSchedulingIgnoredDuringExecution" (append $required $term) -}}
{{- $_ := set $affinity "podAntiAffinity" $podAntiAffinity -}}
{{- end }}
{{- with $affinity }}
affinity:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- if $isDeployment }}
{{- with $parent.deployment.priorityClassName }}
priorityClassName: {{ . | quote }}
{{- end }}
{{- if ne $parent.deployment.terminationGracePeriodSeconds nil }}
terminationGracePeriodSeconds: {{ $parent.deployment.terminationGracePeriodSeconds }}
{{- end }}
{{- end }}
{{- end }}
{{- end }}

{{- define "k8s-control-plane.parent-deployment-spec" -}}
{{- if .Values.parentWorkloads.enabled -}}
{{- with .Values.parentWorkloads.deployment.strategy }}
strategy:
  {{- toYaml . | nindent 2 }}
{{- end }}
minReadySeconds: {{ .Values.parentWorkloads.deployment.minReadySeconds }}
{{- end }}
{{- end }}

{{- define "k8s-control-plane.validate" -}}
{{- $validDigest := "^sha256:[0-9a-f]{64}$" -}}
{{- range $name, $image := dict "apiServer" .Values.apiServer.image "controllerManager" .Values.controllerManager.image "scheduler" .Values.scheduler.image -}}
{{- if and $image.digest (not (regexMatch $validDigest $image.digest)) -}}
{{- fail (printf "%s.image.digest must be sha256:<64 lowercase hex characters>" $name) -}}
{{- end -}}
{{- end -}}
{{- if not (has .Values.bootstrap.kubeadmAPIVersion (list "kubeadm.k8s.io/v1beta3" "kubeadm.k8s.io/v1beta4")) -}}
{{- fail "bootstrap.kubeadmAPIVersion must be kubeadm.k8s.io/v1beta3 or kubeadm.k8s.io/v1beta4" -}}
{{- end -}}
{{- if not (has .Values.konnectivity.server.clientIdentity (list "legacyAdmin" "dedicated")) -}}
{{- fail "konnectivity.server.clientIdentity must be legacyAdmin or dedicated" -}}
{{- end -}}
{{- if and (not .Values.konnectivity.enabled) (eq .Values.konnectivity.server.clientIdentity "dedicated") -}}
{{- fail "konnectivity.server.clientIdentity=dedicated requires konnectivity.enabled=true" -}}
{{- end -}}
{{- if and .Values.parentWorkloads.deployment.pdb.enabled (not .Values.parentWorkloads.enabled) -}}
{{- fail "parentWorkloads.deployment.pdb.enabled requires parentWorkloads.enabled=true" -}}
{{- end -}}
{{- if .Values.parentWorkloads.deployment.pdb.enabled -}}
{{- $pdbSpec := .Values.parentWorkloads.deployment.pdb.spec -}}
{{- if hasKey $pdbSpec "selector" -}}
{{- fail "do not set parentWorkloads.deployment.pdb.spec.selector" -}}
{{- end -}}
{{- $hasMin := hasKey $pdbSpec "minAvailable" -}}
{{- $hasMax := hasKey $pdbSpec "maxUnavailable" -}}
{{- if eq $hasMin $hasMax -}}
{{- fail "parentWorkloads.deployment.pdb.spec must set exactly one of minAvailable or maxUnavailable" -}}
{{- end -}}
{{- end -}}
{{- if and .Values.parentWorkloads.enabled (dig "readOnlyRootFilesystem" false .Values.parentWorkloads.containerSecurityContext) (not .Values.utilities.writableTmp) -}}
{{- fail "utilities.writableTmp=true is required when parentWorkloads.containerSecurityContext.readOnlyRootFilesystem=true" -}}
{{- end -}}
{{- end }}
