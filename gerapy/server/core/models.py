# Create your models here.
from django.db.models import Model, CharField, GenericIPAddressField, IntegerField, TextField, DateTimeField, \
    ManyToManyField, ForeignKey, DO_NOTHING, BooleanField, BigAutoField


class Client(Model):
    """
    Scrapyd
    """
    name = CharField(max_length=255, default=None)
    ip = CharField(max_length=255, blank=True, null=True)
    port = IntegerField(default=6800, blank=True, null=True)
    description = TextField(blank=True, null=True)
    auth = IntegerField(default=0, blank=True, null=True)
    username = CharField(max_length=255, blank=True, null=True)
    password = CharField(max_length=255, blank=True, null=True)
    created_at = DateTimeField(auto_now_add=True, blank=True, null=True)
    updated_at = DateTimeField(auto_now=True, blank=True, null=True)
    
    def __str__(self):
        """
        to string
        :return: name
        """
        return self.name


class Project(Model):
    """
    Project Object, for configurable and un-configurable
    """
    name = CharField(max_length=255, default=None)
    description = CharField(max_length=255, null=True, blank=True)
    egg = CharField(max_length=255, null=True, blank=True)
    configuration = TextField(blank=True, null=True)
    configurable = IntegerField(default=0, blank=True)
    built_at = DateTimeField(default=None, blank=True, null=True)
    generated_at = DateTimeField(default=None, blank=True, null=True)
    created_at = DateTimeField(auto_now_add=True, blank=True, null=True)
    updated_at = DateTimeField(auto_now=True, blank=True, null=True)
    clients = ManyToManyField(Client, through='Deploy', unique=False)
    
    def __str__(self):
        """
        to string
        :return: name
        """
        return self.name


class Deploy(Model):
    """
    Deploy records
    """
    client = ForeignKey(Client, unique=False, on_delete=DO_NOTHING)
    project = ForeignKey(Project, unique=False, on_delete=DO_NOTHING)
    description = CharField(max_length=255, blank=True, null=True)
    deployed_at = DateTimeField(default=None, blank=True, null=True)
    created_at = DateTimeField(auto_now_add=True, blank=True, null=True)
    updated_at = DateTimeField(auto_now=True, blank=True, null=True)
    
    class Meta:
        unique_together = ('client', 'project')


class Monitor(Model):
    """
    Monitor configuration
    """
    name = CharField(max_length=255, default=None)
    description = CharField(max_length=255, null=True, blank=True)
    type = CharField(max_length=255, null=True, blank=True)
    configuration = TextField(null=True, blank=True)
    project = ForeignKey(Project, blank=True, null=True, on_delete=DO_NOTHING)
    created_at = DateTimeField(auto_now_add=True, blank=True, null=True)
    updated_at = DateTimeField(auto_now=True, blank=True, null=True)


class Task(Model):
    """
    Task for scheduler
    """
    clients = TextField(null=True, blank=True)
    project = CharField(max_length=255, null=True, blank=True)
    spider = CharField(max_length=255, null=True, blank=True)
    name = CharField(max_length=255, null=True, blank=True)
    args = TextField(null=True, blank=True)
    description = TextField(null=True, blank=True)
    trigger = CharField(max_length=255, null=True, blank=True)
    configuration = TextField(null=True, blank=True)
    modified = BooleanField(blank=True, default=False)
    created_at = DateTimeField(auto_now_add=True, blank=True, null=True)
    updated_at = DateTimeField(auto_now=True, blank=True, null=True)
    
    def __str__(self):
        """
        to string
        :return: name
        """
        return '_'.join([str(self.name), str(self.project), str(self.spider)])


class ProjectVersion(Model):
    """
    An immutable build of a project. The same source always produces the same
    version (derived from a content hash), so a version id can never be
    re-uploaded with different contents.
    """
    # lifecycle states of a build
    BUILDING = 'building'
    READY = 'ready'
    FAILED = 'failed'
    BUILD_STATUS_CHOICES = (
        (BUILDING, 'building'),
        (READY, 'ready'),
        (FAILED, 'failed'),
    )

    project = CharField(max_length=255, null=False, blank=False)
    # timestamp-prefixed so ids sort lexicographically in build order, with a
    # content hash suffix to guarantee immutability and global uniqueness
    version = CharField(max_length=255, null=False, blank=False, unique=True)
    checksum = CharField(max_length=64, null=False, blank=False)
    egg_path = CharField(max_length=1024, null=True, blank=True)
    size = IntegerField(default=0, blank=True)
    # json manifest: file list with per-file hashes, used for version diffs
    manifest = TextField(null=True, blank=True)
    description = CharField(max_length=255, null=True, blank=True)
    build_status = CharField(max_length=32, choices=BUILD_STATUS_CHOICES, default=BUILDING)
    # sanitized build output (secrets removed, length bounded)
    build_log = TextField(null=True, blank=True)
    built_at = DateTimeField(default=None, blank=True, null=True)
    created_at = DateTimeField(auto_now_add=True, blank=True, null=True)
    updated_at = DateTimeField(auto_now=True, blank=True, null=True)

    def __str__(self):
        return '%s@%s' % (self.project, self.version)


class DeploymentRecord(Model):
    """
    One row per deployment attempt (successful, failed or rolled back). It is
    the audit trail: version diff, health check results, rollback reason and
    the operator are all recorded here and queryable through the API.
    """
    # terminal and in-progress states. Anything other than SUCCESS keeps the
    # previously active version serving traffic.
    DEPLOYING = 'deploying'
    SUCCESS = 'success'
    FAILED = 'failed'
    ROLLED_BACK = 'rolled_back'
    REJECTED = 'rejected'
    ROLLBACK = 'rollback'
    STATUS_CHOICES = (
        (DEPLOYING, 'deploying'),
        (SUCCESS, 'success'),
        (FAILED, 'failed'),
        (ROLLED_BACK, 'rolled_back'),
        (REJECTED, 'rejected'),
        (ROLLBACK, 'rollback'),
    )

    # failure categories, mapped from the underlying exceptions so callers can
    # distinguish build failures, port conflicts and missing dependencies
    REASON_BUILD_FAILED = 'build_failed'
    REASON_PORT_CONFLICT = 'port_conflict'
    REASON_DEPENDENCY_MISSING = 'dependency_missing'
    REASON_HEALTH_CHECK = 'health_check_failed'
    REASON_CLIENT_UNREACHABLE = 'client_unreachable'
    REASON_RUNNING_TASKS = 'running_tasks'
    REASON_CONCURRENT = 'concurrent_deployment'
    REASON_DATA_INCOMPATIBLE = 'data_incompatible'
    REASON_UNKNOWN = 'unknown'

    id = BigAutoField(primary_key=True)
    client = ForeignKey(Client, null=True, blank=True, on_delete=DO_NOTHING)
    client_name = CharField(max_length=255, null=True, blank=True)
    project = CharField(max_length=255, null=False, blank=False)
    version = CharField(max_length=255, null=True, blank=True)
    # version that was active before this attempt; the rollback target
    previous_version = CharField(max_length=255, null=True, blank=True)
    status = CharField(max_length=32, choices=STATUS_CHOICES, default=DEPLOYING)
    reason = CharField(max_length=64, null=True, blank=True)
    # json: added/removed/changed files between previous and new version
    diff = TextField(null=True, blank=True)
    # json list of per-attempt health check results (already sanitized)
    health_checks = TextField(null=True, blank=True)
    # free-text explanation for a rollback, sanitized
    rollback_reason = TextField(null=True, blank=True)
    # policy chosen when running jobs were detected
    running_task_policy = CharField(max_length=32, null=True, blank=True)
    running_jobs = IntegerField(default=0, blank=True)
    operator = CharField(max_length=255, null=True, blank=True)
    # bounded, sanitized deployment log
    log = TextField(null=True, blank=True)
    log_path = CharField(max_length=1024, null=True, blank=True)
    started_at = DateTimeField(default=None, blank=True, null=True)
    finished_at = DateTimeField(default=None, blank=True, null=True)
    created_at = DateTimeField(auto_now_add=True, blank=True, null=True)
    updated_at = DateTimeField(auto_now=True, blank=True, null=True)
