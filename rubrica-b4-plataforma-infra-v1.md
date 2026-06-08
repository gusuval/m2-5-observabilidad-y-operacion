---
id: b4-plataforma-infra-v1
version: 1.0
language: es
sections:
  terraform_structure:
    title: "Estructura Terraform"
    weight: 0.15
    source: repo
  gcp_resources:
    title: "Implementación de recursos GCP"
    weight: 0.25
    source: repo
  plan_idempotency:
    title: "Plan limpio + idempotencia"
    weight: 0.10
    source: repo
  production_protections:
    title: "Protecciones de producción"
    weight: 0.10
    source: repo
  ansible_deploy:
    title: "Ansible deploy idempotente"
    weight: 0.15
    source: repo
  env_differences:
    title: "Diferencias staging / producción"
    weight: 0.10
    source: repo
  cicd_wif:
    title: "CI/CD con Workload Identity Federation"
    weight: 0.10
    source: repo
  readme_decisions:
    title: "README + decisiones"
    weight: 0.05
    source: repo
rules:
  round: integer
---

# Rúbrica · Bloque 4 · Plataforma e Infraestructura (AcmeOMS sobre GCP)

> Evalúa el "Trabajo final — Bloque 4 · Plataforma e infraestructura" del Máster en Ingeniería de Software. La rúbrica refleja al pie de la letra la del propio enunciado (8 bloques · 100 pts) traducida al formato data-driven del pipeline. Sin nota mínima — un trabajo incompleto puede suspender.

---

## Reglas generales

- **Notas enteras** (0-10), sin decimales.
- **Sin nota mínima** — un trabajo incompleto puede suspender (los puntos del enunciado se pueden perder enteros).
- **Cada check vale 1 punto**; cada nivel tiene un máximo declarado.
- **Tres niveles** dentro de cada bloque:
  - **Base** — lo mínimo exigible que pide el enunciado para considerar el bloque entregado.
  - **Notable** — cumple el criterio completo de la rúbrica original del Bloque 4.
  - **Excepcional** — calidad extra o cubre uno de los bonus opcionales del enunciado.
- **Penalizaciones del enunciado**: se modelan como **checks Base positivos** ("sin credenciales estáticas", "mismo SHA staging/prod") en las secciones correspondientes. Si el alumno incumple, el check no se marca y se pierden los puntos en esa sección.

---

## Ejes y pesos (espejo del enunciado, sección 5)

| Bloque | Peso | Pts original | Lo que cubre |
|---|---|---|---|
| **Estructura Terraform** | 15% | 15 | Módulos delimitados, variables tipadas, outputs, versions.tf |
| **Implementación recursos GCP** | 25% | 25 | VPC, Cloud SQL, Memorystore, Cloud Run, LB+CDN, Secret Manager |
| **Plan limpio + idempotencia** | 10% | 10 | `terraform plan` sin cambios en segunda ejecución |
| **Protecciones de producción** | 10% | 10 | `deletion_protection`, `prevent_destroy`, PITR, `database_version` pin |
| **Ansible deploy idempotente** | 15% | 15 | `changed=0` en segunda ejecución, módulos `google.cloud.*` |
| **Diferencias staging/prod** | 10% | 10 | `diff group_vars` solo capacidad/endpoints |
| **CI/CD con WIF** | 10% | 10 | OIDC, sin claves estáticas, trust policy con `attribute.repository` |
| **README + decisiones** | 5% | 5 | Cómo arrancar + 3 trade-offs |

**Nota final** = `0.15 × NotaTF + 0.25 × NotaGCP + 0.10 × NotaPlan + 0.10 × NotaProt + 0.15 × NotaAns + 0.10 × NotaEnv + 0.10 × NotaCI + 0.05 × NotaRDM`

---

<!-- section: terraform_structure -->

## Estructura Terraform (15%)

### Base — 1 pt cada uno (máx 4)
- [ ] `tf_modulos_delimitados` — Carpeta `terraform/modules/` con los 4 módulos del enunciado (`network`, `database`, `compute`, `iam`) cada uno en su propio `main.tf`.
- [ ] `tf_versions_lock` — `versions.tf` con `required_version` de Terraform y providers (`google`, `google-beta`) fijados a una versión menor (`~> X.Y.0`).
- [ ] `tf_variables_tipadas` — `variables.tf` con cada variable declarando `type` y `description` (no `variable "foo" {}` desnudo).
- [ ] `tf_main_compone_modulos` — `main.tf` raíz compone los 4 módulos pasando inputs explícitos (no inputs implícitos por orden).

### Notable — 1 pt cada uno (máx 3)
- [ ] `tf_outputs_utiles` — `outputs.tf` expone al menos LB IP/hostname, Cloud SQL connection name y Cloud Run URL.
- [ ] `tf_envs_separados` — `terraform/envs/staging.tfvars` y `terraform/envs/production.tfvars` con misma estructura y valores distintos.
- [ ] `tf_estructura_sigue_esqueleto` — La estructura sigue el esqueleto del enunciado (`docker/`, `terraform/`, `ansible/`, `.github/workflows/`).

### Excepcional — 1 pt cada uno (máx 3)
- [ ] `tf_backend_remoto_gcs` — Backend remoto en GCS con state locking (no estado local).
- [ ] `tf_modules_reusables` — Módulos parametrizados de forma que cualquier entorno los pueda reusar sin tocar el módulo (variables, no hardcoded).
- [ ] `tf_naming_conventions` — Convención de nombres consistente (`<proyecto>-<env>-<recurso>`) aplicada en todos los recursos.

**Total máximo: 10 pts**

---

<!-- section: gcp_resources -->

## Implementación de recursos GCP (25%)

### Base — 1 pt cada uno (máx 5)
- [ ] `gcp_vpc_2_zonas` — VPC personalizada con subredes en al menos 2 zonas de `europe-west3` (REG-GDPR-001).
- [ ] `gcp_cloudsql_regional` — Cloud SQL PostgreSQL con `availability_type = "REGIONAL"` (NFR-AVAIL-001).
- [ ] `gcp_memorystore_redis` — Memorystore Redis tier `STANDARD_HA` desplegado en la misma VPC (NFR-PERF-001).
- [ ] `gcp_cloud_run_autoscaling` — Cloud Run con `max_instance_count` configurado a 5× el pico medido (NFR-SCAL-001).
- [ ] `gcp_lb_https_tls13` — Cloud Load Balancer HTTPS con política SSL forzando TLS 1.3 (NFR-SEC-002).

### Notable — 1 pt cada uno (máx 3)
- [ ] `gcp_secret_manager_bindings` — Secret Manager con bindings IAM a la Service Account de Cloud Run (no secretos en env vars de Cloud Run).
- [ ] `gcp_cloud_cdn_habilitado` — Cloud CDN delante del bucket de la SPA con `enable_cdn = true` en el backend.
- [ ] `gcp_cloud_storage_spa` — Cloud Storage con la SPA + assets, bucket no público (acceso vía LB+CDN).

### Excepcional — 1 pt cada uno (máx 2)
- [ ] `gcp_multi_region_dr` — Réplica Cloud SQL en `europe-central2` + runbook documentado de failover (bonus +10 del enunciado).
- [ ] `gcp_cdn_politicas_finas` — Cache TTLs derivados de los headers de la app, negative caching y signed URLs para la SPA (bonus +5 del enunciado).

**Total máximo: 10 pts**

---

<!-- section: plan_idempotency -->

## Plan limpio + idempotencia (10%)

### Base — 1 pt cada uno (máx 4)
- [ ] `plan_clean_first_run` — `terraform init && terraform plan -var-file=envs/staging.tfvars` sale sin errores con un plan limpio (solo añade recursos, sin warnings de validación).
- [ ] `apply_succeeds` — `terraform apply -var-file=envs/staging.tfvars` crea toda la infra de staging sin errores.
- [ ] `plan_no_changes_second_run` — Segundo `terraform plan -var-file=envs/staging.tfvars` muestra literal "No changes" — confirmación de idempotencia.
- [ ] `plan_sin_warnings` — Plan sin warnings de deprecación de providers ni `experiments`.

### Notable — 1 pt cada uno (máx 3)
- [ ] `tf_lifecycle_ignore_changes` — Donde aplica (ej. labels añadidos por GCP a runtime, hash de imagen), `lifecycle { ignore_changes = [...] }` evita drift falso.
- [ ] `tf_provider_version_pinned` — Providers en `~> X.Y.0` (versión menor fijada, no `>= X.Y`).
- [ ] `tf_state_consistente` — Estado consistente con la realidad: `terraform refresh` no detecta drift en recursos críticos.

### Excepcional — 1 pt cada uno (máx 3)
- [ ] `tf_tflint_aprobado` — `tflint` (u otro linter) ejecuta sin avisos en el árbol completo.
- [ ] `tf_imports_documentados` — Si hay recursos pre-existentes (project, APIs habilitadas), imports documentados con `import` blocks o `terraform import` en README.
- [ ] `tf_separate_state_per_env` — Estado separado por entorno (workspaces o backends distintos) — staging y producción no comparten state file.

**Total máximo: 10 pts**

---

<!-- section: production_protections -->

## Protecciones de producción (10%)

### Base — 1 pt cada uno (máx 5)
- [ ] `prot_deletion_protection_db` — Cloud SQL con `deletion_protection = true`.
- [ ] `prot_prevent_destroy_db` — Bloque `lifecycle { prevent_destroy = true }` en el recurso de Cloud SQL.
- [ ] `prot_backup_pitr_14d` — `backup_configuration` con `point_in_time_recovery_enabled = true` y retención ≥ 14 días (OPS-005).
- [ ] `prot_database_version_pinneada` — `database_version` con versión específica (ej. `POSTGRES_15`, no genérica/latest).
- [ ] `prot_encryption_at_rest` — Encriptación en reposo configurada (al menos AES-256 por defecto explícito; NFR-SEC-001).

### Notable — 1 pt cada uno (máx 3)
- [ ] `prot_iam_least_privilege` — Service Accounts con roles mínimos (no `roles/owner` ni `roles/editor`; roles granulares por servicio).
- [ ] `prot_private_ip_db` — Cloud SQL sin IP pública (private service access via VPC peering).
- [ ] `prot_logs_audit_retention` — Cloud Logging con sink hacia un bucket de retención larga para audit trail inmutable (REG-GDPR-003).

### Excepcional — 1 pt cada uno (máx 2)
- [ ] `prot_cmek_aplicado` — CMEK propia (KMS) aplicada a Cloud SQL y al menos un bucket de Cloud Storage (bonus +5 del enunciado).
- [ ] `prot_runbook_restauracion` — Runbook documentado con el comando exacto de restauración PITR para el ejercicio mensual en staging (OPS-005).

**Total máximo: 10 pts**

---

<!-- section: ansible_deploy -->

## Ansible deploy idempotente (15%)

### Base — 1 pt cada uno (máx 4)
- [ ] `ans_playbook_deploy` — `playbooks/deploy.yml` que recibe `env` e `image_sha` por `-e` y despliega esa imagen al Cloud Run del entorno indicado.
- [ ] `ans_playbook_rollback` — `playbooks/rollback.yml` que devuelve el tráfico a la revisión inmediatamente anterior.
- [ ] `ans_idempotente_changed_0` — Segundo run consecutivo del playbook devuelve `changed=0` (idempotencia real).
- [ ] `ans_modulos_google_cloud` — Usa módulos de la colección `google.cloud` (`gcp_cloudrun_*`) — no `shell` ni `command` con `gcloud` directo.

### Notable — 1 pt cada uno (máx 3)
- [ ] `ans_manejo_revision_y_trafico` — Crea revisión nueva con tag y hace switch de tráfico explícito (no `traffic 100%` por defecto sin revisión nueva).
- [ ] `ans_check_mode_funciona` — `ansible-playbook --check` corre sin side effects en GCP (todos los módulos respetan check mode).
- [ ] `ans_role_oms_cloud_run` — Lógica organizada en role `roles/oms_cloud_run/tasks/main.yml` (no todo el playbook plano).

### Excepcional — 1 pt cada uno (máx 3)
- [ ] `ans_inventario_dinamico_gcp` — `inventory/gcp.yml` con plugin `gcp_compute` que descubre hosts por labels.
- [ ] `ans_bastion_vm_role` — Bonus +10 del enunciado: Compute Engine bastion con agente Datadog desplegado vía role Ansible, descubierto por inventario dinámico (label `role=bastion`).
- [ ] `ans_assertions_post_deploy` — Asserts post-deploy: healthcheck HTTP 200 contra el nuevo endpoint antes de switchear tráfico al 100%.

**Total máximo: 10 pts**

---

<!-- section: env_differences -->

## Diferencias staging / producción (10%)

### Base — 1 pt cada uno (máx 4)
- [ ] `env_group_vars_all` — `group_vars/all.yml` con lo común a todos los entornos (project_id, region, image_repo).
- [ ] `env_group_vars_staging` — `group_vars/staging.yml` con valores específicos de staging (capacidad menor, endpoints, tags).
- [ ] `env_group_vars_production` — `group_vars/production.yml` con valores específicos de producción.
- [ ] `env_diff_solo_capacidad` — `diff group_vars/staging.yml group_vars/production.yml` muestra **solo** capacidad/endpoints (max_instances, db tier, redis size) — nada de comportamiento condicional.

### Notable — 1 pt cada uno (máx 3)
- [ ] `env_diferencias_documentadas` — Cada diferencia entre `staging.yml` y `production.yml` justificada con 1 línea de comentario (defendible ante una pregunta).
- [ ] `env_tfvars_paralelos` — `terraform/envs/staging.tfvars` y `production.tfvars` con la misma estructura de variables, valores distintos.
- [ ] `env_no_comportamiento_distinto` — No hay `when: env == 'production'` que cambie lógica (solo capacidad/endpoints diferenciados).

### Excepcional — 1 pt cada uno (máx 3)
- [ ] `env_secrets_por_entorno` — Secret Manager con secretos por entorno (`oms-db-password-staging`, `oms-db-password-production`) con bindings IAM separados.
- [ ] `env_tags_y_labels` — Recursos GCP etiquetados con `env=staging|production` para facturación y filtrado en monitoring.
- [ ] `env_misma_imagen_promovida` — La `image_sha` promovida de staging a producción es **bit a bit la misma** (penalización del enunciado evitada: −10 si difiere).

**Total máximo: 10 pts**

---

<!-- section: cicd_wif -->

## CI/CD con Workload Identity Federation (10%)

### Base — 1 pt cada uno (máx 4)
- [ ] `cicd_workflow_existe` — `.github/workflows/ci-cd.yml` presente con jobs de build + test + deploy.
- [ ] `cicd_id_token_permission` — El workflow declara `permissions: id-token: write` (necesario para OIDC).
- [ ] `cicd_sin_gcp_sa_key_json` — Sin `GCP_SA_KEY_JSON`, sin claves JSON de Service Account en `secrets:` (penalización del enunciado evitada: −20 si `gitleaks` pilla una credencial estática).
- [ ] `cicd_workload_identity_provider` — Step de autenticación usa `google-github-actions/auth@v2` con `workload_identity_provider:` apuntando al pool de WIF.

### Notable — 1 pt cada uno (máx 3)
- [ ] `cicd_trust_policy_repo_constraint` — Trust policy del WIF restringe el acceso con `attribute.repository` específico (no abierta a cualquier repo de la org).
- [ ] `cicd_jobs_separados_con_needs` — Build / test / deploy en jobs separados con `needs:` (fail fast).
- [ ] `cicd_promote_misma_imagen` — Job de promote a producción referencia el SHA construido en staging (no rebuild).

### Excepcional — 1 pt cada uno (máx 3)
- [ ] `cicd_sign_imagen` — Imagen firmada con `cosign` (o equivalente) tras el build.
- [ ] `cicd_tag_release_v` — Push de tag `vX.Y.Z` dispara el deploy a producción (no en cada push a `main`).
- [ ] `cicd_gitleaks_step` — Step de `gitleaks` (u otro scanner) corre en el workflow para detectar credenciales estáticas en cada commit.

**Total máximo: 10 pts**

---

<!-- section: readme_decisions -->

## README + decisiones (5%)

### Base — 1 pt cada uno (máx 4)
- [ ] `rdm_existe` — `README.md` presente en la raíz del repo.
- [ ] `rdm_como_arrancar` — Sección "Cómo arrancar" con los comandos exactos para crear staging desde cero (`terraform init`, `apply`, `ansible-playbook deploy.yml`).
- [ ] `rdm_3_decisiones` — Sección "Decisiones" con 3 trade-offs explicados (ej. Cloud Run vs GKE, multi-region vs single-region, Memorystore vs Redis Enterprise).
- [ ] `rdm_3_cambios_ia` — Documenta 3 cambios concretos hechos al borrador generado por IA antes de aprobar el código (política de uso de IA, sección 8 del enunciado).

### Notable — 1 pt cada uno (máx 3)
- [ ] `rdm_diagrama_arquitectura` — Diagrama de la arquitectura desplegada (ASCII o mermaid) coherente con el dibujo de la sección 2 del enunciado.
- [ ] `rdm_lista_comandos_validacion` — Los 7 comandos de "Lo que tiene que pasar" (sección 4 del enunciado) listados en el README con resultado esperado.
- [ ] `rdm_referencias_videos` — Referencias explícitas a los vídeos del bloque (V1 Docker, V2 Cloud, V3 Terraform, V4 WIF, V5 Ansible) donde se justifica cada decisión técnica.

### Excepcional — 1 pt cada uno (máx 3)
- [ ] `rdm_expand_contract_documentado` — Documenta el flujo expand-and-contract para hacer un cambio destructivo de schema sin downtime (bonus +5 del enunciado).
- [ ] `rdm_costes_estimados` — Sección de costes mensuales estimados de la infra (Cloud SQL + Memorystore + Cloud Run + LB + storage) por entorno.
- [ ] `rdm_diagrama_secuencia_deploy` — Diagrama de secuencia del flujo de deploy completo (PR → CI build → WIF auth → push image → Ansible deploy → switch tráfico).

**Total máximo: 10 pts**

---

## Antipatrones específicos que penalizan

> Estas son las **penalizaciones globales** del enunciado (sección 5). En esta rúbrica se reflejan como checks Base concretos en las secciones afectadas. Si los incumples, pierdes la nota de la sección entera donde encajan.

- **Credencial estática en el repo** (`gitleaks` la pilla): rompe `cicd_sin_gcp_sa_key_json` y suele rompe varias secciones a la vez. Equivalente a la penalización de −20 pts del enunciado.
- **`image_sha` distinto entre staging y producción**: rompe `env_misma_imagen_promovida` y `cicd_promote_misma_imagen`. Equivalente a la penalización de −10 pts del enunciado.
- **`terraform apply` que rompe producción en el entorno de evaluación**: rompe `apply_succeeds`, `plan_no_changes_second_run` y `prot_prevent_destroy_db`. Equivalente a la penalización de −5 pts del enunciado.
- **Mezclar lógica con configuración** (`when: env == 'production'` en lugar de variar capacidad): rompe `env_no_comportamiento_distinto` y la nota de "Diferencias staging/prod" al completo.
- **Playbook que no es idempotente** (`changed > 0` en segunda ejecución): rompe `ans_idempotente_changed_0` y baja "Ansible deploy" en dos puntos.
- **Trust policy de WIF abierta** (sin `attribute.repository` constraint): rompe `cicd_trust_policy_repo_constraint`. Riesgo de seguridad alto incluso si el workflow funciona.
- **ADR de IA sin revisar**: no documentar 3 cambios concretos hechos al borrador (sección 8 del enunciado) rompe `rdm_3_cambios_ia` y muestra que el alumno no entendió la política.
