#!/usr/bin/env bash
# =============================================================================
# db-pool-saturated.sh: automatización del runbook 01-db-pool-saturated (V3)
#
#   --dry-run   Ejecuta el DIAGNÓSTICO (pasos 1-3 del runbook), imprime la acción
#               recomendada y NO modifica nada. Es lo primero que lanza el on-call.
#   (sin flag)  Diagnóstico + ACCIÓN 1: termina las sesiones de oms_app que llevan
#               más de --idle-minutes inactivas (idle / idle in transaction).
#
# Idempotente: el filtro solo selecciona sesiones inactivas más allá del umbral.
# Una segunda ejecución inmediata no encuentra nada que terminar y sale con 0.
# Nunca toca sesiones de otros usuarios (oms_ops, replicación, cloudsqladmin)
# ni la propia sesión.
#
# Las acciones 1b, 2, 3 y 4 del runbook NO se automatizan a propósito: requieren
# criterio humano (terminar un bloqueante, pausar un batch con impacto en
# usuarios, cambiar la capacidad). El script solo las RECOMIENDA.
#
# Conexión: variables estándar de libpq (PGHOST, PGPORT, PGDATABASE, PGUSER,
# PGPASSWORD), igual que en el "setup de sesión" de runbooks/README.md.
# En el Cloud Run Job oms-ops-kill-idle las inyecta Secret Manager.
#
# Uso:
#   ./db-pool-saturated.sh --dry-run
#   ./db-pool-saturated.sh --idle-minutes 10              # uso manual del on-call
#   ./db-pool-saturated.sh --idle-minutes 30 --quiet      # cron preventivo horario
#
# Códigos de salida: 0 OK · 1 error de uso · 2 sin conexión a BD · 3 otro script en ejecución
# =============================================================================
set -euo pipefail

# ─── Valores por defecto ─────────────────────────────────────────────────────
DRY_RUN=false
IDLE_MINUTES=10
APP_USER="oms_app"
DB_NAME="${PGDATABASE:-oms}"
QUIET=false
PUSHGATEWAY_URL="${PUSHGATEWAY_URL:-}"          # p.ej. http://pushgateway.oms-ops.internal:9091
LOCK_FILE="${LOCK_FILE:-/tmp/db-pool-saturated.lock}"
# Umbrales del diagnóstico (los mismos que en el runbook)
ZOMBIE_THRESHOLD=20          # > 20 sesiones idle largas → zombis
DOMINANT_APP_PCT=70          # una app con > 70 % de las conexiones → batch
BLOCKER_MIN_VICTIMS=5        # un pid bloqueando a ≥ 5 sesiones → tapón

usage() {
  sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
  cat <<EOF

Opciones:
  --dry-run              Solo diagnóstico y recomendación; no termina ninguna sesión.
  --idle-minutes N       Umbral de inactividad para la Acción 1 (por defecto: ${IDLE_MINUTES}; mínimo 5).
  --app-user USER        Usuario de BD cuyas sesiones se pueden terminar (por defecto: ${APP_USER}).
  --quiet                Solo imprime el resumen final (para el cron).
  -h, --help             Esta ayuda.
EOF
}

log()  { $QUIET || printf '%s %s\n' "$(date -Iseconds)" "$*"; }
say()  { printf '%s %s\n' "$(date -Iseconds)" "$*"; }
die()  { printf '%s ERROR: %s\n' "$(date -Iseconds)" "$*" >&2; exit "${2:-1}"; }

# ─── Argumentos ──────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)       DRY_RUN=true; shift ;;
    --idle-minutes)  [[ $# -ge 2 ]] || die "--idle-minutes necesita un valor"; IDLE_MINUTES="$2"; shift 2 ;;
    --app-user)      [[ $# -ge 2 ]] || die "--app-user necesita un valor"; APP_USER="$2"; shift 2 ;;
    --quiet)         QUIET=true; shift ;;
    -h|--help)       usage; exit 0 ;;
    *)               usage >&2; die "opción desconocida: $1" ;;
  esac
done

[[ "$IDLE_MINUTES" =~ ^[0-9]+$ ]] || die "--idle-minutes debe ser un entero"
(( IDLE_MINUTES >= 5 )) || die "--idle-minutes < 5 no está permitido: terminaría conexiones de pools sanos entre peticiones"
[[ "$APP_USER" =~ ^[a-z_][a-z0-9_]*$ ]] || die "--app-user con caracteres no válidos"
[[ "$APP_USER" != "oms_ops" && "$APP_USER" != "cloudsqladmin" ]] || die "no se permiten sesiones de $APP_USER"
command -v psql >/dev/null || die "psql no está instalado"

# ─── Un solo script a la vez (cron + on-call en paralelo = doble ejecución) ───
exec 9>"$LOCK_FILE"
flock -n 9 || die "otra ejecución de $(basename "$0") está en curso (lock: $LOCK_FILE)" 3

# psql en modo no interactivo, sin .psqlrc, salida sin alinear, para parsear.
PSQL=(psql -X -q -At -v ON_ERROR_STOP=1 -d "$DB_NAME" -F $'\t')
q() { "${PSQL[@]}" -c "$1"; }

push_metrics() {
  # Métricas para Prometheus vía Pushgateway (vigiladas por KillIdleConnectionsCronStale).
  [[ -n "$PUSHGATEWAY_URL" ]] || return 0
  $DRY_RUN && return 0      # un dry-run no cuenta como ejecución del cron
  cat <<EOF | curl -sf --max-time 5 --data-binary @- \
      "$PUSHGATEWAY_URL/metrics/job/kill-idle-connections" >/dev/null \
      || log "WARN: no se pudo publicar en Pushgateway (no afecta al resultado)"
# TYPE ops_job_last_success_timestamp_seconds gauge
ops_job_last_success_timestamp_seconds $(date +%s)
# TYPE ops_job_terminated_sessions gauge
ops_job_terminated_sessions $1
EOF
}

# ─── 0. Conectividad ─────────────────────────────────────────────────────────
q "SELECT 1" >/dev/null 2>&1 || die "no hay conexión a Postgres (¿setup de sesión hecho? ¿túnel IAP activo?)" 2
log "Conectado a ${PGHOST:-localhost}:${PGPORT:-5432}/${DB_NAME} como ${PGUSER:-$USER}. Modo: $($DRY_RUN && echo DRY-RUN || echo EJECUCIÓN). Umbral idle: ${IDLE_MINUTES} min."

# ─── 1. ¿Quién consume las conexiones? ───────────────────────────────────────
log "── Diagnóstico 1: consumo por aplicación y estado"
CONSUMERS="$(q "
  SELECT coalesce(nullif(application_name,''),'(sin nombre)'), state, count(*),
         round(100.0 * count(*) / sum(count(*)) OVER (), 1)
  FROM pg_stat_activity
  WHERE datname = '${DB_NAME}' AND backend_type = 'client backend'
  GROUP BY 1, 2 ORDER BY 3 DESC;")"
$QUIET || { printf '   %-28s %-32s %6s %6s\n' APP STATE N PCT; \
            printf '%s\n' "$CONSUMERS" | awk -F'\t' 'NF{printf "   %-28s %-32s %6s %5s%%\n",$1,$2,$3,$4}'; }

TOTALS="$(q "SELECT count(*), current_setting('max_connections') FROM pg_stat_activity;")"
TOTAL_CONN="${TOTALS%%$'\t'*}"; MAX_CONN="${TOTALS##*$'\t'}"
log "   Conexiones totales: ${TOTAL_CONN} / ${MAX_CONN}"

# App dominante (sumando todos sus estados), excluyendo la propia app principal
DOMINANT="$(q "
  SELECT application_name, round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS pct
  FROM pg_stat_activity
  WHERE datname = '${DB_NAME}' AND backend_type = 'client backend'
  GROUP BY 1 ORDER BY 2 DESC LIMIT 1;")"
DOMINANT_APP="${DOMINANT%%$'\t'*}"; DOMINANT_PCT="${DOMINANT##*$'\t'}"

# ─── 2. Zombis ──────────────────────────────────────────────────────────────
log "── Diagnóstico 2: sesiones de ${APP_USER} inactivas > ${IDLE_MINUTES} min"
ZOMBIES="$(q "
  SELECT count(*) FROM pg_stat_activity
  WHERE datname = '${DB_NAME}' AND usename = '${APP_USER}'
    AND state IN ('idle','idle in transaction','idle in transaction (aborted)')
    AND state_change < now() - interval '${IDLE_MINUTES} minutes'
    AND pid <> pg_backend_pid();")"
log "   Sesiones zombi candidatas: ${ZOMBIES}"

# ─── 3. Bloqueos ────────────────────────────────────────────────────────────
log "── Diagnóstico 3: sesiones que bloquean a otras"
BLOCKERS="$(q "
  SELECT b.pid, count(*) AS victims, coalesce(bl.application_name,''),
         coalesce(extract(epoch FROM now() - bl.xact_start)::int, 0)
  FROM pg_stat_activity w
  JOIN LATERAL unnest(pg_blocking_pids(w.pid)) AS b(pid) ON true
  JOIN pg_stat_activity bl ON bl.pid = b.pid
  WHERE w.datname = '${DB_NAME}'
  GROUP BY b.pid, bl.application_name, bl.xact_start
  ORDER BY victims DESC LIMIT 5;")"
if [[ -z "$BLOCKERS" ]]; then
  log "   Ninguna."
else
  $QUIET || printf '%s\n' "$BLOCKERS" | awk -F'\t' '{printf "   pid %-8s bloquea a %-3s sesiones · app=%s · transacción abierta %ss\n",$1,$2,$3,$4}'
fi
TOP_BLOCKER_PID="$(printf '%s\n' "$BLOCKERS" | awk -F'\t' -v m="$BLOCKER_MIN_VICTIMS" 'NF && $2>=m && $4>=120 {print $1; exit}')"

# ─── Recomendación (mismo árbol de decisión que el runbook) ──────────────────
RECS=()
(( ZOMBIES > ZOMBIE_THRESHOLD )) && RECS+=("ACCIÓN 1: terminar ${ZOMBIES} sesiones idle (> ${ZOMBIE_THRESHOLD} → patrón OMS-1234)")
(( ZOMBIES > 0 && ZOMBIES <= ZOMBIE_THRESHOLD )) && RECS+=("ACCIÓN 1 (menor): hay ${ZOMBIES} sesiones idle; terminarlas es inocuo")
[[ -n "$TOP_BLOCKER_PID" ]] && RECS+=("ACCIÓN 1b: el pid ${TOP_BLOCKER_PID} bloquea a ≥ ${BLOCKER_MIN_VICTIMS} sesiones durante > 2 min (manual: pg_cancel_backend → pg_terminate_backend)")
if [[ -n "$DOMINANT_APP" && "$DOMINANT_APP" != "oms-orders" && "$DOMINANT_APP" != "oms-catalog" ]] \
   && awk -v p="$DOMINANT_PCT" -v t="$DOMINANT_APP_PCT" 'BEGIN{exit !(p>t)}'; then
  RECS+=("ACCIÓN 2: '${DOMINANT_APP}' tiene el ${DOMINANT_PCT} % de las conexiones → flag_on orders.batch-jobs-paused")
fi
(( ${#RECS[@]} == 0 )) && RECS+=("Sin zombis, bloqueos ni batch dominante → es CAPACIDAD: diagnóstico 4 y ACCIÓN 3 (réplica / pool) del runbook")

say "── Recomendación:"
for r in "${RECS[@]}"; do say "   • $r"; done

# ─── Acción 1 ───────────────────────────────────────────────────────────────
if $DRY_RUN; then
  say "DRY-RUN: no se ha terminado ninguna sesión. Para ejecutar la Acción 1: $(basename "$0") --idle-minutes ${IDLE_MINUTES}"
  exit 0
fi

if (( ZOMBIES == 0 )); then
  say "Acción 1: nada que terminar (idempotente: 0 sesiones cumplen el filtro)."
  push_metrics 0
  exit 0
fi

log "── Ejecutando Acción 1"
TERMINATED="$(q "
  WITH victims AS (
    SELECT pid, application_name, state, now() - state_change AS idle_for
    FROM pg_stat_activity
    WHERE datname = '${DB_NAME}' AND usename = '${APP_USER}'
      AND state IN ('idle','idle in transaction','idle in transaction (aborted)')
      AND state_change < now() - interval '${IDLE_MINUTES} minutes'
      AND pid <> pg_backend_pid()
  )
  SELECT pid, application_name, state, idle_for, pg_terminate_backend(pid) FROM victims;")"
N_TERMINATED="$(printf '%s\n' "$TERMINATED" | awk -F'\t' 'NF && $5=="t"' | wc -l | tr -d ' ')"
$QUIET || printf '%s\n' "$TERMINATED" | awk -F'\t' 'NF{printf "   terminada pid=%s app=%s estado=%s inactiva=%s ok=%s\n",$1,$2,$3,$4,$5}'

say "Acción 1 completada: ${N_TERMINATED} sesiones terminadas (umbral ${IDLE_MINUTES} min). Verifica: max(db_pool_in_use / db_pool_size) < 0.7 durante 10 min."
push_metrics "$N_TERMINATED"
