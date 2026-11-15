"""
Tests for versioned deployment: immutable builds, health-checked atomic
switch, automatic rollback, concurrency locking, running-job policies,
retention bounds, secret redaction and the audit trail.

Scrapyd is replaced by an in-memory fake that implements the small subset of
the scrapyd API the engine uses; its "active version" rule (highest
lexicographic version id) matches real scrapyd.
"""
import json
import os
import shutil
import tempfile
from unittest import mock

from django.test import TestCase, Client as HttpClient
from django.contrib.auth.models import User

from gerapy.server.core.models import Client, Project, Deploy, ProjectVersion, DeploymentRecord
from gerapy.server.core import deployment, views
from gerapy.server.core.deployment import (
    VersionStore, DeploymentEngine, DeployLogger, FileLock,
    ConcurrentDeploymentError, BuildFailedError,
    sanitize, truncate, build_manifest, diff_manifests, check_data_compatibility,
    make_version_id, make_staging_id, is_staging, STAGING_PREFIX, RELEASE_PREFIX,
    POLICY_ABORT, POLICY_CONTINUE, POLICY_WAIT,
    prune_records, rotate_deploy_logs,
)


class FakeScrapyd(object):
    """in-memory scrapyd: versions map to egg bytes; highest id is active"""

    def __init__(self, spiders=None, fail_add_staging=False, fail_list=False,
                 fail_delete=False, jobs=None):
        self.versions = {}
        self.calls = []
        self._spiders = spiders or ['sample']
        self.fail_add_staging = fail_add_staging
        self.fail_list = fail_list
        self.fail_delete = fail_delete
        self._jobs = jobs or {}

    def add_version(self, project, version, egg):
        self.calls.append(('add_version', project, version))
        if self.fail_add_staging and is_staging(version):
            raise RuntimeError('ModuleNotFoundError: No module named named \'missing_dep\'')
        self.versions[version] = egg
        return list(self._spiders)

    def list_versions(self, project):
        self.calls.append(('list_versions', project))
        return sorted(self.versions.keys())

    def delete_version(self, project, version):
        self.calls.append(('delete_version', project, version))
        if self.fail_delete:
            raise RuntimeError('delete failed')
        self.versions.pop(version, None)
        return True

    def list_spiders(self, project):
        self.calls.append(('list_spiders', project))
        if self.fail_list:
            raise RuntimeError('daemon list failed')
        return list(self._spiders)

    def list_jobs(self, project):
        self.calls.append(('list_jobs', project))
        return self._jobs

    def active_version(self):
        ids = [v for v in self.versions if not is_staging(v)]
        return sorted(ids)[-1] if ids else None

    def remote_release_ids(self):
        return sorted(v for v in self.versions if not is_staging(v))


def make_project_tree(root, project_name, files=None):
    """create a minimal on-disk project with the given relative files"""
    base = os.path.join(root, 'projects', project_name)
    os.makedirs(os.path.join(base, project_name, 'spiders'), exist_ok=True)
    files = dict(files or {})
    files.setdefault('scrapy.cfg', '[settings]\ndefault = %s.settings\n' % project_name)
    files.setdefault('setup.py',
                     "from setuptools import setup, find_packages\n"
                     "setup(name='%s', version='1.0', packages=find_packages(),\n"
                     "      entry_points={'scrapy': ['settings=%s.settings']})\n"
                     % (project_name, project_name))
    open(os.path.join(base, project_name, '__init__.py'), 'w').close()
    open(os.path.join(base, project_name, 'spiders', '__init__.py'), 'w').close()
    open(os.path.join(base, project_name, 'settings.py'), 'w').write('BOT_NAME = %r\n' % project_name)
    for relative, content in files.items():
        path = os.path.join(base, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, 'w').write(content)
    return base


class DeploymentTempDirTestCase(TestCase):
    """each test runs inside its own throwaway gerapy workspace"""

    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix='gerapy-deploy-test-')
        self._old_cwd = os.getcwd()
        os.chdir(self.workdir)
        os.makedirs(os.path.join(self.workdir, 'projects'))
        self.client_model = Client.objects.create(
            name='local', ip='127.0.0.1', port=6800)
        self.project = Project.objects.create(name='demo')

    def tearDown(self):
        os.chdir(self._old_cwd)
        shutil.rmtree(self.workdir, ignore_errors=True)

    def upload_version(self, project_name='demo', egg_bytes=b'EGG-CONTENT',
                       manifest_files=None, description='test build'):
        """bypass the subprocess egg build by registering uploaded bytes"""
        store = VersionStore()
        # ensure a project tree exists so a manifest can be built
        make_project_tree(self.workdir, project_name, manifest_files)
        pv, _ = store.build(project_name, description=description, egg_bytes=egg_bytes)
        return pv

    def make_record(self, **kwargs):
        defaults = dict(client=self.client_model, client_name='local',
                        project='demo', status=DeploymentRecord.DEPLOYING,
                        operator='tester')
        defaults.update(kwargs)
        return DeploymentRecord.objects.create(**defaults)


class SanitizeAndBoundsTest(TestCase):

    def test_secret_redaction_env_and_json_and_url(self):
        raw = 'export PASSWORD=hunter2 TOKEN=abc123 normal=ok\n' \
              '{"username": "alice", "api_key": "secret-key"}\n' \
              'connecting to http://scrapy:pw@127.0.0.1:6800'
        clean = sanitize(raw)
        self.assertNotIn('hunter2', clean)
        self.assertNotIn('abc123', clean)
        self.assertNotIn('secret-key', clean)
        self.assertNotIn('pw@', clean)
        self.assertIn('normal=ok', clean)
        self.assertIn('alice', clean)

    def test_truncate_keeps_upper_bound(self):
        text = 'x' * 10000
        out = truncate(text, limit=1000)
        self.assertLessEqual(len(out), 1000)
        self.assertIn('truncated', out)

    def test_deploy_logger_rotates_and_never_stores_secrets(self):
        record = self.make_record_unbound()
        log = DeployLogger(record.id)
        log.info('DB_PASSWORD=supersecret started')
        self.assertTrue(os.path.exists(log.path))
        with open(log.path, encoding='utf-8') as f:
            on_disk = f.read()
        self.assertNotIn('supersecret', on_disk)
        self.assertIn('***', on_disk)

    def make_record_unbound(self):
        old_cwd = os.getcwd()
        tmp = tempfile.mkdtemp(prefix='gerapy-log-test-')
        os.chdir(tmp)
        try:
            client = Client.objects.create(name='c', ip='1.1.1.1', port=1)
            return DeploymentRecord.objects.create(client=client, project='p')
        finally:
            os.chdir(old_cwd)
            self.addCleanup(shutil.rmtree, tmp, True)


class ManifestAndDiffTest(DeploymentTempDirTestCase):

    def test_manifest_diff_and_sensitive_files(self):
        base = make_project_tree(self.workdir, 'demo', {
            'demo/spiders/a.py': 'print(1)',
            '.env': 'PASSWORD=should-not-be-read',
        })
        manifest = build_manifest(base)
        self.assertIn('demo/spiders/a.py', manifest)
        self.assertIn('.env', manifest)
        self.assertTrue(manifest['.env'].get('sensitive'))
        # hash only -- the secret content itself must not appear in the manifest
        self.assertEqual(set(manifest['.env'].keys()), {'hash', 'sensitive'})

        base2 = make_project_tree(self.workdir, 'demo2', {
            'demo/spiders/a.py': 'print(2)',
            'demo/spiders/b.py': 'print(3)',
        })
        manifest2 = build_manifest(base2)
        diff = diff_manifests(manifest, manifest2)
        self.assertIn('demo/spiders/b.py', diff['added'])
        self.assertIn('.env', diff['removed'])
        self.assertIn('demo/spiders/a.py', diff['changed'])

    def test_data_compatibility_flags_removed_schema_files(self):
        old = {'demo/models.py': {'hash': 'a'}, 'demo/spiders/x.py': {'hash': 'b'}}
        new = {'demo/spiders/x.py': {'hash': 'b'}}
        result = check_data_compatibility(old, new)
        self.assertFalse(result['passed'])
        self.assertIn('demo/models.py', result['removed_data_files'])
        ok = check_data_compatibility(old, {'demo/models.py': {'hash': 'a2'},
                                            'demo/spiders/x.py': {'hash': 'b'}})
        self.assertTrue(ok['passed'])


class VersionStoreTest(DeploymentTempDirTestCase):

    def test_upload_is_content_addressed_and_immutable(self):
        pv = self.upload_version(egg_bytes=b'hello-egg')
        self.assertTrue(pv.version.startswith(RELEASE_PREFIX))
        self.assertEqual(pv.build_status, ProjectVersion.READY)
        self.assertTrue(os.path.exists(pv.egg_path))
        # same bytes -> same version id, no duplicate row or directory
        again, created = VersionStore().build('demo', egg_bytes=b'hello-egg')
        self.assertFalse(created)
        self.assertEqual(again.version, pv.version)
        # different bytes -> new version
        other, created = VersionStore().build('demo', egg_bytes=b'different-egg')
        self.assertTrue(created)
        self.assertNotEqual(other.version, pv.version)

    def test_prune_respects_retention_and_protects_active(self):
        versions = []
        for i in range(4):
            versions.append(self.upload_version(egg_bytes=b'egg-%d' % i).version)
        # keep newest two plus protect the oldest (still active on a client)
        removed = VersionStore().prune('demo', keep=2, active_versions={versions[0]})
        remaining = os.listdir(os.path.join(self.workdir, 'versions', 'demo'))
        # newest two and the protected oldest survive; only the middle one goes
        self.assertEqual(sorted(remaining), sorted([versions[0], versions[-1], versions[-2]]))
        self.assertEqual(removed, [versions[1]])
        self.assertEqual(ProjectVersion.objects.filter(project='demo').count(), 3)

    def test_prune_without_protection_keeps_exactly_n(self):
        versions = []
        for i in range(4):
            versions.append(self.upload_version(egg_bytes=b'plain-%d' % i).version)
        VersionStore().prune('demo', keep=2)
        remaining = os.listdir(os.path.join(self.workdir, 'versions', 'demo'))
        self.assertEqual(sorted(remaining), sorted(versions[-2:]))

    def test_missing_project_tree_is_a_build_failure(self):
        with self.assertRaises(BuildFailedError) as ctx:
            VersionStore().build('does-not-exist')
        self.assertEqual(ctx.exception.reason, 'build_failed')

    def test_real_subprocess_build_failure_is_captured_and_classified(self):
        # a broken setup.py makes the egg build exit non-zero; the failure must
        # be raised as a typed error instead of escaping as a raw OSError
        make_project_tree(self.workdir, 'broken', {
            'setup.py': 'raise RuntimeError("ModuleNotFoundError: No module named \'ghost_lib\'")',
        })
        from gerapy.server.core.deployment import DeploymentError, DependencyMissingError
        with self.assertRaises(DeploymentError) as ctx:
            VersionStore().build('broken')
        self.assertIsInstance(ctx.exception, DependencyMissingError)
        self.assertEqual(ctx.exception.reason, 'dependency_missing')
        self.assertIn('ghost_lib', str(ctx.exception))
        # no partial version directory is left behind
        self.assertFalse(os.path.exists(os.path.join(self.workdir, 'versions', 'broken')))

    def test_successful_real_build_is_immutable_and_tree_clean(self):
        # full path through the subprocess builder (no pre-supplied egg bytes)
        make_project_tree(self.workdir, 'realbuild', {
            'realbuild/spiders/sample.py': 'class SampleSpider:\n    name = "sample"\n',
        })
        pv, created = VersionStore().build('realbuild')
        self.assertTrue(created)
        self.assertEqual(pv.build_status, ProjectVersion.READY)
        self.assertTrue(os.path.exists(pv.egg_path))
        # the working tree must not accumulate eggs or build temp dirs
        tree = os.listdir(os.path.join(self.workdir, 'projects', 'realbuild'))
        self.assertFalse(any(name.endswith('.egg') for name in tree))
        # rebuilding unchanged sources reuses the version (content addressing)
        again, created_again = VersionStore().build('realbuild')
        self.assertFalse(created_again)
        self.assertEqual(again.version, pv.version)

    def test_staging_id_always_sorts_below_release(self):
        release = make_version_id('a' * 12)
        staging = make_staging_id(release)
        self.assertTrue(staging.startswith(STAGING_PREFIX))
        self.assertLess(staging, release)
        self.assertTrue(is_staging(staging))
        self.assertFalse(is_staging(release))


class DeploymentEngineTest(DeploymentTempDirTestCase):

    def deploy(self, fake, version, **kwargs):
        record = self.make_record(version=version)
        with mock.patch('gerapy.server.core.deployment.get_scrapyd', return_value=fake), \
                mock.patch('gerapy.server.core.deployment.requests') as http:
            http.get.return_value = mock.Mock(ok=True, status_code=200)
            http.exceptions.RequestException = Exception
            result = DeploymentEngine().deploy(
                self.client_model, 'demo', version, record=record, **kwargs)
        return result, record

    def test_successful_deploy_switches_atomically_and_records_audit(self):
        pv = self.upload_version()
        fake = FakeScrapyd()
        result, record = self.deploy(fake, pv.version)
        self.assertEqual(result.status, DeploymentRecord.SUCCESS)
        # staging was used for health checks and removed afterwards
        self.assertTrue(all(not is_staging(v) for v in fake.versions))
        # the release egg is the active (highest) version
        self.assertEqual(fake.active_version(), pv.version)
        # Deploy row maintained for backward compatibility
        self.assertTrue(Deploy.objects.filter(client=self.client_model, project=self.project).exists())
        # audit trail content
        record.refresh_from_db()
        self.assertEqual(record.operator, 'tester')
        self.assertIsNotNone(record.health_checks)
        checks = json.loads(record.health_checks)
        check_names = [c['check'] for c in checks]
        self.assertIn('daemon', check_names)
        self.assertIn('egg_import', check_names)
        self.assertIn('post_switch', check_names)
        self.assertTrue(all(c['passed'] for c in checks))
        self.assertTrue(record.log and os.path.exists(record.log_path))
        # diff recorded
        self.assertIsNotNone(json.loads(record.diff))

    def test_health_check_failure_keeps_old_version_active(self):
        old = self.upload_version(egg_bytes=b'old-egg')
        fake = FakeScrapyd()
        # establish a working previous release
        ok, _ = self.deploy(fake, old.version)
        self.assertEqual(ok.status, DeploymentRecord.SUCCESS)
        active_before = fake.active_version()

        new = self.upload_version(egg_bytes=b'new-egg')
        failing = FakeScrapyd(fail_add_staging=True)
        # copy existing remote state into the failing daemon
        failing.versions = dict(fake.versions)
        result, record = self.deploy(failing, new.version)
        self.assertEqual(result.status, DeploymentRecord.FAILED)
        self.assertEqual(record.reason, 'dependency_missing')
        # the old release is still the active version -- no half-switch
        self.assertEqual(failing.active_version(), active_before)
        # nothing staging left behind
        self.assertTrue(all(not is_staging(v) for v in failing.versions))
        # rollback reason audited
        self.assertIn('missing_dep', record.rollback_reason)

    def test_post_switch_failure_triggers_automatic_rollback(self):
        old = self.upload_version(egg_bytes=b'old-egg-2')
        fake = FakeScrapyd()
        self.deploy(fake, old.version)
        new = self.upload_version(egg_bytes=b'new-egg-2')
        # daemon accepts the egg but cannot list spiders afterwards
        flaky = FakeScrapyd(fail_list=True)
        flaky.versions = dict(fake.versions)
        result, record = self.deploy(flaky, new.version)
        self.assertEqual(result.status, DeploymentRecord.FAILED)
        self.assertEqual(record.reason, 'health_check_failed')
        # rolled back: the bad release was deleted and the old one is active
        self.assertNotIn(new.version, flaky.versions)
        self.assertEqual(flaky.active_version(), old.version)
        self.assertTrue(record.rollback_reason)

    def test_default_single_version_retention_matches_legacy_behaviour(self):
        first = self.upload_version(egg_bytes=b'v1')
        fake = FakeScrapyd()
        self.deploy(fake, first.version, keep_versions=1)
        second = self.upload_version(egg_bytes=b'v2')
        result, _ = self.deploy(fake, second.version, keep_versions=1)
        self.assertEqual(result.status, DeploymentRecord.SUCCESS)
        # exactly one remote version remains -- the historical default
        self.assertEqual(fake.remote_release_ids(), [second.version])

    def test_multi_version_retention_keeps_n_releases(self):
        fake = FakeScrapyd()
        versions = [self.upload_version(egg_bytes=b'egg-%d' % i).version for i in range(3)]
        # local artifact pruning is exercised separately; here the disk store is
        # kept intact so every built version remains deployable
        with mock.patch.object(VersionStore, 'prune', return_value=[]):
            for version in versions:
                result, _ = self.deploy(fake, version, keep_versions=2)
                self.assertEqual(result.status, DeploymentRecord.SUCCESS)
        self.assertEqual(len(fake.remote_release_ids()), 2)
        self.assertEqual(fake.active_version(), versions[-1])

    def test_running_jobs_abort_policy_blocks_switch(self):
        pv = self.upload_version()
        jobs = {'running': [{'id': 'job-1', 'spider': 'sample'}], 'pending': [], 'finished': []}
        fake = FakeScrapyd(jobs=jobs)
        result, record = self.deploy(fake, pv.version, running_policy=POLICY_ABORT)
        self.assertEqual(result.status, DeploymentRecord.FAILED)
        self.assertEqual(record.reason, 'running_tasks')
        self.assertEqual(record.running_jobs, 1)
        # nothing was activated
        self.assertIsNone(fake.active_version())

    def test_running_jobs_continue_policy_deploys_without_interruption(self):
        pv = self.upload_version()
        jobs = {'running': [{'id': 'job-1'}], 'pending': [], 'finished': []}
        fake = FakeScrapyd(jobs=jobs)
        result, _ = self.deploy(fake, pv.version, running_policy=POLICY_CONTINUE)
        self.assertEqual(result.status, DeploymentRecord.SUCCESS)
        self.assertEqual(fake.active_version(), pv.version)
        # the job was never cancelled by the engine
        self.assertNotIn(('cancel',), [c[:1] for c in fake.calls])

    def test_wait_policy_waits_then_deploys(self):
        pv = self.upload_version()
        jobs = {'running': [{'id': 'job-1'}], 'pending': [], 'finished': []}
        fake = FakeScrapyd(jobs=jobs)
        # simulate the job finishing after the first poll
        original_list_jobs = fake.list_jobs

        def list_jobs(project):
            fake._jobs = {'running': [], 'pending': [], 'finished': []}
            return original_list_jobs(project)

        fake.list_jobs = list_jobs
        with mock.patch('gerapy.server.core.deployment.RUNNING_JOB_POLL_INTERVAL', 0), \
                mock.patch('gerapy.server.core.deployment.RUNNING_JOB_WAIT_TIMEOUT', 5):
            result, _ = self.deploy(fake, pv.version, running_policy=POLICY_WAIT)
        self.assertEqual(result.status, DeploymentRecord.SUCCESS)

    def test_wait_policy_times_out_and_fails_safe(self):
        pv = self.upload_version()
        jobs = {'running': [{'id': 'job-1'}], 'pending': [], 'finished': []}
        fake = FakeScrapyd(jobs=jobs)
        with mock.patch('gerapy.server.core.deployment.RUNNING_JOB_POLL_INTERVAL', 0), \
                mock.patch('gerapy.server.core.deployment.RUNNING_JOB_WAIT_TIMEOUT', 0):
            result, record = self.deploy(fake, pv.version, running_policy=POLICY_WAIT)
        self.assertEqual(result.status, DeploymentRecord.FAILED)
        self.assertEqual(record.reason, 'running_tasks')
        self.assertIsNone(fake.active_version())

    def test_concurrent_deployment_is_rejected(self):
        pv = self.upload_version()
        lock = FileLock('client-%s-project-demo' % self.client_model.id, timeout=0.2)
        lock.acquire()
        try:
            record = self.make_record(version=pv.version)
            with mock.patch('gerapy.server.core.deployment.get_scrapyd',
                            return_value=FakeScrapyd()):
                with self.assertRaises(ConcurrentDeploymentError):
                    DeploymentEngine().deploy(
                        self.client_model, 'demo', pv.version, record=record)
            record.refresh_from_db()
            self.assertEqual(record.status, DeploymentRecord.REJECTED)
            self.assertEqual(record.reason, 'concurrent_deployment')
        finally:
            lock.release()

    def test_data_incompatibility_blocks_deploy_when_enforced(self):
        old = self.upload_version(egg_bytes=b'old-schema')
        fake = FakeScrapyd()
        self.deploy(fake, old.version, keep_versions=2)
        # simulate a new version whose manifest drops a schema-definition file
        # by writing its manifest directly, then registering matching bytes
        new_pv, _ = VersionStore().build('demo', egg_bytes=b'new-no-schema')
        new_manifest = json.loads(new_pv.manifest)
        new_manifest.pop('demo/settings.py', None)
        new_pv.manifest = json.dumps(new_manifest)
        new_pv.save()
        with open(os.path.join(os.path.dirname(new_pv.egg_path), 'manifest.json'), 'w') as f:
            json.dump(new_manifest, f)
        # give the old version a manifest containing the schema file that the
        # new version drops
        old_manifest = json.loads(old.manifest)
        old_manifest['demo/models.py'] = {'hash': 'removed-in-new'}
        old.manifest = json.dumps(old_manifest)
        old.save()
        with open(os.path.join(os.path.dirname(old.egg_path), 'manifest.json'), 'w') as f:
            json.dump(old_manifest, f)
        result, record = self.deploy(fake, new_pv.version, enforce_data_compatibility=True)
        self.assertEqual(result.status, DeploymentRecord.FAILED)
        self.assertEqual(record.reason, 'data_incompatible')
        self.assertEqual(fake.active_version(), old.version)
        # opting out still deploys; the check row remains in the audit trail
        result2, record2 = self.deploy(FakeScrapyd(), new_pv.version,
                                       enforce_data_compatibility=False)
        self.assertEqual(result2.status, DeploymentRecord.SUCCESS)
        checks = json.loads(record2.health_checks)
        self.assertIn('data_compatibility', [c['check'] for c in checks])

    def test_daemon_unreachable_is_classified(self):
        pv = self.upload_version()
        record = self.make_record(version=pv.version)
        with mock.patch('gerapy.server.core.deployment.get_scrapyd',
                        return_value=FakeScrapyd()), \
                mock.patch('gerapy.server.core.deployment.requests') as http:
            import requests as real_requests
            http.exceptions.RequestException = real_requests.exceptions.RequestException
            http.get.side_effect = real_requests.exceptions.ConnectionError('refused')
            result = DeploymentEngine().deploy(
                self.client_model, 'demo', pv.version, record=record)
        self.assertEqual(result.status, DeploymentRecord.FAILED)
        self.assertEqual(record.reason, 'client_unreachable')

    def test_manual_rollback_reactivates_old_version(self):
        fake = FakeScrapyd()
        v1 = self.upload_version(egg_bytes=b'rollback-1').version
        with mock.patch.object(VersionStore, 'prune', return_value=[]):
            self.deploy(fake, v1, keep_versions=2)
            v2 = self.upload_version(egg_bytes=b'rollback-2').version
            self.deploy(fake, v2, keep_versions=2)
        self.assertEqual(fake.active_version(), v2)
        record = self.make_record()
        with mock.patch('gerapy.server.core.deployment.get_scrapyd', return_value=fake), \
                mock.patch('gerapy.server.core.deployment.requests') as http:
            http.get.return_value = mock.Mock(ok=True, status_code=200)
            http.exceptions.RequestException = Exception
            result = DeploymentEngine().rollback(
                self.client_model, 'demo', v1, operator='tester', record=record)
        self.assertEqual(result.status, DeploymentRecord.ROLLBACK)
        self.assertEqual(fake.active_version(), v1)
        self.assertEqual(result.version, v1)
        self.assertIn('manual rollback', result.rollback_reason)

    def test_audit_table_is_bounded(self):
        for i in range(5):
            DeploymentRecord.objects.create(project='demo', status=DeploymentRecord.SUCCESS)
        prune_records(keep=3)
        self.assertEqual(DeploymentRecord.objects.count(), 3)

    def test_deploy_log_files_are_bounded(self):
        record = self.make_record()
        log = DeployLogger(record.id)
        for _ in range(5):
            log.info('x' * 50000)
        # rotation means at most one .log plus one .1, each bounded
        logs = [name for name in os.listdir(os.path.dirname(log.path))
                if name.endswith('.log') or name.endswith('.log.1')]
        self.assertLessEqual(len(logs), 2)
        for name in logs:
            self.assertLessEqual(
                os.path.getsize(os.path.join(os.path.dirname(log.path), name)),
                deployment.DEPLOY_LOG_MAX_BYTES + 60000)
        rotate_deploy_logs(keep=0)
        self.assertEqual(
            len([n for n in os.listdir(os.path.dirname(log.path)) if n.startswith('deploy-')]), 0)


class DeploymentViewTest(DeploymentTempDirTestCase):

    def setUp(self):
        super(DeploymentViewTest, self).setUp()
        self.user = User.objects.create_user('alice', password='pw123456')
        from rest_framework.authtoken.models import Token
        self.token = Token.objects.create(user=self.user)
        self.http = HttpClient(HTTP_AUTHORIZATION='Token %s' % self.token.key)
        self.pv = self.upload_version()

    def test_audit_endpoint_filters_and_paginates(self):
        for status in (DeploymentRecord.SUCCESS, DeploymentRecord.FAILED):
            DeploymentRecord.objects.create(
                client=self.client_model, project='demo', status=status,
                operator='alice', version=self.pv.version)
        resp = self.http.get('/api/deployment/audit?project=demo&status=success&limit=1')
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body['total'], 1)
        self.assertEqual(body['data'][0]['status'], 'success')
        self.assertEqual(body['data'][0]['operator'], 'alice')

    def test_version_diff_endpoint(self):
        other = self.upload_version(egg_bytes=b'other-egg')
        resp = self.http.get('/api/project/demo/versions/diff?from=%s&to=%s'
                             % (self.pv.version, other.version))
        self.assertEqual(resp.status_code, 200)
        self.assertIn('summary', resp.json())

    def test_version_list_endpoint(self):
        resp = self.http.get('/api/project/demo/versions')
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body['result'], '1')
        self.assertTrue(any(v['version'] == self.pv.version for v in body['data']))

    def test_preflight_endpoint_with_fake_daemon(self):
        fake = FakeScrapyd()
        with mock.patch('gerapy.server.core.views.get_scrapyd', return_value=fake), \
                mock.patch('gerapy.server.core.views.check_daemon_health') as health:
            health.return_value = {'check': 'daemon', 'passed': True, 'detail': 'ok'}
            resp = self.http.get('/api/client/%d/preflight?project=demo' % self.client_model.id)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()['passed'])

    def test_deploy_unknown_version_returns_404_and_audits(self):
        resp = self.http.post(
            '/api/client/%d/project/demo/version/nope/deploy' % self.client_model.id,
            data=json.dumps({'keep_versions': 1}),
            content_type='application/json')
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(DeploymentRecord.objects.filter(reason='build_failed').exists())

    def test_concurrent_api_call_returns_409(self):
        lock = FileLock('client-%s-project-demo' % self.client_model.id, timeout=0.2)
        lock.acquire()
        try:
            resp = self.http.post(
                '/api/client/%d/project/demo/version/%s/deploy'
                % (self.client_model.id, self.pv.version),
                data=json.dumps({}), content_type='application/json')
            self.assertEqual(resp.status_code, 409)
            self.assertEqual(resp.json()['status'], 'rejected')
        finally:
            lock.release()

    def test_legacy_deploy_endpoint_default_single_version_regression(self):
        """the original /deploy URL must keep working with no new parameters"""
        fake = FakeScrapyd()
        with mock.patch('gerapy.server.core.views.get_scrapyd', return_value=fake), \
                mock.patch('gerapy.server.core.deployment.get_scrapyd', return_value=fake), \
                mock.patch('gerapy.server.core.deployment.requests') as http:
            http.get.return_value = mock.Mock(ok=True, status_code=200)
            http.exceptions.RequestException = Exception
            resp = self.http.post(
                '/api/client/%d/project/demo/deploy' % self.client_model.id,
                data=json.dumps({}), content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body['status'], 'success')
        self.assertEqual(fake.remote_release_ids(), [body['version']])
        # legacy Deploy row shape still present
        self.assertIn('id', body)
        self.assertTrue(Deploy.objects.filter(client=self.client_model).exists())

    def test_rollback_endpoint_records_operator(self):
        fake = FakeScrapyd()
        healthy = {'check': 'daemon', 'passed': True, 'detail': 'ok'}
        with mock.patch('gerapy.server.core.views.get_scrapyd', return_value=fake), \
                mock.patch('gerapy.server.core.deployment.get_scrapyd', return_value=fake), \
                mock.patch('gerapy.server.core.views.check_daemon_health', return_value=healthy), \
                mock.patch('gerapy.server.core.deployment.requests') as http:
            http.get.return_value = mock.Mock(ok=True, status_code=200)
            http.exceptions.RequestException = Exception
            # forward deploy the exact (content-addressed) version first, then
            # exercise rollback to that same retained version
            self.http.post(
                '/api/client/%d/project/demo/version/%s/deploy'
                % (self.client_model.id, self.pv.version),
                data=json.dumps({'keep_versions': 2}), content_type='application/json')
            resp = self.http.post(
                '/api/client/%d/project/demo/version/%s/rollback'
                % (self.client_model.id, self.pv.version),
                data=json.dumps({}), content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['status'], 'rollback')
        record = DeploymentRecord.objects.filter(status='rollback').first()
        self.assertEqual(record.operator, 'alice')

    def test_audit_requires_authentication(self):
        anon = HttpClient()
        resp = anon.get('/api/deployment/audit')
        self.assertEqual(resp.status_code, 401)

    def test_legacy_deploy_build_failure_is_audited_and_returns_500(self):
        # break the project so the on-demand build inside deploy fails; the
        # previous (absent) version stays untouched and the attempt is audited
        setup = os.path.join(self.workdir, 'projects', 'demo', 'setup.py')
        with open(setup, 'w') as f:
            f.write('raise RuntimeError("ModuleNotFoundError: No module named \'ghost\'")')
        fake = FakeScrapyd()
        # call the view directly: the Django 2.2 test client's technical-500
        # template rendering is incompatible with Python 3.13+, but the view's
        # own JSON response is what this behaviour is about
        from rest_framework.test import APIRequestFactory, force_authenticate
        factory = APIRequestFactory()
        request = factory.post('/x', {}, format='json')
        force_authenticate(request, user=self.user)
        with mock.patch('gerapy.server.core.views.get_scrapyd', return_value=fake), \
                mock.patch('gerapy.server.core.deployment.get_scrapyd', return_value=fake), \
                mock.patch('gerapy.server.core.views.check_daemon_health',
                           return_value={'check': 'daemon', 'passed': True, 'detail': 'ok'}):
            resp = views.project_deploy(
                request, self.client_model.id, 'demo')
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(json.loads(resp.content)['reason'], 'dependency_missing')
        record = DeploymentRecord.objects.filter(reason='dependency_missing').first()
        self.assertIsNotNone(record)
        self.assertEqual(record.status, DeploymentRecord.FAILED)
        # nothing reached the daemon
        self.assertEqual(fake.versions, {})


class PortAndDependencyClassificationTest(TestCase):

    def test_port_conflict_pattern(self):
        from gerapy.server.core.deployment import classify_error
        self.assertEqual(classify_error('OSError: [Errno 98] Address already in use'),
                         'port_conflict')

    def test_missing_dependency_pattern(self):
        from gerapy.server.core.deployment import classify_error
        self.assertEqual(classify_error("ModuleNotFoundError: No module named 'scrapy_redis'"),
                         'dependency_missing')

    def test_unknown_error_defaults_safely(self):
        from gerapy.server.core.deployment import classify_error
        self.assertEqual(classify_error('something else broke'), 'unknown')
        self.assertEqual(classify_error(''), 'unknown')


class FileLockTest(DeploymentTempDirTestCase):

    def test_lock_is_mutually_exclusive_across_threads(self):
        import threading
        holder = FileLock('shared-resource', timeout=0.3)
        holder.acquire()
        acquired = []

        def try_lock():
            try:
                FileLock('shared-resource', timeout=0.1).acquire()
                acquired.append(True)
            except ConcurrentDeploymentError:
                acquired.append(False)

        t = threading.Thread(target=try_lock)
        t.start()
        t.join()
        self.assertEqual(acquired, [False])
        holder.release()
        # once released, the same lock is obtainable again
        again = FileLock('shared-resource', timeout=0.1)
        again.acquire()
        again.release()

    def test_check_port_free_detects_occupied_port(self):
        import socket as socket_mod
        from gerapy.server.core.deployment import check_port_free, PortConflictError
        sock = socket_mod.socket(socket_mod.AF_INET, socket_mod.SOCK_STREAM)
        sock.bind(('127.0.0.1', 0))
        sock.listen(1)
        occupied_port = sock.getsockname()[1]
        try:
            with self.assertRaises(PortConflictError):
                check_port_free('127.0.0.1', occupied_port)
        finally:
            sock.close()
        # freed port passes
        result = check_port_free('127.0.0.1', occupied_port)
        self.assertTrue(result['passed'])
