# Scripts: runbooks automatizados

> Bonus "Implementación automatizada de 1 runbook": script idempotente con `--dry-run`.

| Script | Runbook | Qué automatiza | Qué NO automatiza (a propósito) |
|---|---|---|---|
| [`db-pool-saturated.sh`](db-pool-saturated.sh) | [`01-db-pool-saturated`](../runbooks/01-db-pool-saturated/runbook.md) | Diagnóstico 1–3 + recomendación de acción + **Acción 1** (terminar sesiones idle de `oms_app`) + métrica de salud para Prometheus | Acciones 1b, 2, 3 y 4: tienen coste para usuarios o para la capacidad y requieren criterio humano |

## Uso

```bash
# Requisito: el "setup de sesión" de runbooks/README.md (túnel IAP + variables PG*)
./db-pool-saturated.sh --dry-run                   # diagnóstico + recomendación, no cambia nada
./db-pool-saturated.sh --idle-minutes 10           # on-call: ejecuta la Acción 1 con el umbral del runbook
./db-pool-saturated.sh --idle-minutes 30 --quiet   # cron preventivo (umbral más conservador)
./db-pool-saturated.sh --help
```

## Garantías de seguridad

- **Idempotente:** solo selecciona sesiones inactivas más allá del umbral. Repetirlo no encuentra nada nuevo que terminar.
- **Acotado:** solo sesiones de `--app-user` (por defecto `oms_app`). Rechaza `oms_ops` y `cloudsqladmin`, y nunca termina su propia sesión.
- **Suelo de seguridad:** `--idle-minutes` < 5 está prohibido, porque cortaría conexiones sanas del pool entre peticiones.
- **Exclusión mutua:** usa `flock`, así que el cron y el on-call no pueden ejecutarlo a la vez (sale con código 3).
- **Observable:** si `PUSHGATEWAY_URL` está definida, publica `ops_job_last_success_timestamp_seconds`, que vigila la alerta `KillIdleConnectionsCronStale`. Un `--dry-run` no publica, porque no cuenta como ejecución.

## Despliegue del cron (V3)

```bash
# Imagen con psql + el script, ejecutada como Cloud Run Job en la VPC (conector serverless → IP privada de Cloud SQL)
gcloud run jobs create oms-ops-kill-idle --region=europe-west3 \
  --image=europe-west3-docker.pkg.dev/oms-production/ops/oms-ops-tools:1.3.0 \
  --command=/opt/oms-ops/scripts/db-pool-saturated.sh --args=--idle-minutes,30,--quiet \
  --set-env-vars=PGHOST=10.20.0.3,PGDATABASE=oms,PGUSER=oms_ops,PUSHGATEWAY_URL=http://pushgateway.oms-ops.internal:9091 \
  --set-secrets=PGPASSWORD=oms-db-ops-password:latest \
  --vpc-connector=oms-serverless-connector --max-retries=0 --task-timeout=120s

gcloud scheduler jobs create http oms-ops-kill-idle --location=europe-west3 \
  --schedule="0 * * * *" --time-zone="Europe/Madrid" \
  --uri="https://run.googleapis.com/v2/projects/oms-production/locations/europe-west3/jobs/oms-ops-kill-idle:run" \
  --http-method=POST --oauth-service-account-email=oms-ops-scheduler@oms-production.iam.gserviceaccount.com
```

## Cómo se probó

Se ejecutó con un `psql` simulado que devuelve escenarios fijos (zombis + bloqueante, batch dominante, 0 zombis) para validar cada rama del árbol de decisión, la idempotencia (la segunda ejecución da "nada que terminar") y el rechazo de argumentos peligrosos. La prueba contra Postgres real es el criterio **H6** del [GameDay #1](../gameday/gameday-plan.md).
