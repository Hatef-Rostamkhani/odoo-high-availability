{{- define "odoo.pod" -}}
automountServiceAccountToken: false
securityContext:
  {{- toYaml .Values.securityContext | nindent 2 }}
  {{- if eq .Values.application.mode "ha" }}
  fsGroupChangePolicy: OnRootMismatch
  {{- end }}
terminationGracePeriodSeconds: 90
{{- with .Values.nodeSelector }}
nodeSelector: {{ toYaml . | nindent 2 }}
{{- end }}
{{- if eq .Values.application.mode "ha" }}
{{ include "odoo.placement" (dict "component" (ternary "cron" "server" (eq .mode "cron"))) }}
{{- end }}
containers:
  - name: odoo
    image: {{ .Values.image | quote }}
    imagePullPolicy: {{ .Values.imagePullPolicy }}
    {{- if .Values.database.tls.caSecret }}
    env:
      - {name: PGSSLROOTCERT, value: /run/database-ca/ca.crt}
    {{- end }}
    command: [python3, /opt/odoo/start.py]
    args: [{{ .mode | quote }}]
    {{- if ne .mode "cron" }}
    ports:
      - {name: http, containerPort: 8069}
      {{- if gt (int .Values.workers) 0 }}
      - {name: websocket, containerPort: {{ .Values.geventPort }}}
      {{- end }}
    {{- end }}
    securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: [ALL]}}
    resources: {{ toYaml (ternary .Values.cron.resources .Values.resources (eq .mode "cron")) | nindent 6 }}
    {{- if eq .mode "cron" }}
    startupProbe:
      exec: {command: [python3, /opt/odoo/cron-health.py]}
      timeoutSeconds: 5
      periodSeconds: 5
      failureThreshold: 120
    readinessProbe:
      exec: {command: [python3, /opt/odoo/cron-health.py]}
      timeoutSeconds: 5
      periodSeconds: 10
    {{- end }}
    {{- if eq .mode "serve" }}
    {{- if eq .Values.application.mode "ha" }}
    lifecycle:
      preStop:
        exec: {command: [python3, -c, "import time; time.sleep(35)"]}
    {{- end }}
    startupProbe:
      timeoutSeconds: {{ .Values.probes.timeoutSeconds }}
      {{- if eq .Values.application.mode "ha" }}
      httpGet: {path: /live, port: 9116}
      {{- else }}
      httpGet: {path: /web/health, port: http}
      {{- end }}
      periodSeconds: 5
      failureThreshold: 120
    readinessProbe:
      timeoutSeconds: {{ .Values.probes.timeoutSeconds }}
      {{- if eq .Values.application.mode "ha" }}
      httpGet: {path: /ready, port: 9116}
      {{- else }}
      httpGet: {path: "/web/health?db_server_status=1", port: http}
      {{- end }}
      periodSeconds: 5
      failureThreshold: 3
    livenessProbe:
      timeoutSeconds: {{ .Values.probes.timeoutSeconds }}
      {{- if eq .Values.application.mode "ha" }}
      httpGet: {path: /live, port: 9116}
      {{- else }}
      httpGet: {path: /web/health, port: http}
      {{- end }}
      periodSeconds: 15
      failureThreshold: 6
    {{- end }}
    volumeMounts:
      - {name: data, mountPath: /var/lib/odoo}
      - {name: configuration, mountPath: /opt/odoo, readOnly: true}
      - {name: database-secret, mountPath: /run/database-secret, readOnly: true}
      - {name: credentials-secret, mountPath: /run/credentials-secret, readOnly: true}
      {{- if .Values.database.tls.caSecret }}
      - {name: database-ca, mountPath: /run/database-ca, readOnly: true}
      {{- end }}
      - {name: runtime, mountPath: /run/odoo}
      - {name: tmp, mountPath: /tmp}
{{- if and (eq .Values.application.mode "ha") (or (eq .mode "serve") (eq .mode "cron")) }}
  - name: ha-exporter
    image: {{ .Values.image | quote }}
    imagePullPolicy: {{ .Values.imagePullPolicy }}
    command: [python3, /opt/odoo/ha-exporter.py]
    env:
      - {name: PGSSLROOTCERT, value: /run/database-ca/ca.crt}
    ports: [{name: ha-metrics, containerPort: 9116}]
    securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: [ALL]}}
    resources:
      requests: {cpu: 20m, memory: 16Mi}
      limits: {cpu: 100m, memory: 64Mi}
    volumeMounts:
      - {name: data, mountPath: /var/lib/odoo, readOnly: true}
      - {name: configuration, mountPath: /opt/odoo, readOnly: true}
      - {name: database-ca, mountPath: /run/database-ca, readOnly: true}
      - {name: runtime, mountPath: /run/odoo, readOnly: true}
{{- end }}
volumes:
  - {name: data, persistentVolumeClaim: {claimName: {{ default "odoo-data" .Values.storage.existingClaim }}}}
  - {name: configuration, configMap: {name: odoo-configuration}}
  - {name: database-secret, secret: {secretName: {{ .Values.database.existingSecret }}, defaultMode: 0440}}
  - {name: credentials-secret, secret: {secretName: {{ .Values.credentialsSecret }}, defaultMode: 0440}}
  {{- if .Values.database.tls.caSecret }}
  - name: database-ca
    secret:
      secretName: {{ .Values.database.tls.caSecret }}
      items: [{key: {{ .Values.database.tls.caKey }}, path: ca.crt}]
      defaultMode: 0444
  {{- end }}
  - {name: runtime, emptyDir: {}}
  - {name: tmp, emptyDir: {}}
{{- end -}}

{{- define "odoo.placement" -}}
affinity:
  nodeAffinity:
    requiredDuringSchedulingIgnoredDuringExecution:
      nodeSelectorTerms:
        - matchExpressions:
            - {key: node-role.kubernetes.io/control-plane, operator: DoesNotExist}
  podAntiAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 100
        podAffinityTerm:
          topologyKey: kubernetes.io/hostname
          labelSelector:
            matchLabels: {app.kubernetes.io/name: odoo, odoo.io/component: {{ .component }}}
topologySpreadConstraints:
  - maxSkew: 1
    minDomains: 2
    topologyKey: kubernetes.io/hostname
    whenUnsatisfiable: DoNotSchedule
    labelSelector:
      matchLabels: {app.kubernetes.io/name: odoo, odoo.io/component: {{ .component }}}
{{- end -}}
