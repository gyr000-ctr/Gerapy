"""
Versioned deployment engine.

A deployment never overwrites the running version. Builds produce immutable
eggs stored under VERSIONS_FOLDER; a deploy uploads the egg to scrapyd first
under a staging version id that always sorts below every release id, runs the
health checks against it, and only then uploads the egg again under the real
release id. Because scrapyd always schedules the lexicographically highest
version, the previous release keeps serving traffic (including already running
jobs) until the health-checked release is in place. If anything fails the
staging version is removed and the previous release stays active -- there is no
half-switched state to recover from.
"""
import errno
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

from gerapy import get_logger
from gerapy.settings import (
    VERSIONS_FOLDER, DEPLOY_LOGS_FOLDER,
    VERSION_RETENTION_COUNT, DEPLOY_LOG_RETENTION_COUNT, DEPLOY_LOG_MAX_BYTES,
    DEPLOY_RECORD_RETENTION_COUNT, DEPLOY_LOCK_TIMEOUT, HEALTH_CHECK_TIMEOUT,
    HEALTH_CHECK_RETRIES, SECRET_KEY_PATTERNS,
)
from gerapy.server.core.utils import get_scrapyd, scrapyd_url

import requests

logger = get_logger(__name__)

# staging ids start with 's' (0x73), below the digit '1' (0x31? no -- '1' is
# 0x31, 's' is 0x73, so use a prefix below digits instead): every release id
# starts with 'rel-', staging ids with '0stg-', and '0' (0x30) < 'r' (0x72).
STAGING_PREFIX = '0stg-'
RELEASE_PREFIX = 'rel-'
# release ids are the prefix + 14 digit build time + '-' + hash; fixed width
# keeps lexicographic ordering identical to chronological ordering
VERSION_TIME_FORMAT = '%Y%m%d%H%M%S'

# running-job policies
POLICY_WAIT = 'wait'          # wait for running jobs to finish before switching
POLICY_CONTINUE = 'continue'  # switch immediately; scrapyd lets old jobs finish
POLICY_ABORT = 'abort'        # refuse the deploy while jobs are running
DEFAULT_RUNNING_POLICY = POLICY_CONTINUE
RUNNING_JOB_WAIT_TIMEOUT = 300
RUNNING_JOB_POLL_INTERVAL = 5

# error patterns used to classify low-level failures into audit reason codes
_PORT_RE = re.compile(r'(address already in use|errno 98|eaddr|bind\(\)|port .* (is|already).*(use|occupied))', re.I)
_DEP_RE = re.compile(r'(modulenotfounderror|no module named|importerror|pkg_resources\.distributionnotfound|'
                     r'could not find a version|command not found)', re.I)

# file names that may carry credentials; their contents are never read into a
# manifest or a log, only their existence and a hash are recorded
SENSITIVE_FILE_RE = re.compile(r'(\.env$|credentials?|secret|passwd|password|\.pem$|\.key$)', re.I)

IGNORED_DIR_NAMES = {'.git', 'build', '__pycache__'}
IGNORED_DIR_SUFFIXES = ('.egg-info',)
IGNORED_FILE_SUFFIXES = ('.pyc', '.egg', '.pyo')
IGNORED_FILE_NAMES = {'.DS_Store'}


class DeploymentError(Exception):
    """base class for deployment failures; carries an audit reason code"""

    reason = 'unknown'

    def __init__(self, message, reason=None):
        super(DeploymentError, self).__init__(message)
        if reason:
            self.reason = reason


class BuildFailedError(DeploymentError):
    reason = 'build_failed'


class PortConflictError(DeploymentError):
    reason = 'port_conflict'


class DependencyMissingError(DeploymentError):
    reason = 'dependency_missing'


class HealthCheckFailedError(DeploymentError):
    reason = 'health_check_failed'


class ClientUnreachableError(DeploymentError):
    reason = 'client_unreachable'


class RunningTasksError(DeploymentError):
    reason = 'running_tasks'


class ConcurrentDeploymentError(DeploymentError):
    reason = 'concurrent_deployment'


class DataIncompatibleError(DeploymentError):
    reason = 'data_incompatible'


def classify_error(text):
    """map raw subprocess / exception text onto one of the audit reason codes"""
    if not text:
        return 'unknown'
    if _PORT_RE.search(text):
        return 'port_conflict'
    if _DEP_RE.search(text):
        return 'dependency_missing'
    return 'unknown'


def sanitize(text):
    """
    remove credentials / environment secrets from arbitrary text before it is
    written into a log file or an audit row. Handles both KEY=value pairs and
    JSON-ish "key": "value" content.
    """
    if text is None:
        return None
    if not isinstance(text, str):
        try:
            text = text.decode('utf-8', errors='replace')
        except Exception:
            text = str(text)

    def is_secret(key):
        lowered = key.lower()
        return any(pattern in lowered for pattern in SECRET_KEY_PATTERNS)

    # JSON style: "password": "value" / 'token': 'value'
    def json_repl(match):
        key, quote = match.group(1), match.group(2)
        return '%s%s%s: %s***%s' % (match.group(0)[0], key, quote, quote, quote) \
            if is_secret(key) else match.group(0)

    text = re.sub(r'([\"\'])([^\"\']+)\1\s*:\s*([\"\'])(.*?)\3',
                  lambda m: ('"%s": "***"' % m.group(2)) if is_secret(m.group(2)) else m.group(0), text)

    # env style: PASSWORD=value, export TOKEN=value (values stop at whitespace/quote)
    def env_repl(match):
        prefix, key = match.group(1), match.group(2)
        return '%s%s=***' % (prefix, key) if is_secret(key) else match.group(0)

    text = re.sub(r'(export\s+)?([A-Za-z_][A-Za-z0-9_]*)=([^\s"\'&;]+)', env_repl, text)

    # scrapyd basic-auth urls: http://user:pass@host
    text = re.sub(r'(https?://)[^:/@]+:[^@]+@', r'\1***:***@', text)
    return text


def truncate(text, limit=DEPLOY_LOG_MAX_BYTES):
    """bound the amount of text stored in a single log field / file"""
    if text is None:
        return None
    if len(text) <= limit:
        return text
    head = int(limit * 0.8)
    tail = int(limit * 0.15)
    return text[:head] + '\n... [truncated %d bytes] ...\n' % (len(text) - limit) + text[-tail:]


def checksum_file(path, chunk_size=65536):
    """sha256 of a file"""
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(chunk_size), b''):
            digest.update(chunk)
    return digest.hexdigest()


def checksum_bytes(data):
    return hashlib.sha256(data).hexdigest()


def project_versions_dir(project_name):
    return os.path.abspath(os.path.join(os.getcwd(), VERSIONS_FOLDER, project_name))


def version_dir(project_name, version):
    return os.path.join(project_versions_dir(project_name), version)


def egg_path_for(project_name, version):
    return os.path.join(version_dir(project_name, version), 'package.egg')


def manifest_path_for(project_name, version):
    return os.path.join(version_dir(project_name, version), 'manifest.json')


def make_version_id(checksum):
    """
    release version id: fixed-width timestamp prefix (chronological ordering on
    scrapyd) plus a monotonic counter and a uuid4 suffix so two builds in the
    same second never collide and always sort in creation order, and a content
    hash for human-readable verification. Fixed field widths keep lexicographic
    ordering identical to chronological ordering.
    """
    import uuid
    make_version_id._counter = (make_version_id._counter + 1) % 100000
    return '%s%s-%05d-%s-%s' % (
        RELEASE_PREFIX, time.strftime(VERSION_TIME_FORMAT, time.localtime()),
        make_version_id._counter, uuid.uuid4().hex[:8], checksum[:8])


make_version_id._counter = 0


def make_staging_id(version):
    """staging id that always sorts below any release id"""
    return '%s%s' % (STAGING_PREFIX, version)


def is_staging(version):
    return bool(version) and version.startswith(STAGING_PREFIX)


def timezone_now_iso():
    from django.utils import timezone
    return timezone.now().isoformat()


def build_manifest(project_path):
    """
    walk the working tree and produce a compact manifest of relative path ->
    sha256. Sensitive files keep only their hash (contents never leave the
    host), and VCS / build-artifact directories are skipped.
    """
    manifest = {}
    project_path = os.path.abspath(project_path)
    for root, dirs, files in os.walk(project_path):
        dirs[:] = [d for d in dirs
                   if d not in IGNORED_DIR_NAMES and not d.endswith(IGNORED_DIR_SUFFIXES)]
        for name in files:
            if name in IGNORED_FILE_NAMES or name.endswith(IGNORED_FILE_SUFFIXES):
                continue
            absolute = os.path.join(root, name)
            relative = os.path.relpath(absolute, project_path)
            try:
                entry = {'hash': checksum_file(absolute)}
                if SENSITIVE_FILE_RE.search(name):
                    entry['sensitive'] = True
                manifest[relative] = entry
            except (OSError, IOError):
                continue
    return manifest


def diff_manifests(old_manifest, new_manifest):
    """compare two manifests and return added / removed / changed relative paths"""
    old_manifest = old_manifest or {}
    new_manifest = new_manifest or {}
    old_paths, new_paths = set(old_manifest), set(new_manifest)
    added = sorted(new_paths - old_paths)
    removed = sorted(old_paths - new_paths)
    changed = sorted(path for path in old_paths & new_paths
                     if old_manifest[path].get('hash') != new_manifest[path].get('hash'))
    return {'added': added, 'removed': removed, 'changed': changed,
            'summary': {'added': len(added), 'removed': len(removed), 'changed': len(changed)}}


def check_data_compatibility(old_manifest, new_manifest):
    """
    rollback safety net: flag when a new version removes files that look like
    data/schema definitions, because rolling back afterwards could leave
    persisted data the old code cannot read. Only advisory metadata is
    compared -- user data itself is never touched.
    """
    data_file_re = re.compile(r'(migrations?/|schema|models?\.py$|items\.py$|pipelines?\.py$)', re.I)
    old_paths, new_paths = set(old_manifest or {}), set(new_manifest or {})
    risky_removed = sorted(p for p in (old_paths - new_paths) if data_file_re.search(p))
    changed = sorted(p for p in old_paths & new_paths
                     if old_manifest[p].get('hash') != new_manifest[p].get('hash')
                     and data_file_re.search(p))
    return {
        'check': 'data_compatibility',
        'passed': not risky_removed,
        'detail': 'removed data-definition files: %s' % risky_removed if risky_removed
        else 'no data-definition files removed; changed: %s' % changed,
        'removed_data_files': risky_removed,
        'changed_data_files': changed,
        'at': timezone_now_iso(),
    }


class FileLock(object):
    """
    inter-process lock backed by an flock(2) file. One lock per
    (client, project) so unrelated projects still deploy in parallel.
    """

    def __init__(self, name, timeout=DEPLOY_LOCK_TIMEOUT):
        locks_dir = os.path.join(os.getcwd(), DEPLOY_LOGS_FOLDER, 'locks')
        if not os.path.exists(locks_dir):
            os.makedirs(locks_dir)
        safe_name = re.sub(r'[^A-Za-z0-9_.-]', '_', str(name))
        self.path = os.path.join(locks_dir, safe_name + '.lock')
        self.timeout = timeout
        self._fh = None

    def acquire(self):
        import fcntl
        self._fh = open(self.path, 'a+')
        deadline = time.time() + self.timeout
        while True:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fh.seek(0)
                self._fh.truncate()
                self._fh.write('%d\n' % os.getpid())
                self._fh.flush()
                return self
            except (IOError, OSError) as e:
                if e.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.time() >= deadline:
                    self._fh.close()
                    self._fh = None
                    raise ConcurrentDeploymentError(
                        'another deployment is in progress for this client/project',
                        reason='concurrent_deployment')
                time.sleep(0.1)

    def release(self):
        if self._fh is None:
            return
        try:
            import fcntl
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


class DeployLogger(object):
    """
    collects sanitized, bounded output for one deployment attempt and mirrors
    it into a size-bounded, rotated log file.
    """

    def __init__(self, record_id):
        logs_dir = os.path.join(os.getcwd(), DEPLOY_LOGS_FOLDER)
        if not os.path.exists(logs_dir):
            os.makedirs(logs_dir)
        self.path = os.path.abspath(os.path.join(logs_dir, 'deploy-%s.log' % record_id))
        self._lines = []

    def write(self, level, message):
        line = '%s - %s - %s' % (time.strftime('%Y-%m-%d %H:%M:%S'), level, sanitize(str(message)))
        self._lines.append(line)
        logger.info('%s', line)
        # rotate when the file grows past the bound instead of appending forever
        try:
            if os.path.exists(self.path) and os.path.getsize(self.path) >= DEPLOY_LOG_MAX_BYTES:
                rotated = self.path + '.1'
                if os.path.exists(rotated):
                    os.remove(rotated)
                os.rename(self.path, rotated)
            with open(self.path, 'a', encoding='utf-8') as f:
                f.write(truncate(line) + '\n')
        except OSError:
            logger.exception('failed writing deployment log %s', self.path)

    def info(self, message):
        self.write('INFO', message)

    def error(self, message):
        self.write('ERROR', message)

    def content(self):
        return truncate('\n'.join(self._lines))


def rotate_deploy_logs(keep=DEPLOY_LOG_RETENTION_COUNT):
    """enforce the on-disk upper bound for deployment log files"""
    logs_dir = os.path.join(os.getcwd(), DEPLOY_LOGS_FOLDER)
    if not os.path.isdir(logs_dir):
        return
    entries = [os.path.join(logs_dir, name) for name in os.listdir(logs_dir)
               if name.startswith('deploy-') and name.endswith('.log')]
    entries.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    for stale in entries[keep:]:
        try:
            os.remove(stale)
            if os.path.exists(stale + '.1'):
                os.remove(stale + '.1')
        except OSError:
            pass


def prune_records(keep=DEPLOY_RECORD_RETENTION_COUNT):
    """bound the audit table; newest rows are retained"""
    from gerapy.server.core.models import DeploymentRecord
    ids = list(DeploymentRecord.objects.order_by('-id').values_list('id', flat=True)[keep:])
    if ids:
        DeploymentRecord.objects.filter(id__in=ids).delete()


def read_manifest(project_name, version):
    path = manifest_path_for(project_name, version)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (ValueError, OSError):
        return {}


class VersionStore(object):
    """
    immutable build artifacts on disk plus their ProjectVersion rows. Building
    happens in an isolated temp directory: the working project tree is only
    read, the egg is moved (never copied alongside the project) and a failed
    build leaves no partial version behind.
    """

    def __init__(self, projects_folder='projects'):
        self.projects_folder = projects_folder

    def project_path(self, project_name):
        return os.path.abspath(os.path.join(os.getcwd(), self.projects_folder, project_name))

    def list_versions(self, project_name):
        from gerapy.server.core.models import ProjectVersion
        return list(ProjectVersion.objects.filter(project=project_name).order_by('-created_at'))

    def get(self, project_name, version):
        from gerapy.server.core.models import ProjectVersion
        return ProjectVersion.objects.filter(project=project_name, version=version).first()

    def find_by_checksum(self, project_name, checksum):
        """content-address lookup: identical bytes reuse the same version"""
        from gerapy.server.core.models import ProjectVersion
        return ProjectVersion.objects.filter(
            project=project_name, checksum=checksum,
            build_status=ProjectVersion.READY).order_by('-created_at').first()

    def build(self, project_name, description=None, egg_bytes=None, build_logger=None):
        """
        create an immutable version. Either build an egg from the working tree
        or register an uploaded egg (egg_bytes). Content addressing means a
        repeat build of unchanged sources returns the existing version instead
        of creating a duplicate.
        """
        from gerapy.server.core.models import ProjectVersion
        from django.utils import timezone

        project_path = self.project_path(project_name)
        if not egg_bytes and not os.path.isdir(project_path):
            raise BuildFailedError('project folder %s does not exist' % project_name,
                                   reason='build_failed')

        if egg_bytes is None:
            egg_bytes, raw_output = self._build_egg_bytes(project_name, project_path)
        else:
            raw_output = 'egg uploaded directly'

        checksum = checksum_bytes(egg_bytes)
        manifest = build_manifest(project_path) if os.path.isdir(project_path) else {}

        # content addressing: a ready version with the same checksum (and an
        # intact artifact) is reused instead of creating a duplicate
        existing = self.find_by_checksum(project_name, checksum)
        if existing and os.path.exists(egg_path_for(project_name, existing.version)):
            if build_logger:
                build_logger.info('content unchanged; reusing immutable version %s' % existing.version)
            return existing, False

        version = make_version_id(checksum)
        target_dir = version_dir(project_name, version)
        tmp_dir = target_dir + '.tmp-%d' % os.getpid()
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)
        os.makedirs(tmp_dir)
        try:
            with open(os.path.join(tmp_dir, 'package.egg'), 'wb') as f:
                f.write(egg_bytes)
            with open(os.path.join(tmp_dir, 'manifest.json'), 'w', encoding='utf-8') as f:
                json.dump(manifest, f, ensure_ascii=False)
            # atomic publish of the version directory
            os.rename(tmp_dir, target_dir)
        finally:
            if os.path.exists(tmp_dir):
                shutil.rmtree(tmp_dir, ignore_errors=True)

        record, _ = ProjectVersion.objects.update_or_create(
            project=project_name, version=version,
            defaults={
                'checksum': checksum,
                'egg_path': egg_path_for(project_name, version),
                'size': len(egg_bytes),
                'manifest': json.dumps(manifest, ensure_ascii=False),
                'description': description,
                'build_status': ProjectVersion.READY,
                'build_log': truncate(sanitize(raw_output)),
                'built_at': timezone.now(),
            })
        self.prune(project_name)
        return record, True

    def mark_failed(self, project_name, checksum, output, description=None):
        """persist a failed build attempt so build failures are auditable"""
        from gerapy.server.core.models import ProjectVersion
        from django.utils import timezone
        version = make_version_id(checksum or 'failed')
        ProjectVersion.objects.update_or_create(
            project=project_name, version=version,
            defaults={'checksum': checksum or '', 'build_status': ProjectVersion.FAILED,
                      'build_log': truncate(sanitize(output)), 'description': description,
                      'built_at': timezone.now()})
        return version

    def _build_egg_bytes(self, project_name, project_path):
        """
        run the existing egg build in an isolated temp dir and return the egg
        contents. The working tree is not modified (no egg left next to the
        source) and build output is captured for failure classification.
        """
        from gerapy.server.core.build import create_default_setup_py
        from gerapy.server.core.config import config as read_scrapy_config

        work_path = os.getcwd()
        build_tmp = None
        try:
            settings = read_scrapy_config(project_path, 'settings', 'default')
            setup_file = os.path.join(project_path, 'setup.py')
            created_setup = not os.path.exists(setup_file)
            if created_setup:
                create_default_setup_py(setup_file, settings=settings, project=project_name)
            build_tmp = tempfile.mkdtemp(prefix='gerapy-build-')
            os.chdir(project_path)
            proc = subprocess.Popen(
                [sys.executable, 'setup.py', 'clean', '-a', 'bdist_uberegg', '-d', build_tmp],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=project_path)
            output_bytes, _ = proc.communicate()
            output = sanitize(output_bytes.decode('utf-8', errors='replace'))
            if proc.returncode != 0:
                reason = classify_error(output)
                if reason == 'port_conflict':
                    raise PortConflictError(output, reason=reason)
                if reason == 'dependency_missing':
                    raise DependencyMissingError(output, reason=reason)
                raise BuildFailedError(output or 'egg build failed (exit %s)' % proc.returncode,
                                       reason='build_failed')
            eggs = [name for name in os.listdir(build_tmp) if name.endswith('.egg')]
            if not eggs:
                raise BuildFailedError('build finished but no egg was produced', reason='build_failed')
            with open(os.path.join(build_tmp, eggs[0]), 'rb') as f:
                return f.read(), output
        except (BuildFailedError, PortConflictError, DependencyMissingError):
            raise
        except (OSError, IOError) as e:
            message = sanitize(str(e))
            reason = classify_error(message)
            if reason == 'dependency_missing':
                raise DependencyMissingError(message, reason=reason)
            raise BuildFailedError(message, reason=reason)
        except Exception as e:
            # configparser errors, setup.py syntax errors and any other build
            # time failure must be classified and audited instead of escaping
            message = sanitize(str(e))
            reason = classify_error(message)
            raise BuildFailedError(message, reason=reason if reason != 'unknown' else 'build_failed')
        finally:
            os.chdir(work_path)
            if build_tmp:
                shutil.rmtree(build_tmp, ignore_errors=True)

    def prune(self, project_name, keep=VERSION_RETENTION_COUNT, active_versions=None):
        """
        delete immutable build artifacts beyond the retention window; versions
        still active on a client are protected from removal.
        """
        active_versions = set(active_versions or [])
        base = project_versions_dir(project_name)
        if not os.path.isdir(base):
            return []
        names = sorted((name for name in os.listdir(base)
                        if os.path.isdir(os.path.join(base, name)) and name.startswith(RELEASE_PREFIX)),
                       reverse=True)
        from gerapy.server.core.models import ProjectVersion
        removed = []
        # the newest `keep` versions occupy the retention window; any version
        # still active on a client is protected on top of that, so rollback
        # targets never disappear while they are serving traffic
        retained = set(names[:keep]) | active_versions
        for name in names:
            if name in retained:
                continue
            shutil.rmtree(os.path.join(base, name), ignore_errors=True)
            ProjectVersion.objects.filter(project=project_name, version=name).delete()
            removed.append(name)
        return removed


def check_daemon_health(client):
    """
    pre-flight check: the scrapyd daemon must be reachable on the configured
    host/port. A connection failure most often means the daemon is down or the
    port is occupied by another process.
    """
    url = scrapyd_url(client.ip, client.port)
    try:
        response = requests.get(url, timeout=HEALTH_CHECK_TIMEOUT)
        ok = response.ok
        detail = 'daemon responded with HTTP %s' % response.status_code
    except requests.exceptions.RequestException as e:
        raise ClientUnreachableError('cannot reach scrapyd at %s: %s' % (url, sanitize(e)),
                                     reason='client_unreachable')
    return {'check': 'daemon', 'passed': bool(ok), 'detail': sanitize(detail), 'at': timezone_now_iso()}


def check_port_free(host, port):
    """
    best-effort local port check used when a build itself starts a service:
    binding the port proves nothing else occupies it.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1)
    try:
        sock.bind((host or '0.0.0.0', int(port)))
    except (OSError, socket.error) as e:
        raise PortConflictError('port %s is already in use: %s' % (port, sanitize(e)),
                                reason='port_conflict')
    finally:
        sock.close()
    return {'check': 'port', 'passed': True, 'detail': 'port %s free' % port,
            'at': timezone_now_iso()}


def get_running_jobs(scrapyd, project_name):
    """pending + running scrapyd jobs for a project; tolerates daemon quirks"""
    try:
        result = scrapyd.list_jobs(project_name) or {}
    except Exception:
        return []
    jobs = []
    for status in ('pending', 'running'):
        for job in result.get(status, []) or []:
            item = dict(job)
            item['status'] = status
            jobs.append(item)
    return jobs


def existing_versions(scrapyd, project_name):
    """scrapyd versions for the project, excluding staging leftovers"""
    try:
        versions = scrapyd.list_versions(project_name) or []
    except Exception:
        return []
    return [v for v in versions if not is_staging(v)]


def cleanup_staging(scrapyd, project_name, staging_version):
    """best-effort removal of a staging egg; never raises into the caller"""
    try:
        scrapyd.delete_version(project=project_name, version=staging_version)
    except Exception:
        logger.exception('failed removing staging version %s', staging_version)


class DeploymentEngine(object):
    """
    orchestrates one deploy: locking -> pre-flight -> staging upload -> health
    checks -> running-job policy -> atomic release -> retention pruning, with
    automatic rollback (staging removal) on every failure path.
    """

    def __init__(self, store=None):
        self.store = store or VersionStore()

    def deploy(self, client, project_name, version, *, operator=None,
               running_policy=DEFAULT_RUNNING_POLICY, keep_versions=1,
               expected_spiders=None, enforce_data_compatibility=True, record=None):
        """
        :param record: an open DeploymentRecord row used as the audit trail
        :return: the updated DeploymentRecord
        """
        from gerapy.server.core.models import DeploymentRecord, ProjectVersion
        from django.utils import timezone

        pv = self.store.get(project_name, version)
        if not pv or pv.build_status != ProjectVersion.READY:
            raise BuildFailedError('version %s is not built or not ready' % version, reason='build_failed')
        egg_file = egg_path_for(project_name, version)
        if not os.path.exists(egg_file):
            raise BuildFailedError('artifact for version %s is missing on disk' % version, reason='build_failed')

        log = DeployLogger(record.id)
        record.log_path = log.path
        if operator is not None:
            record.operator = operator
        record.running_task_policy = running_policy
        record.version = version
        record.status = DeploymentRecord.DEPLOYING
        record.started_at = timezone.now()
        record.save()
        checks = []

        try:
            with FileLock('client-%s-project-%s' % (client.id, project_name)):
                return self._run_locked(client, project_name, pv, egg_file, version,
                                        running_policy, keep_versions, expected_spiders,
                                        enforce_data_compatibility, record, log, checks)
        except ConcurrentDeploymentError:
            # a competing deployment owns the lock; the view maps this to 409.
            # Record the rejection so the audit trail still explains it.
            record.status = DeploymentRecord.REJECTED
            record.reason = 'concurrent_deployment'
            record.rollback_reason = truncate(sanitize('deployment rejected: another deployment is in progress'))
            record.log = log.content()
            record.finished_at = timezone.now()
            record.save()
            rotate_deploy_logs()
            prune_records()
            raise
        except DeploymentError as e:
            return self._fail(record, log, e, e.reason, checks)
        except Exception as e:  # pragma: no cover - defensive catch-all
            return self._fail(record, log, e, classify_error(str(e)) or 'unknown', checks)

    def _run_locked(self, client, project_name, pv, egg_file, version, running_policy,
                    keep_versions, expected_spiders, enforce_data_compatibility, record, log, checks):
        from gerapy.server.core.models import DeploymentRecord, Deploy
        from django.utils import timezone
        from django.db import transaction

        scrapyd = get_scrapyd(client)
        with open(egg_file, 'rb') as f:
            egg_bytes = f.read()
        staging_version = make_staging_id(version)

        # 1. pre-flight: daemon reachable
        log.info('pre-flight: checking scrapyd daemon on %s:%s' % (client.ip, client.port))
        checks.append(check_daemon_health(client))

        # 2. remember the currently active (highest) version for diff/rollback
        remote_versions = existing_versions(scrapyd, project_name)
        previous_version = remote_versions[-1] if remote_versions else None
        record.previous_version = previous_version
        log.info('previous active version: %s' % previous_version)

        # 3. version diff + data-compatibility check against the previous release
        new_manifest = read_manifest(project_name, version)
        old_manifest = read_manifest(project_name, previous_version) if previous_version else {}
        record.diff = json.dumps(diff_manifests(old_manifest, new_manifest), ensure_ascii=False)
        compat = check_data_compatibility(old_manifest, new_manifest)
        checks.append(compat)
        if enforce_data_compatibility and not compat['passed']:
            raise DataIncompatibleError(compat['detail'], reason='data_incompatible')

        # 4. running-job policy (scrapyd keeps old jobs running regardless)
        jobs = get_running_jobs(scrapyd, project_name)
        record.running_jobs = len(jobs)
        if jobs:
            log.info('found %d in-flight job(s), policy=%s' % (len(jobs), running_policy))
            if running_policy == POLICY_ABORT:
                raise RunningTasksError(
                    'deployment aborted: %d job(s) still running' % len(jobs), reason='running_tasks')
            if running_policy == POLICY_WAIT:
                self._wait_for_jobs(scrapyd, project_name, jobs, log)

        # 5. staging upload + egg health checks (import + spider enumeration)
        log.info('uploading staging egg %s' % staging_version)
        try:
            spiders, egg_checks = self._staged_health_checks(
                scrapyd, project_name, staging_version, egg_bytes, expected_spiders, log)
            checks.extend(egg_checks)

            # 6. atomic switch: release id sorts above every previous release,
            #    so this single call is the moment traffic moves to the new code
            log.info('health checks passed, activating release %s' % version)
            scrapyd.add_version(project_name, version, egg_bytes)
        finally:
            # staging must never linger; it is irrelevant to the active version
            cleanup_staging(scrapyd, project_name, staging_version)

        # 7. post-switch verification: the daemon must still list the project
        post_check = self._post_switch_check(scrapyd, project_name, version, spiders)
        checks.append(post_check)
        if not post_check['passed']:
            # automatic rollback: remove the freshly added release so the
            # previous version becomes the highest (active) one again
            log.error('post-switch check failed, rolling back release %s' % version)
            try:
                scrapyd.delete_version(project=project_name, version=version)
            except Exception as rollback_err:
                log.error('rollback failed: %s' % rollback_err)
                raise HealthCheckFailedError(
                    'post-switch failure and rollback failed: %s' % post_check['detail'],
                    reason='health_check_failed')
            record.rollback_reason = truncate(sanitize(post_check['detail']))
            raise HealthCheckFailedError(
                'post-switch health check failed, rolled back to %s' % previous_version,
                reason='health_check_failed')

        # 8. prune remote versions beyond the retention window (default 1 keeps
        #    the historical single-version behaviour; running jobs keep using
        #    their loaded egg even after the remote version is deleted)
        self._prune_remote(scrapyd, project_name, version, keep_versions, log)

        # 9. local artifact pruning, protecting versions still deployed
        active = set(existing_versions(scrapyd, project_name))
        self.store.prune(project_name, keep=max(keep_versions, 1), active_versions=active)

        with transaction.atomic():
            Deploy.objects.update_or_create(
                client=client, project=pv and _project_row(project_name),
                defaults={'deployed_at': timezone.now(),
                          'description': _project_description(project_name)})
            record.status = DeploymentRecord.SUCCESS
            record.reason = None
            record.health_checks = json.dumps(checks, ensure_ascii=False)
            record.log = log.content()
            record.finished_at = timezone.now()
            record.save()
        rotate_deploy_logs()
        prune_records()
        return record

    def _staged_health_checks(self, scrapyd, project_name, staging_version, egg_bytes,
                              expected_spiders, log):
        checks = []
        try:
            spiders = scrapyd.add_version(project_name, staging_version, egg_bytes)
        except Exception as e:
            message = sanitize(str(e))
            reason = classify_error(message)
            if reason == 'port_conflict':
                raise PortConflictError(message, reason=reason)
            if reason == 'dependency_missing':
                raise DependencyMissingError(message, reason=reason)
            raise HealthCheckFailedError('scrapyd rejected staging egg: %s' % message,
                                         reason='health_check_failed')
        spiders = list(spiders or [])
        checks.append({'check': 'egg_import', 'passed': True,
                       'detail': 'scrapyd imported egg and found %d spider(s)' % len(spiders),
                       'spiders': spiders, 'at': timezone_now_iso()})
        if expected_spiders is not None:
            missing = sorted(set(expected_spiders) - set(spiders))
            checks.append({'check': 'spiders_present', 'passed': not missing,
                           'detail': 'missing spiders: %s' % missing if missing
                           else 'all expected spiders present', 'at': timezone_now_iso()})
            if missing:
                raise HealthCheckFailedError('egg is missing expected spiders %s' % missing,
                                             reason='health_check_failed')
        # retry loop on the read-side check to absorb short daemon hiccups
        last_error = None
        for attempt in range(HEALTH_CHECK_RETRIES + 1):
            try:
                loaded = scrapyd.list_spiders(project_name)
                checks.append({'check': 'daemon_list_spiders', 'passed': True,
                               'detail': '%d spider(s) listable' % len(loaded or []),
                               'attempt': attempt + 1, 'at': timezone_now_iso()})
                return spiders, checks
            except Exception as e:
                last_error = sanitize(str(e))
                checks.append({'check': 'daemon_list_spiders', 'passed': False,
                               'detail': last_error, 'attempt': attempt + 1,
                               'at': timezone_now_iso()})
                log.error('list_spiders attempt %d failed: %s' % (attempt + 1, last_error))
                time.sleep(min(1 * (attempt + 1), 3))
        raise HealthCheckFailedError('daemon could not list spiders after upload: %s' % last_error,
                                     reason='health_check_failed')

    def _post_switch_check(self, scrapyd, project_name, version, spiders):
        try:
            remote = existing_versions(scrapyd, project_name)
            active_ok = bool(remote) and remote[-1] == version
            loaded = scrapyd.list_spiders(project_name)
            passed = active_ok and set(spiders or []).issubset(set(loaded or []))
            return {'check': 'post_switch', 'passed': passed,
                    'detail': 'active=%s, spiders=%s' % (remote[-1] if remote else None, loaded),
                    'at': timezone_now_iso()}
        except Exception as e:
            return {'check': 'post_switch', 'passed': False,
                    'detail': sanitize(str(e)), 'at': timezone_now_iso()}

    def _prune_remote(self, scrapyd, project_name, active_version, keep_versions, log):
        remote = existing_versions(scrapyd, project_name)
        # keep the newest keep_versions releases (the active one is always the
        # newest at this point); scrapyd itself finishes already running jobs
        # against the egg they started with, so deleting an old version never
        # interrupts them
        ordered = sorted(remote, reverse=True)
        retained = set(ordered[:max(keep_versions, 1)])
        for version_id in ordered:
            if version_id in retained:
                continue
            try:
                scrapyd.delete_version(project=project_name, version=version_id)
                log.info('pruned remote version %s' % version_id)
            except Exception as e:
                log.error('failed pruning remote version %s: %s' % (version_id, e))

    def _wait_for_jobs(self, scrapyd, project_name, initial_jobs, log):
        deadline = time.time() + RUNNING_JOB_WAIT_TIMEOUT
        known = {job.get('id') for job in initial_jobs}
        while time.time() < deadline:
            current = get_running_jobs(scrapyd, project_name)
            known = {job.get('id') for job in current}
            if not known:
                return
            log.info('waiting for %d in-flight job(s)' % len(known))
            time.sleep(RUNNING_JOB_POLL_INTERVAL)
        raise RunningTasksError('timed out waiting for running jobs: %s' % sorted(known),
                                reason='running_tasks')

    def rollback(self, client, project_name, target_version, *, operator=None, record=None):
        """
        manual rollback: re-activate a previously built, still-retained version
        by re-uploading its immutable egg with its original version id.
        """
        from gerapy.server.core.models import DeploymentRecord, ProjectVersion
        from django.utils import timezone

        pv = self.store.get(project_name, target_version)
        if not pv or pv.build_status != ProjectVersion.READY:
            raise BuildFailedError('rollback target %s is not available' % target_version,
                                   reason='build_failed')
        egg_file = egg_path_for(project_name, target_version)
        if not os.path.exists(egg_file):
            raise BuildFailedError('artifact for %s is missing' % target_version, reason='build_failed')

        log = DeployLogger(record.id)
        record.log_path = log.path
        if operator is not None:
            record.operator = operator
        record.version = target_version
        record.status = DeploymentRecord.DEPLOYING
        record.started_at = timezone.now()
        checks = []
        try:
            with FileLock('client-%s-project-%s' % (client.id, project_name)):
                scrapyd = get_scrapyd(client)
                checks.append(check_daemon_health(client))
                remote = existing_versions(scrapyd, project_name)
                record.previous_version = remote[-1] if remote else None
                with open(egg_file, 'rb') as f:
                    egg_bytes = f.read()
                staging = make_staging_id(target_version)
                try:
                    spiders, egg_checks = self._staged_health_checks(
                        scrapyd, project_name, staging, egg_bytes, None, log)
                    checks.extend(egg_checks)
                    # a rollback target is usually older than the currently
                    # active version, so re-uploading it would not make it the
                    # highest id. Remove every release newer than the target
                    # first (their eggs may stay retained locally for re-deploy);
                    # already running jobs keep executing their loaded egg.
                    newer_than_target = [v for v in remote
                                         if not is_staging(v) and v > target_version]
                    for newer in newer_than_target:
                        try:
                            scrapyd.delete_version(project=project_name, version=newer)
                            log.info('rollback: removed newer remote version %s' % newer)
                        except Exception as e:
                            raise HealthCheckFailedError(
                                'could not remove newer version %s before rollback: %s'
                                % (newer, sanitize(e)), reason='health_check_failed')
                    # now the target egg becomes the highest (active) version
                    scrapyd.add_version(project_name, target_version, egg_bytes)
                finally:
                    cleanup_staging(scrapyd, project_name, staging)
                post = self._post_switch_check(scrapyd, project_name, target_version, spiders)
                checks.append(post)
                if not post['passed']:
                    raise HealthCheckFailedError('rollback health check failed: %s' % post['detail'],
                                                 reason='health_check_failed')
                record.status = DeploymentRecord.ROLLBACK
                record.rollback_reason = truncate(sanitize('manual rollback to %s' % target_version))
                record.health_checks = json.dumps(checks, ensure_ascii=False)
                record.log = log.content()
                record.finished_at = timezone.now()
                record.save()
                rotate_deploy_logs()
                prune_records()
                return record
        except ConcurrentDeploymentError:
            record.status = DeploymentRecord.REJECTED
            record.reason = 'concurrent_deployment'
            record.rollback_reason = truncate(sanitize('rollback rejected: another deployment is in progress'))
            record.log = log.content()
            record.finished_at = timezone.now()
            record.save()
            rotate_deploy_logs()
            prune_records()
            raise
        except DeploymentError as e:
            # a failed rollback must leave the previously active version in
            # place; the attempt itself is recorded as 'failed' (ROLLED_BACK is
            # reserved for forward deploys that were automatically rolled back)
            return self._fail(record, log, e, e.reason, checks)
        except Exception as e:  # pragma: no cover - defensive
            return self._fail(record, log, e, classify_error(str(e)) or 'unknown', checks)

    def _fail(self, record, log, error, reason, checks, status=None):
        from gerapy.server.core.models import DeploymentRecord
        from django.utils import timezone
        status = status or DeploymentRecord.FAILED
        log.error('%s: %s' % (reason, error))
        record.status = status
        record.reason = reason
        record.health_checks = json.dumps(checks, ensure_ascii=False)
        record.rollback_reason = truncate(sanitize(str(error)))
        record.log = log.content()
        record.finished_at = timezone.now()
        record.save()
        rotate_deploy_logs()
        prune_records()
        return record


def _project_row(project_name):
    from gerapy.server.core.models import Project
    return Project.objects.filter(name=project_name).first()


def _project_description(project_name):
    project = _project_row(project_name)
    return project.description if project else None
