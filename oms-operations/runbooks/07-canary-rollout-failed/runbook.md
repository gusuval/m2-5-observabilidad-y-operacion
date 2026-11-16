# Runbook: Canary de Cloud Run fallido (rollout de una release)

> **Del catálogo (Media, V3+V4: auto-rollback).** Es el runbook de "acabamos de desplegar y algo va peor". El principio: **primero se revierte, después se investiga.**
> **Versión:** V3 (ver `post-mortems.md`). **Owner:** equipo SRE (pipeline) con el equipo que publica la release. **Última revisión:** 2026-10-14.

---

## Trigger

**Alerta principal:** `CanaryRolloutFailed` (severity: **critical**, PagerDuty)
**Condición:** durante un rollout, la revisión con tag `canary` de `oms-orders` u `oms-catalog` es claramente peor que la revisión con tag `stable` del mismo servicio, durante **5 minutos**, con al menos **100 peticiones** en 10 min en la canary:
- `reason="errors"`: ratio de 5xx de la canary **> 2× el de la estable** y **> 1 %**.
- `reason="latency"`: p95 de la canary **> 1,5× el de la estable** y **> 250 ms** (NFR-PERF-002; evita disparar por 30 ms frente a 20 ms).

**Alertas asociadas:**

| Alerta | Severity | Condición |
|---|---|---|
| `CanaryAnalysisNotRunning` | warning | Hay una canary con tráfico (entre 0 y 100 %) y el job `canary-analysis` lleva > 5 min sin publicar latido. La red de seguridad V3 no está mirando. |
| `CanaryRolloutStuck` | warning | Una canary lleva > 2 h con tráfico parcial: el pipeline murió a mitad o alguien lo dejó a medias, y se acerca el final de la ventana OPS-002. |

**Cómo es un rollout normal (para entender la alerta):** pipeline de GitLab CI, solo en ventana OPS-002 (09:00–18:00 CET, no viernes ni vísperas de festivo; lo impone el job `check-deploy-window`):

```
build → test (+ query-plan-check) → deploy-staging → e2e-staging → migrate-expand (prod)
  → deploy-canary  : gcloud run deploy <svc> --image=…:<sha> --no-traffic --tag canary
  → canary-10      : gcloud run services update-traffic <svc> --to-tags canary=10    → canary-analysis (10 min)
  → canary-50      : gcloud run services update-traffic <svc> --to-tags canary=50    → canary-analysis (10 min)
  → promote-100    : gcloud run services update-traffic <svc> --to-latest
                     + --update-tags previous=<antigua stable>,stable=<nueva>
```

`oms-outbox-relay` no tiene ingress (no admite traffic splitting): se despliega después de `promote-100` de `oms-orders`.

**Cuándo dispara (patrones conocidos):**
1. **Bug funcional** en un camino concreto (cupones, tarjetas regalo): 5xx solo en un endpoint. Ver PM #1 y #3.
2. **Regresión de rendimiento**: N+1 queries o un índice que falta. Sin errores, solo latencia. Ver PM #2.
3. **Configuración**: variable de entorno o secreto nuevo que no existe en producción. La canary falla al 100 % desde el primer minuto.
4. **Falso positivo por tráfico bajo**: a las 09:05 con el 10 %, 3 errores son el 3 %. El mínimo de 100 peticiones lo filtra; si dispara igual, el paso 3 lo confirma.

---

## Contexto

**Qué impacto tiene:**
- Solo el **porcentaje de tráfico que va a la canary** (10 % o 50 %) sufre el fallo. Con el 10 %, 1 de cada 10 clientes ve el error o la lentitud.
- Si la canary falla en `POST /api/orders`, consume el budget del **SLO 1 de Orders** (99,9 %, ~70 peticiones fallidas en 28 d): con el 10 % del tráfico y la canary al 30 % de errores, se pierden ~5 pedidos por cada 10 min que siga sirviendo. **Cada minuto cuenta: revierte antes de analizar.**
- Si la canary es lenta en `GET /api/orders*`, consume el budget del **SLO 2** (latencia).

**Qué NO afecta:**
- **La revisión estable** sigue sirviendo el resto del tráfico sin cambios. Revertir es mover tráfico, no desplegar: tarda segundos.
- **Los datos:** una canary no puede romper el esquema porque el pipeline solo aplica migraciones **expand** (añadir columnas, tablas, índices) que la revisión estable tolera. Las **contract** (borrar o renombrar) van en una release posterior, cuando ninguna revisión antigua sirve tráfico.
- **Los pedidos ya creados** por la canary son válidos (mismo esquema, misma BD). Si tienes dudas sobre su contenido, el paso 4 los localiza por `revision` en los logs.

**Pre-requisitos para diagnosticar:**
- Grupo `oms-oncall@` con `roles/run.developer` (para `update-traffic`), `roles/monitoring.viewer`, `roles/logging.viewer` sobre `oms-production`. Rol `Maintainer` en el proyecto GitLab `oms/oms-app` (cancelar pipelines y cambiar variables de CI).
- Token de GitLab para la API: secreto `oms-gitlab-ops-token` en Secret Manager.
- Dashboard [`oms-deploys`](https://grafana.example.com/d/oms-deploys) (panel "Canary vs stable": errores y p95 por `revision`) y canal `#oms-releases` (el pipeline publica ahí cada etapa y los flags de la release).
- **Setup de sesión** ejecutado (ver [`../README.md`](../README.md#setup-de-sesión-del-on-call-copiar-una-vez-al-empezar-el-incidente)) y, además:

```bash
export JOB=oms-orders                     # el servicio de la alerta (label job)
export GITLAB_API=https://gitlab.example.com/api/v4/projects/oms%2Foms-app
export GITLAB_TOKEN="$(gcloud secrets versions access latest --secret=oms-gitlab-ops-token)"
# Nombres de las revisiones por tag (los usan todos los comandos de este runbook)
export CANARY=$(gcloud run services describe $JOB --region=$REGION --format=json | jq -r '.status.traffic[] | select(.tag=="canary") | .revisionName')
export STABLE=$(gcloud run services describe $JOB --region=$REGION --format=json | jq -r '.status.traffic[] | select(.tag=="stable") | .revisionName')
echo "canary=$CANARY stable=$STABLE"
```

---

## Diagnóstico (en orden, ejecuta y mira)

> Si la canary tiene **> 5 % de 5xx en `POST /api/orders`**, no hagas el diagnóstico: ejecuta ya la **Acción 2** y vuelve aquí después. El diagnóstico sirve para decidir *cómo* revertir, no *si* revertir.

### 0. ¿Cuánto tráfico tiene la canary y qué dice la alerta? (30 s)

```bash
gcloud run services describe $JOB --region=$REGION --format="table(status.traffic[].revisionName, status.traffic[].percent, status.traffic[].tag)"
promq "sum by (revision) (rate(http_requests_total{job=\"$JOB\", code=~\"5..\"}[5m])) / sum by (revision) (rate(http_requests_total{job=\"$JOB\"}[5m]))"
promq "histogram_quantile(0.95, sum by (le, revision) (rate(http_request_duration_seconds_bucket{job=\"$JOB\"}[5m])))"
```

**Si la canary ya tiene 0 %:** el job `canary-analysis` ya ha revertido (V3). Ve directo a **Verificación** y luego al paso 4 para el ticket.
**Si la canary tiene 10 % o 50 % y sus errores o su p95 son claramente peores que los de `$STABLE`:** confirmado. Anuncia en `#incident-response` y `#oms-releases`: *"CanaryRolloutFailed en $JOB ($CANARY). Revirtiendo tráfico a $STABLE."* y sigue.
**Si la canary ya tiene 100 % (`promote-100` hecho):** no hay estable con la que comparar; la alerta vendrá de la burn rate o del runbook 02. El rollback es a la revisión con tag `previous` (Acción 2, variante B).

### 1. ¿El pipeline sigue vivo? (¿va a promocionar mientras investigas?)

```bash
curl -s -H "PRIVATE-TOKEN: $GITLAB_TOKEN" "$GITLAB_API/pipelines?per_page=5&ref=main" \
  | jq -r '.[] | "\(.id)\t\(.status)\t\(.created_at)\t\(.web_url)"'
```

**Si el último pipeline está `running`:** cancélalo **antes** de cualquier otra cosa (Acción 2 incluye el comando). Si no, la siguiente etapa puede subir la canary al 50 % mientras tú miras logs.
**Si está `failed` en `canary-analysis`:** el auto-rollback se ejecutó. Mira el log del job (`$GITLAB_API/jobs/<id>/trace`) para ver qué umbral falló.

### 2. ¿Es la canary o es el entorno?

```bash
promq "sum(rate(http_requests_total{job=\"$JOB\", revision=\"$STABLE\", code=~\"5..\"}[5m])) / sum(rate(http_requests_total{job=\"$JOB\", revision=\"$STABLE\"}[5m]))"
promq "sum(rate(http_requests_total{job=\"$JOB\", revision=\"$STABLE\", code=~\"5..\"}[5m] offset 1h)) / sum(rate(http_requests_total{job=\"$JOB\", revision=\"$STABLE\"}[5m] offset 1h))"
```

**Si la estable también ha empeorado respecto a hace 1 h:** el problema no es (solo) la canary. Puede ser la BD (→ [01](../01-db-pool-saturated/runbook.md), [09](../09-postgres-unavailable/runbook.md)) o Stripe (→ [03](../03-stripe-payment-gateway-degraded/runbook.md)). Revierte igualmente (Acción 2: no cuesta nada) y sigue el otro runbook.
**Si la estable está como siempre y solo la canary falla:** es la release. Sigue.

### 3. ¿Qué endpoints fallan o se ralentizan en la canary?

```bash
promq "topk(10, sum by (endpoint, method, code) (rate(http_requests_total{job=\"$JOB\", revision=\"$CANARY\", code=~\"5..\"}[10m])))"
promq "topk(10, histogram_quantile(0.95, sum by (le, endpoint) (rate(http_request_duration_seconds_bucket{job=\"$JOB\", revision=\"$CANARY\"}[10m]))))"
```

**Si los errores se concentran en un endpoint ligado a la funcionalidad nueva de la release** (lo dice el mensaje del pipeline en `#oms-releases`): candidato a apagarlo con el flag de la release (**Acción 1**).
**Si fallan endpoints que la release no tocaba, o todos:** es configuración o una dependencia común. **Acción 2** directamente.
**Si el total de peticiones de la canary en 10 min es < 100:** puede ser un falso positivo por tráfico bajo. Aun así, revierte: volver a desplegar la canary cuesta 10 min; un pedido perdido no se recupera.

### 4. ¿Qué dicen los logs de la canary?

```bash
gcloud logging read "resource.type=\"cloud_run_revision\" AND resource.labels.revision_name=\"$CANARY\" AND severity>=ERROR" \
  --freshness=20m --limit=200 --format=json \
  | jq -r '.[] | "\(.jsonPayload.event // "-")\t\(.jsonPayload.error_code // "-")"' | sort | uniq -c | sort -rn | head
gcloud logging read "resource.type=\"cloud_run_revision\" AND resource.labels.revision_name=\"$CANARY\" AND severity>=ERROR" \
  --freshness=20m --limit=3 --format="value(jsonPayload.trace_id, jsonPayload.event, jsonPayload.message)"
```

Anota en el ticket el `event` más repetido y 2–3 `trace_id`: es lo que necesita el equipo de la release para el fix.
**Si los errores son `config.*` o de arranque (`secret not found`, `env var missing`):** la canary no debería volver a desplegarse hasta corregir la configuración del servicio.

### 5. ¿Qué trae la release: flags y migraciones?

```bash
curl -s -H "PRIVATE-TOKEN: $GITLAB_TOKEN" "$GITLAB_API/releases?per_page=1" | jq -r '.[0] | .tag_name, .description' | head -40
psql -X -c "SELECT version, name, applied_at FROM public.schema_migrations WHERE applied_at > now() - interval '1 day' ORDER BY applied_at DESC;"
```

La descripción de la release (plantilla obligatoria del MR) lista los **flags de la release** (`release.<ticket>-<slug>`) y las **migraciones** (`NNNN_expand_*` o `NNNN_contract_*`).
**Si hay un flag de release y el paso 3 apunta a su funcionalidad:** Acción 1.
**Si solo hay migraciones `expand`:** la revisión estable funciona con el esquema nuevo. Revertir el tráfico es seguro. **No se revierte el esquema.**
**Si aparece una migración `contract` aplicada en esta release:** se ha saltado la regla de expand-and-contract. La revisión estable podría fallar con el esquema actual (le falta una columna que espera). **No muevas el tráfico a ciegas:** ve a la **Acción 3**.

---

## Acciones (de menor a mayor impacto, idempotentes)

### Acción 1: Apagar el flag de la funcionalidad nueva (impacto: bajo · ≤ 15 s · solo si la release tiene flag)

```bash
export RELEASE_FLAG=release.<ticket>-<slug>      # el de la descripción de la release (paso 5)
flag_off $RELEASE_FLAG
flag_get $RELEASE_FLAG
```

**Qué hace:** la canary deja de ejecutar el código nuevo. La revisión estable no lo tiene, así que no se ve afectada.
**Por qué primero:** es lo más rápido y deja la canary sirviendo, lo que confirma si el fallo era de la funcionalidad nueva: si en 2–3 min los errores de la canary bajan al nivel de la estable, has acotado la causa.
**Idempotente:** sí.
**Cuándo NO:** si el fallo no está en la funcionalidad del flag (paso 3), o si la canary tiene > 5 % de 5xx en el checkout. En ese caso, Acción 2 a la vez, sin esperar.
**Importante:** aunque el flag lo arregle, **haz también la Acción 2**. Una canary con el código defectuoso no debe seguir hacia el 50 %.

### Acción 2: Devolver todo el tráfico a la revisión estable y parar el pipeline (impacto: bajo · segundos · la acción estándar)

```bash
# 1. Parar el pipeline para que no promocione (id del paso 1; si no está running, devuelve error y no pasa nada)
curl -s -X POST -H "PRIVATE-TOKEN: $GITLAB_TOKEN" "$GITLAB_API/pipelines/<PIPELINE_ID>/cancel" | jq -r '.status'

# 2A. Rollout a medias (10 % / 50 %): todo a la estable
gcloud run services update-traffic $JOB --region=$REGION --to-revisions=$STABLE=100

# 2B. Si ya estaba promocionada al 100 %: todo a la revisión anterior (tag previous)
export PREVIOUS=$(gcloud run services describe $JOB --region=$REGION --format=json | jq -r '.status.traffic[] | select(.tag=="previous") | .revisionName')
gcloud run services update-traffic $JOB --region=$REGION --to-revisions=$PREVIOUS=100

# 3. Comprobar
gcloud run services describe $JOB --region=$REGION --format="table(status.traffic[].revisionName, status.traffic[].percent, status.traffic[].tag)"
```

**Qué hace:** mueve el 100 % del tráfico a una revisión que ya estaba sirviendo hace minutos. No hay build, no hay arranque en frío masivo (la estable ya tiene instancias calientes) y tarda < 30 s. La canary **conserva su tag** con 0 % de tráfico: el equipo puede seguir probándola en `https://canary---<url-del-servicio>` sin afectar a clientes.
**Idempotente:** sí. Ejecutar `update-traffic` con la misma asignación no cambia nada.
**Coste:** la funcionalidad nueva se retrasa hasta el siguiente intento. Es todo.
**Fuera de ventana OPS-002:** revertir está **siempre** permitido (es mitigación, no despliegue). Anótalo en el ticket.
**Si revertiste 2B:** el equipo de la release tiene que confirmar que la revisión `previous` es compatible con las migraciones aplicadas desde entonces (paso 5).

### Acción 3: La estable no es compatible con el esquema actual (impacto: alto · requiere aprobación)

Solo si el paso 5 encontró una migración `contract` aplicada en esta release.

```bash
# 1. Mientras se decide: apagar la funcionalidad (Acción 1) y congelar despliegues (ver Acción 4)
# 2. Probar la estable contra el esquema actual SIN tráfico de clientes, por su URL de tag
curl -s -o /dev/null -w "%{http_code} %{time_total}s\n" -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  "https://stable---$(gcloud run services describe $JOB --region=$REGION --format='value(status.url)' | sed 's#https://##')/healthz/deep"
```

Ejecútalo desde `oms-ops-bastion` si el ingress del servicio es solo interno + Load Balancer.

**Qué hace:** comprueba, sin tráfico real, si la estable sigue funcionando con el esquema de hoy (`/healthz/deep` hace una lectura y una escritura de prueba en cada esquema).
- **Si la estable responde 200:** la migración no le afecta en los caminos principales. Acción 2A con aprobación.
- **Si responde 5xx:** no hay vuelta atrás por tráfico. Las opciones son *roll forward* (hotfix de la canary por el pipeline con la etiqueta `emergency-release`) o una migración inversa escrita a mano. **Nunca** se restaura un backup para deshacer una migración: se perderían los pedidos creados desde entonces.
**Quién aprueba:** Engineering Manager de guardia (PagerDuty nivel 2) y el tech lead del módulo. El roll forward fuera de ventana también lo aprueba el EM.

### Acción 4: Congelar despliegues hasta el post-mortem (impacto: bajo para producción · alto para el equipo)

```bash
curl -s -X PUT -H "PRIVATE-TOKEN: $GITLAB_TOKEN" "$GITLAB_API/variables/DEPLOY_FREEZE" --form "value=true" | jq -r '.key + "=" + .value'
```

**Qué hace:** el job `check-deploy-window` falla si `DEPLOY_FREEZE=true`, así que ningún pipeline llega a `deploy-canary`. Es el flag del pipeline.
**Idempotente:** sí.
**Cuándo:** si la misma release ha fallado dos veces, si hubo Acción 3, o si el budget del SLO 1 está en 🔴 (la [política de error budget](../../slos/error-budget-policy.md) ya impone el freeze de features).
**Quién decide:** el on-call lo activa; quitarlo lo decide el EM tras el post-mortem.

---

## Verificación

**Métricas que tienen que volver a la normalidad:**
```promql
# 1. La canary no recibe tráfico
max(run_revision_traffic_percent{job="oms-orders", tag="canary"}) == 0

# 2. Error rate del servicio (ya solo la estable) por debajo del 0,1 %
sum(rate(http_requests_total{job="oms-orders", code=~"5.."}[5m])) / sum(rate(http_requests_total{job="oms-orders"}[5m])) < 0.001

# 3. p95 de endpoints críticos por debajo de 250 ms
histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{job="oms-orders", method="GET", endpoint=~"/api/orders.*"}[5m]))) < 0.25
```
(Cambia `oms-orders` por `oms-catalog` si la alerta era del catálogo.)

**Ventana de observación:** **10 minutos** con las 3 condiciones en verde.

**Cómo confirmas:**
1. Panel "Canary vs stable" de [`oms-deploys`](https://grafana.example.com/d/oms-deploys): la línea de la canary cae a 0 peticiones.
2. `CanaryRolloutFailed` *resolved* en Alertmanager y ninguna burn rate (`OrdersAvailabilityBurnRateFast`, `OrdersLatencyBurnRate`) activa.
3. El pipeline aparece `canceled` o `failed`, no `running`.
4. Comunica en `#incident-response` y `#oms-releases`: *"Canary $CANARY de $JOB revertida. 100 % en $STABLE, error rate X %, p95 Y ms estables 10 min. Flags apagados: [lista]. Migraciones: solo expand, no se revierten. Release bloqueada hasta fix."*

---

## Rollback

| Acción | Rollback | Cuándo |
|---|---|---|
| 1 (flag de release) | `flag_on $RELEASE_FLAG` | Solo cuando la release corregida esté desplegada y su canary haya pasado el análisis. Nunca con la revisión defectuosa. |
| 2 (tráfico a estable) | Volver a lanzar el pipeline del fix: `deploy-canary` con 10 % y análisis completo. **No** se hace `--to-tags canary=10` a mano sobre la revisión defectuosa. | Tras el fix. Si el análisis fue un falso positivo (paso 3: < 100 peticiones), relanzar el mismo pipeline en la siguiente ventana. |
| 3 (roll forward) | Si el hotfix también falla, Acción 2 al hotfix anterior que funcionara y escalar al EM. | — |
| 4 (freeze) | `curl -s -X PUT -H "PRIVATE-TOKEN: $GITLAB_TOKEN" "$GITLAB_API/variables/DEPLOY_FREEZE" --form "value=false"` | Lo decide el EM tras el post-mortem. |

---

## Después del incidente

- [ ] Ticket con: servicio, `$CANARY`, `$STABLE`, motivo (`errors` o `latency`), etapa en la que falló (10/50/100 %), peticiones fallidas y `trace_id` de ejemplo.
- [ ] Issue al equipo de la release con los logs del paso 4. El fix vuelve a pasar por canary completa (sin atajos).
- [ ] Si la funcionalidad no tenía flag y lo habría arreglado en 15 s: añadir el flag a la release corregida.
- [ ] Si hubo una migración `contract` en la misma release que el código que la necesita: revisar el MR template y el job de CI que valida el orden expand/contract.
- [ ] Si `canary-analysis` no revirtió solo (V3): por qué. ¿Umbral, mínimo de peticiones, latido caído (`CanaryAnalysisNotRunning`)?
- [ ] Budget consumido del SLO 1 o 2 y su efecto en la [política de error budget](../../slos/error-budget-policy.md).
- [ ] Si algún paso no sirvió o faltó: actualiza este runbook en el mismo PR del post-mortem.

---

## Enlaces relacionados

- Alerta YAML: [`alert.yaml`](alert.yaml)
- Post-mortems históricos: [`post-mortems.md`](post-mortems.md)
- SLOs que protege: [`../../slos/slos-orders.md`](../../slos/slos-orders.md) (SLO 1 disponibilidad y SLO 2 latencia) · [política de error budget](../../slos/error-budget-policy.md)
- Dashboard: <https://grafana.example.com/d/oms-deploys>
- Runbooks relacionados: [`02-api-p95-high`](../02-api-p95-high/runbook.md) (latencia sin deploy) · [`10-checkout-unavailable`](../10-checkout-unavailable/runbook.md) (triage end-to-end) · [`01-db-pool-saturated`](../01-db-pool-saturated/runbook.md) y [`03-stripe-payment-gateway-degraded`](../03-stripe-payment-gateway-degraded/runbook.md) (si la estable también empeoró)
