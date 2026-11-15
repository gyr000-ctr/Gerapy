# Versioned Deployment

Gerapy still defaults to **single-version** deployment, with behaviour identical
to older releases. On top of that it now offers optional versioned deployment:
immutable versions, pre-deploy health checks, atomic switching, automatic
rollback on failure, version retention and an audit trail.

## 1. Concepts

- **Immutable version (ProjectVersion)**: every built or uploaded egg is
  identified by a content hash (sha256) and stored under
  `versions/<project>/<version>/`, containing `package.egg` and
  `manifest.json`. Rebuilding unchanged sources reuses the same version;
  nothing is ever overwritten.
- **Version id**: `rel-<14-digit timestamp>-<5-digit monotonic counter>-<8-digit uuid>-<8-digit hash>`,
  fixed width so lexicographic order matches chronological order. Scrapyd always
  schedules the lexicographically highest version.
- **Staging version**: health checks use the `0stg-` prefix, which always sorts
  below any `rel-` release id, so a staging egg can never become the active
  version.
- **Deployment record (DeploymentRecord)**: every attempt (success / failure /
  rollback / rejection) is persisted with the version diff, health check
  results, rollback reason, running-job policy and operator, and is queryable
  through the audit API.

## 2. Atomic switch and automatic rollback

A deployment runs these steps in order:

1. Acquire a `(client, project)` file lock (flock). A concurrent deployment is
   rejected outright (HTTP 409).
2. Pre-flight: the Scrapyd daemon must be reachable (this surfaces port
   conflicts and a dead daemon).
3. Compute the file-level diff against the previously active version and run a
   data-compatibility check: a new version that drops data-definition files
   (`models.py`, `items.py`, `migrations`, ...) is blocked unless explicitly
   overridden.
4. Apply the running-job policy: `continue` (default; Scrapyd lets old jobs
   finish undisturbed), `wait` (block until they finish; fail on timeout while
   keeping the old version) or `abort` (refuse while jobs are running).
5. Upload the egg under a staging version id. Scrapyd imports the egg and
   enumerates its spiders, so missing dependencies or broken code fail here; the
   staging egg is removed and **the previous version stays the highest, active
   version throughout**.
6. Once health checks pass, upload the egg again under its real release id.
   That single call is the moment traffic switches (the project directory is
   never copied; only the egg is uploaded).
7. Verify the active version and spider enumeration again after the switch; on
   failure the fresh release is deleted immediately, automatically restoring
   the previous version.
8. Prune remote and local artifacts according to `keep_versions` (default 1);
   versions still active on a client are protected.

No failure path leaves a half-switched state: the previous version remains
servable for the whole operation.

## 3. API

Every endpoint requires token authentication
(`Authorization: Token <token>`).

| Method | Path | Purpose |
| --- | --- | --- |
| GET/POST | `/api/project/<name>/versions` | list versions / build an immutable version from the working tree |
| POST | `/api/project/<name>/versions/upload` | register an uploaded egg as an immutable version (no switch) |
| GET | `/api/project/<name>/versions/diff?from=&to=` | file diff between two versions |
| POST | `/api/client/<id>/project/<name>/deploy` | backward-compatible deploy (single version by default) |
| POST | `/api/client/<id>/project/<name>/version/<version>/deploy` | deploy a specific version |
| POST | `/api/client/<id>/project/<name>/version/<version>/rollback` | roll back to a specific version |
| GET | `/api/client/<id>/preflight?project=<name>` | standalone health pre-flight |
| GET | `/api/deployment/audit` | audit query (filters: project/client_id/status/version/operator/reason; paging: limit/offset) |
| GET | `/api/deployment/audit/<id>` | one audit record including its full sanitized log |

Optional request body for deploys:

```json
{
  "keep_versions": 3,
  "running_policy": "continue",
  "expected_spiders": ["sample"],
  "enforce_data_compatibility": true
}
```

## 4. Failure categories

The audit `reason` field is one of: `build_failed`, `port_conflict`,
`dependency_missing`, `health_check_failed`, `client_unreachable`,
`running_tasks`, `concurrent_deployment`, `data_incompatible`, `unknown`.

## 5. Logs, artifacts and secret handling

- Per-deployment logs go to `logs/deploy/deploy-<record_id>.log`; each file is
  size-bounded and rotated, and the number of files is capped. The audit table
  is likewise bounded; only the oldest rows are dropped.
- All log/audit text is sanitized before being written: environment variables
  such as `PASSWORD`/`TOKEN`/`SECRET`/`API_KEY`, JSON credential fields and
  basic-auth embedded in URLs are replaced with `***`.
- Sensitive files (`.env`, `*.pem`, `*.key`, ...) contribute only their hash to
  a manifest; their contents are never read into manifests or logs.
- A switch uploads an egg; it never copies the whole project directory.

## 6. Settings (environment variables)

| Variable | Default | Meaning |
| --- | --- | --- |
| `VERSIONS_FOLDER` | `versions` | immutable artifact directory |
| `DEPLOY_LOGS_FOLDER` | `logs/deploy` | deployment log directory |
| `VERSION_RETENTION_COUNT` | `10` | versions kept locally per project |
| `DEPLOY_LOG_RETENTION_COUNT` | `1000` | deployment log files retained |
| `DEPLOY_LOG_MAX_BYTES` | `131072` | per-file deployment log byte cap |
| `DEPLOY_RECORD_RETENTION_COUNT` | `1000` | audit table row cap |
| `DEPLOY_LOCK_TIMEOUT` | `5` | seconds to wait for the deployment lock |
| `HEALTH_CHECK_TIMEOUT` | `5` | health check timeout in seconds |
| `HEALTH_CHECK_RETRIES` | `2` | read-side health check retries |
