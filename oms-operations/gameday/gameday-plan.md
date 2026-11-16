# GameDay plan: "Lunes de campaña". Pool de conexiones saturado por sesiones zombi y job batch

> Valida el runbook obligatorio [`01-db-pool-saturated`](../runbooks/01-db-pool-saturated/runbook.md), versión V3, incluido su script [`db-pool-saturated.sh`](../scripts/db-pool-saturated.sh).
> Reproduce el patrón real de los post-mortems: zombis del bug OMS-1234, un export batch que se come el pool y el tráfico de la campaña de los lunes.

---

## Identificación

| | |
|---|---|
| **GameDay #** | 1 (2026-Q4) |
| **Fecha y duración** | Miércoles 25 de noviembre de 2026, 10:00–12:00 CET (dentro de la ventana OPS-002, lejos del lunes de campaña y 2 días antes de Black Friday, para llegar con el runbook validado) |
| **Bloques** | 30 min de preparación · 60 min de ejecución y recuperación · 30 min de post-mortem en caliente |
| **Entorno** | **Pre-producción `oms-staging`** (europe-west3), Cloud SQL `oms-pg-staging` con el **mismo tier y `max_connections`** que producción. **NO producción.** |
| **Owner** | Equipo SRE OMS |
| **Runbook que valida** | `../runbooks/01-db-pool-saturated/` (V3) |

---

## Hipótesis

> **"Un on-call que nunca ha ejecutado el runbook `01-db-pool-saturated` es capaz de identificar la causa y mitigar la saturación del pool en menos de 10 minutos desde la alerta, siguiendo solo el runbook y los dashboards, sin degradar el SLO de Orders por debajo del 99,9 % durante la ventana del ejercicio."**

**Criterios de éxito (todos medibles):**

| # | Criterio | Cómo se mide | Umbral de éxito |
|---|---|---|---|
| H1 | La alerta dispara a tiempo | Diferencia entre la inyección de fallo y la alerta `DBConnectionPoolSaturated` en Alertmanager | ≤ 4 min (3 min de `for:` + 1 de evaluación) |
| H2 | Time-to-mitigate | Diferencia entre la alerta y el pool < 70 % sostenido | **≤ 10 min** |
| H3 | El runbook basta | Observador 1 cuenta las búsquedas fuera del runbook o los dashboards (internet, Slack, preguntar al Game Master) | **0** |
| H4 | Diagnóstico correcto | El on-call identifica las **dos** causas inyectadas (zombis + batch) y aplica la Acción 1 **y** la Acción 2 | 2 de 2 |
| H5 | Impacto acotado | SLI de `POST /api/orders` en staging (tráfico sintético) durante la ventana | ≥ 99,9 % entre T+0 y T+30 |
| H6 | La automatización funciona | `db-pool-saturated.sh --dry-run` recomienda las acciones correctas | Recomienda Acción 1 + Acción 2 |
| H7 | Rollback limpio | Todos los flags y la configuración vuelven a su valor de partida | `flag_get` + `gcloud run services describe` sin diferencias |

---

## Escenario

**Preparación previa (semana del 16/11, owner: Game Master):** escribir y probar en un entorno efímero los dos scripts del escenario, `gameday/scenarios/pool-saturation.py` (abre o cierra las N sesiones; **rechaza cualquier `--project` distinto de `oms-staging`**) y `gameday/scenarios/restore-flags.py`, además del test de carga `gameday/load/checkout-monday.js`. Sin estos tres artefactos probados, el GameDay no se ejecuta.

**Cómo provocamos el fallo:**

```bash
# Fase A (T+0): 60 sesiones "zombi" con application_name='oms-orders', abiertas con el usuario oms_app,
#               que hacen BEGIN y se quedan idle in transaction (reproduce el bug OMS-1234)
python3 gameday/scenarios/pool-saturation.py --project oms-staging --phase zombies --sessions 60

# Fase B (T+2): lanzar el export batch con concurrencia descontrolada (DB_POOL_SIZE=40 solo para este GameDay)
gcloud run jobs execute oms-batch-export --project=oms-staging --region=europe-west3 \
  --update-env-vars=DB_POOL_SIZE=40,EXPORT_RANGE_MONTHS=48 --async

# Durante todo el ejercicio: tráfico sintético de checkout al 300 % de la media de producción (≈ 2 POST /api/orders/s)
k6 run --env BASE_URL=https://staging.oms.example.com gameday/load/checkout-monday.js
```

**Por qué esta combinación:** con solo zombis, la Acción 1 resuelve el problema en 1 minuto y no aprendemos nada nuevo. Con zombis **y** batch, el on-call tiene que leer el diagnóstico 1, ver que hay dos patrones y aplicar dos acciones distintas. Es la situación del PM #2, la que más tiempo costó.

**Cuándo lo lanzamos:** T+0 (fase A) y T+2 min (fase B). El on-call shadow **no sabe** que hay dos fases.

**Pre-flight checks (T-30 → T-5):**
- [ ] `gcloud config get-value project` **devuelve `oms-staging`** en la máquina del Game Master y en la del on-call shadow
- [ ] Tráfico sintético corriendo al 300 % y SLI de staging ≥ 99,9 % en los últimos 15 min (baseline estable)
- [ ] Alertmanager de staging enruta a un servicio PagerDuty **`OMS Staging GameDay`** (no al de producción)
- [ ] Snapshot de flags de staging guardado: `curl … /api/admin/projects/oms/features > /tmp/flags-before.json`
- [ ] Backup on-demand de `oms-pg-staging` hecho (`gcloud sql backups create --instance=oms-pg-staging`)
- [ ] Dashboards abiertos en pantalla compartida: `oms-db-overview` y `oms-overview` (datasource **staging**)
- [ ] On-call shadow con el runbook abierto y el setup de sesión ejecutado **contra staging**
- [ ] Canal `#gameday-2026-11-pool` abierto y anunciado en `#oms-sre` y `#oms-releases` ("staging en GameDay 10:00–12:00, no desplegar")
- [ ] Producción en 🟢 y sin incidentes abiertos. Si no es así, **se pospone**.

---

## Alcance acotado

**SÍ se toca:**
- Cloud SQL `oms-pg-staging` (europe-west3): sesiones de `oms_app` y el pool de `oms-orders` y `oms-catalog` en staging
- Cloud Run Job `oms-batch-export` de staging
- Feature flags del entorno `staging` de Unleash (`orders.batch-jobs-paused`, `orders.read-from-replica`)

**NO se toca (innegociable):**
- **Nada en `oms-production`**: ni lectura de secretos de producción, ni flags de producción, ni consultas a `oms-pg-prod`
- El tier, los flags de Cloud SQL y el failover HA de `oms-pg-staging` (la Acción 4 y el escalado de tier quedan **fuera** del ejercicio: si el on-call llega ahí, el Game Master lo para y se anota como hallazgo)
- Red, firewalls, IAM, Cloud Armor
- La cuenta de Stripe: el tráfico sintético usa Stripe en **modo test**
- Memorystore de staging (otro GameDay)

---

## Métricas a observar (antes / durante / después)

| Métrica (PromQL en staging) | Baseline (antes) | Esperada (durante) | Verificación (después) |
|---|---|---|---|
| `max(db_pool_in_use / db_pool_size)` | < 30 % | 95–100 % entre T+2 y la mitigación | **< 70 % durante 10 min** |
| `sum(db_pool_pending_acquires)` | 0 | > 10 | **0** |
| `count(pg_stat_activity)` con `state` idle in transaction > 10 min | 0 | 60 | **0** |
| p95 de `POST /api/orders` | ~320 ms (incluye Stripe test) | > 2 s (esperas de acquire) | vuelve a ≤ 400 ms |
| Error rate 5xx de `POST /api/orders` | < 0,1 % | pico del 2–8 % | **< 0,1 %** |
| Budget del SLO 1 en staging (ventana del ejercicio) | 100 % | se consume | Documentar el % consumido. **H5 falla** si el SLI de la ventana < 99,9 % |
| `ops_job_last_success_timestamp_seconds{job="kill-idle-connections"}` | < 1 h | (el cron preventivo se **desactiva** durante el ejercicio) | se reactiva y ejecuta OK |

> El cron preventivo de V3 se pausa en staging (`gcloud scheduler jobs pause oms-ops-kill-idle --location=europe-west3 --project=oms-staging`) durante el ejercicio. Si no, limpiaría los zombis antes de que el on-call los vea y no estaríamos probando el runbook.

---

## Escape hatch

**Aborto inmediato si se da cualquiera de estas condiciones (decide el Game Master, sin debate):**
- Cualquier métrica, log o alerta **de producción** cambia de forma correlacionada con el ejercicio
- Error rate en staging > **20 % durante > 3 min** (ya no aprendemos nada, solo hacemos daño a otros equipos que usan staging)
- `oms-pg-staging` deja de aceptar conexiones de `oms_ops` (perderíamos la capacidad de recuperar)
- El on-call shadow intenta ejecutar algo **fuera del alcance** (Acción 4, `gcloud sql instances patch`, `failover`)
- T+30 sin mitigación (H2 ya ha fallado claramente)

**Recuperación de emergencia (Game Master, en este orden):**
```bash
python3 gameday/scenarios/pool-saturation.py --project oms-staging --stop --release-all   # cierra las 60 sesiones inyectadas
gcloud run jobs executions list --job=oms-batch-export --project=oms-staging --region=europe-west3 \
  --filter="status.completionTime=null" --format="value(name)" \
  | xargs -r -I{} gcloud run jobs executions cancel {} --project=oms-staging --region=europe-west3 --quiet
# Si nada de lo anterior libera conexiones:
psql "host=127.0.0.1 dbname=oms user=oms_ops" -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename='oms_app' AND datname='oms';"
# Restaurar flags y cron
python3 gameday/scenarios/restore-flags.py --project oms-staging --from /tmp/flags-before.json
gcloud scheduler jobs resume oms-ops-kill-idle --location=europe-west3 --project=oms-staging
```

---

## Checklist temporal (minuto a minuto)

```
T-30  Equipo en sala. Dashboards abiertos. Tráfico sintético al 300 %.
T-15  Pre-flight completado (todas las casillas). Cron preventivo pausado en staging.
T-5   El on-call shadow recibe el brief: "vas a recibir una página de staging; trátala como real, sigue el runbook".
      No se le dice qué alerta ni cuántas causas hay.

T+0   Game Master: fase A (60 zombis). Observadores arrancan el cronómetro.
T+2   Game Master: fase B (export batch).
T+3…5 Debe dispararse DBConnectionPoolSaturated (H1).

T+0–20  Observación activa. NADIE ayuda al on-call. Solo se responde a "¿esto está dentro del alcance?".
T+20    Si no hay mitigación: el Game Master da UNA pista ("mira el paso 1 del diagnóstico") y se anota.
T+30    Si sigue sin mitigación: escape hatch y recuperación de emergencia.
T+30–40 Verificación: 10 min con el pool < 70 % (o recuperación de emergencia).
T+40–50 Rollback guiado por el runbook (H7): flags OFF, comparación con el snapshot, cron reanudado.

T+60–90 Post-mortem en caliente con todo el equipo (formato blameless).
```

---

## Roles

| Rol | Persona | Responsabilidad |
|---|---|---|
| **Game Master** | SRE #1 (el SRE fuera de guardia esa semana) | Ejecuta el escenario, vigila el escape hatch, tiene la **única** autoridad para abortar |
| **On-call shadow** | Developer del equipo Orders que **nunca** ha estado de guardia con este runbook (rotación de onboarding) | Sigue el runbook como si fuera real, en voz alta |
| **Observador 1** | Developer del equipo Payments | Cronómetro y nota de cada acción del on-call con su timestamp. Cuenta las salidas del runbook (H3) |
| **Observador 2** | SRE #2 (el de guardia esa semana; sigue mirando producción en segundo plano) | Foco en métricas: qué se desvía, cuándo y cuánto. Vigila que producción no se mueva |
| **Comunicación** | Engineering Manager OMS | Mantiene `#gameday-2026-11-pool` al día, avisa a quien use staging y decide si se pospone |

> Que el on-call shadow **no** sea un SRE es intencionado. El runbook tiene que servirle a alguien que no tocó el sistema en 6 meses.

---

## Post-mortem (se rellena al terminar el ejercicio)

> Esta sección se completa en el bloque T+60–90. La estructura queda preparada para que el post-mortem en caliente no empiece de una página en blanco.

### Resumen
_Pendiente de ejecución (25/11/2026)._ Dos frases: ¿se validó la hipótesis? ¿Qué criterios H1–H7 pasaron?

### Resultado por criterio
| # | Umbral | Resultado | ¿Pasa? |
|---|---|---|---|
| H1 | ≤ 4 min | _min_ | ☐ |
| H2 | ≤ 10 min | _min_ | ☐ |
| H3 | 0 búsquedas fuera | _n_ | ☐ |
| H4 | 2/2 causas | _n/2_ | ☐ |
| H5 | SLI ≥ 99,9 % | _%_ | ☐ |
| H6 | El script recomienda 1 + 2 | _sí/no_ | ☐ |
| H7 | Sin diferencias | _sí/no_ | ☐ |

### Lo que funcionó del runbook
_Pendiente._

### Lo que NO funcionó o falta en el runbook
_Pendiente._ (Es lo más valioso del GameDay. Preguntas guía: ¿en qué paso dudó el on-call? ¿Qué comando falló o hubo que adaptar? ¿Faltaba algún permiso?)

### Acciones derivadas
| # | Acción | Owner | Fecha límite |
|---|---|---|---|
| 1 | _Pendiente_ | | antes del 27/11 (Black Friday) si bloquea |
| 2 | _Pendiente_ | | |

### Versión nueva del runbook
- [ ] Aplicar las acciones derivadas al runbook (PR enlazada al post-mortem)
- [ ] Añadir la ocurrencia como "V3.1 (GameDay #1)" en [`../runbooks/01-db-pool-saturated/post-mortems.md`](../runbooks/01-db-pool-saturated/post-mortems.md)
- [ ] Programar el siguiente GameDay con el **mismo escenario** en febrero de 2027, para verificar la mejora, y uno nuevo sobre el runbook 03 (Stripe degradado), aprovechando el modo test de Stripe
