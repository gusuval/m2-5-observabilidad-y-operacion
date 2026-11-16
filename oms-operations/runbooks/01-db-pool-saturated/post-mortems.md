# Post-mortems del runbook: db-pool-saturated

> Evolución del runbook a lo largo de 3 ocurrencias del incidente: V1 manual → V2 con comandos → V3 automatizada.
> Formato blameless: se analizan sistemas y procesos, no personas. Los on-call se identifican por rol.
> *Escenarios simulados para el trabajo del Bloque 5, construidos sobre la arquitectura real del OMS.*

---

## Versión 1: mitigación manual (PM #1)

**Fecha del incidente:** lunes 9 de marzo de 2026, 09:07 → 09:30 CET
**Duración del incidente:** 23 minutos (primer 503 a las 09:07 → verificación en verde a las 09:30)
**Detectado por:** quejas de usuarios en el chat de soporte a las 09:10. Todavía no había alerta del pool; la única alerta era un error rate genérico > 5 %, que disparó a las 09:14.
**On-call:** SRE de guardia (semana 11)
**Impacto:** ~180 intentos de `POST /api/orders` con 503 en 23 min (≈ 31 % de los intentos de esa franja), unos 40 pedidos perdidos según la comparación con el lunes anterior. **Agotó el error budget del SLO de Orders** (99,9 % → se permiten ~70 peticiones fallidas en 28 d; fallaron ~180). La política pasó a ⚫ y hubo freeze de features hasta el 23/03.

### Lo que ocurrió
A las 09:00 salió la campaña de email semanal y el tráfico de checkout subió un 400 % en 10 minutos. Cloud Run escaló `oms-orders` de 3 a 14 instancias. Con cada instancia abriendo su pool de 10 conexiones y ~80 sesiones `idle` de hace más de 1 h ocupando hueco, a las 09:07 Postgres llegó a `max_connections` y las instancias nuevas no conseguían conexión. Los pools daban timeout a los 2 s y devolvían 503.

### Cómo se mitigó (improvisado, sin runbook)
El on-call no tenía acceso `psql` preparado. Abrió Cloud SQL Studio en la consola, tardó ~5 min en dar con la vista de sesiones y vio decenas de conexiones `idle` de `oms-orders` con `state_change` de hacía horas. Las terminó **una a una** desde la UI, unas 60 en total. A las 09:25 el pool había bajado al 60 % y los 503 pararon. Esperó 5 min y cerró.

### Aprendizajes y acciones
- **No teníamos alerta del pool.** Detectamos el problema por los clientes, 3 minutos antes que por nuestra propia alerta genérica. → Crear `DBConnectionPoolSaturated` al 90 % durante 3 min. *(Hecho, 10/03)*
- El on-call tardó ~5 min solo en encontrar las sesiones en la consola, y terminar 60 sesiones a mano llevó otros 8. → Runbook V1 con los pasos. *(Hecho, 11/03)*
- Las conexiones idle > 1 h no deberían existir. → Ticket **OMS-1234**: investigar por qué la app no devuelve las conexiones.
- Marketing lanza campañas sin avisar a ingeniería. → Acordado un aviso con 24 h en `#oms-releases` para envíos de más de 20.000 emails.

### Versión 1 del runbook tras este post-mortem

> **Runbook v1 (texto literal):**
> "Si dispara DBConnectionPoolSaturated: abrir Cloud SQL Studio de `oms-pg-prod`, mirar las sesiones activas
> y buscar conexiones idle de más de 1 h. Terminarlas. Esperar a que el pool baje del 70 %."

(Eso es todo. Dice **qué** hacer, pero no **cómo**. Por eso hay V2.)

---

## Versión 2: diagnóstico y comandos exactos (PM #2)

**Fecha del incidente:** lunes 6 de abril de 2026, 09:04 → 09:12 CET
**Duración del incidente:** 8 minutos (alerta 09:04 → verificado 09:12; con impacto en el usuario solo 09:05–09:09)
**Detectado por:** alerta `DBConnectionPoolSaturated` a las 09:04. Respuesta del on-call a las 09:05.
**On-call:** SRE de guardia (semana 15), otra persona distinta a la de PM #1.
**Impacto:** 22 peticiones con 503. Consumió el 31 % del budget del periodo (política en 🟡).

### Qué se vio durante esta segunda ocurrencia
Mismo detonante: campaña de los lunes. La alerta nueva funcionó y el on-call tenía el runbook V1, pero:
1. "Terminarlas" no decía **cómo**. El on-call buscó la sintaxis de `pg_terminate_backend` y la cláusula `WHERE` correcta, y perdió ~4 min.
2. Su primera query terminó también una sesión de `oms_ops` (la suya propia en otra pestaña) porque el filtro no excluía usuarios de operación.
3. No había forma de saber si el problema eran zombis o un pico real. Esta vez el 30 % de las conexiones eran de `oms-batch-export`, porque un usuario había pedido un export CSV de 4 años de pedidos. El V1 no lo contemplaba.
4. Al terminar, el on-call no sabía cuánto esperar para dar el incidente por cerrado.

### Qué cambió en el runbook (V1 → V2)
- **SQL exacto y copiable** en cada paso, con filtro `usename = 'oms_app'` para no tocar nunca sesiones de operación ni de replicación.
- **Diagnóstico previo antes de actuar**: ¿quién consume (por `application_name` y `state`)?, ¿hay zombis?, ¿hay locks? Cada salida lleva a una acción concreta.
- **Acciones ordenadas de menor a mayor impacto**: kill idle → pausar batch → réplica / pool → degradar. La pausa del batch se hizo **como feature flag** (`orders.batch-jobs-paused`) en vez de "matar el job".
- **Verificación medible**: pool < 70 % + 0 esperas + 5xx < 0,1 % durante 10 min.
- **Setup de sesión común** (túnel IAP + `psql` preconfigurado) en `runbooks/README.md`, para no perder minutos en la conexión.
- **Rollback** de cada acción, incluido el recálculo de la fórmula de capacidad antes de tocar `DB_POOL_SIZE`.

### Acciones derivadas
- `oms-batch-export` pasa a tener su propio pool limitado a 3 conexiones (`DB_POOL_SIZE=3`). *(Hecho, sprint 15)*
- Alerta warning `DBConnectionPoolPressure` al 75 %: avisar antes, sin despertar a nadie. *(Hecho)*
- Root cause de OMS-1234 encontrada: el handler de errores de `payments` hace `return` sin liberar el cliente si Stripe lanza timeout dentro de la transacción. El cliente se queda `idle in transaction` hasta que lo mata `idle_in_transaction_session_timeout`, que **no estaba configurado**. Fix en curso.
- Evaluar un cron preventivo que haga la Acción 1 cada hora.

---

## Versión 3: automatización preventiva (PM #3)

**Fecha:** lunes 11 de mayo de 2026, campaña de las 09:00. **El incidente no llegó a producirse.**

### Qué pasó (o, mejor, qué NO pasó)
La campaña generó el mismo pico (+380 %). A las 08:00 y a las 09:00, el job preventivo `kill-idle-connections` (Cloud Scheduler → Cloud Run Job, cada hora) había terminado 11 y 3 sesiones idle de más de 30 min respectivamente. El pool llegó a un máximo del **78 %**: saltó la alerta warning `DBConnectionPoolPressure` en Slack, pero **no se paginó a nadie y no hubo ni un solo 503**.

### Qué cambió en el runbook (V2 → V3)
- **La Acción 1 está automatizada** en `scripts/db-pool-saturated.sh`: idempotente, con `--dry-run`, y publica `ops_job_last_success_timestamp_seconds`. Se ejecuta cada hora con umbral de 30 min y, a mano, con el umbral del runbook (10 min).
- El mismo script en modo `--dry-run` **ejecuta el diagnóstico 1–3 y recomienda la acción**. El on-call ya no tiene que interpretar 3 salidas de SQL a las 3 AM.
- **Defensa en la base de datos:** `idle_in_transaction_session_timeout = 60s` en los flags de Cloud SQL. Postgres mata por sí solo las sesiones abandonadas a mitad de transacción.
- **Nueva alerta** `KillIdleConnectionsCronStale`: si la automatización deja de ejecutarse durante 3 h, nos enteramos. Una automatización que falla en silencio es peor que no tenerla.
- La alerta critical **se mantiene** al 90 %, porque un pico de tráfico real puede saturar el pool sin zombis. La warning al 75 % pasa a ser la señal habitual.
- El fix de OMS-1234 se desplegó el 28/04. Desde entonces el cron encuentra < 5 zombis por hora (antes eran > 60). El cron se mantiene como red de seguridad ante bugs futuros parecidos.

### El runbook como script

- **Script:** [`../../scripts/db-pool-saturated.sh`](../../scripts/db-pool-saturated.sh)
- **Uso manual:** `./db-pool-saturated.sh --dry-run` (diagnóstico + recomendación) · `./db-pool-saturated.sh --idle-minutes 10` (ejecuta la Acción 1)
- **Cron:** `0 * * * *` (Cloud Scheduler `oms-ops-kill-idle`, zona `Europe/Madrid`) → Cloud Run Job `oms-ops-kill-idle` con `--idle-minutes 30`
- **Owner:** equipo SRE
- **Salud del script:** `ops_job_last_success_timestamp_seconds{job="kill-idle-connections"}`, vigilada por `KillIdleConnectionsCronStale`

### Métrica del runbook a lo largo de la evolución

| | V1 (manual) | V2 (comandos) | V3 (preventivo) |
|---|---|---|---|
| Veces disparado por incidente real | 1 | 1 | 0 (solo un warning) |
| Detección | Clientes (+3 min antes que la alerta) | Alerta propia | Alerta warning sin impacto |
| Tiempo medio de mitigación | 23 min | 8 min | 0 (no hubo impacto) |
| Peticiones fallidas | ~180 | 22 | 0 |
| Error budget consumido | > 100 % (⚫ freeze) | 31 % (🟡) | 0 % |
| Toil del on-call | Alto (UI, 60 clics) | Medio (copiar y pegar SQL) | Cero |
| Siguiente mejora | Comandos exactos | Automatizar y arreglar la causa | Capacity planning para Black Friday (×5) |

---

## Conclusión

El recorrido de este runbook es el ciclo del Vídeo 3 completo. En V1 apagamos el fuego con las manos, el cliente se enteró antes que nosotros y gastamos el budget entero en una mañana (con freeze incluido). En V2 el runbook pasó a ser una herramienta: SQL exacto, decisiones por salida y verificación con números. El cambio clave fue tratar un job batch como un **flag** y no como un proceso al que matar. En V3 dejamos de reaccionar: una automatización vigilada y un parámetro de Postgres hacen que el incidente no ocurra, y el fix de la causa raíz quita la necesidad de que el on-call intervenga. El aprendizaje más incómodo vino del V3: **la automatización también necesita su propia alerta**, porque el día que el cron falle en silencio volvemos al V1 sin darnos cuenta. Lo pendiente ya no es operativo sino de capacidad: con el pico ×5 de Black Friday, la fórmula `max-instances × DB_POOL_SIZE < 380` deja de cuadrar, y eso se resolverá con PgBouncer o con un tier mayor de Cloud SQL, no con un runbook.
