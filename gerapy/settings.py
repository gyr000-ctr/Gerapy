from os import getenv, getcwd
from os.path import join
import time

APP_DEBUG = getenv('APP_DEBUG', False)

# logs
LOG_ENABLED = getenv('LOG_ENABLED', True)
LOG_TO_CONSOLE = getenv('LOG_TO_CONSOLE', True)
LOG_TO_FILE = getenv('LOG_TO_FILE', True)
LOG_LEVEL = getenv('LOG_LEVEL', 'DEBUG' if APP_DEBUG else 'INFO')
LOG_DIR = getenv('LOG_DIR', 'logs')
LOG_FORMAT = getenv('LOG_FORMAT',
                    '%(levelname)s - %(asctime)s - process: %(process)d - %(filename)s - %(name)s - %(lineno)d - %(module)s - %(message)s')
LOG_PATH = join(getcwd(), LOG_DIR, time.strftime("%Y%m%d%H%M%S", time.localtime()) + '.log')

# projects
PROJECTS_FOLDER = getenv('PROJECTS_FOLDER', 'projects')
LOGS_FOLDER = LOG_DIR

# versioned deployment
# immutable build artifacts (eggs + manifests) are kept here, separately from
# the working project tree so a switch never has to copy a whole project
VERSIONS_FOLDER = getenv('VERSIONS_FOLDER', 'versions')
# per-deployment log directory; every file is size-bounded and rotated
DEPLOY_LOGS_FOLDER = getenv('DEPLOY_LOGS_FOLDER', 'logs/deploy')
# how many built versions are retained on disk per project (oldest are pruned)
VERSION_RETENTION_COUNT = int(getenv('VERSION_RETENTION_COUNT', 10))
# how many deployment (audit) log files are retained on disk
DEPLOY_LOG_RETENTION_COUNT = int(getenv('DEPLOY_LOG_RETENTION_COUNT', 1000))
# maximum bytes kept inside a single deployment log file
DEPLOY_LOG_MAX_BYTES = int(getenv('DEPLOY_LOG_MAX_BYTES', 128 * 1024))
# how many DeploymentRecord rows are retained overall (audit trail upper bound)
DEPLOY_RECORD_RETENTION_COUNT = int(getenv('DEPLOY_RECORD_RETENTION_COUNT', 1000))
# seconds to wait for a deployment lock before rejecting a concurrent deploy
DEPLOY_LOCK_TIMEOUT = float(getenv('DEPLOY_LOCK_TIMEOUT', 5))
# seconds to wait for the scrapyd daemon during a health check
HEALTH_CHECK_TIMEOUT = float(getenv('HEALTH_CHECK_TIMEOUT', 5))
# health check is retried this many times with a short sleep between attempts
HEALTH_CHECK_RETRIES = int(getenv('HEALTH_CHECK_RETRIES', 2))
# keys whose values must never be written to logs or audit records
SECRET_KEY_PATTERNS = ('password', 'passwd', 'secret', 'token', 'authorization',
                       'api_key', 'apikey', 'credential', 'auth')

# scheduler
SCHEDULER_HEARTBEAT = 3
# apscheduler job defaults. The library defaults (misfire_grace_time=1s,
# max_instances=1) cause "Run time of job ... was missed!" warnings and skipped
# runs whenever the scheduler thread is briefly busy or a scrapyd request is
# slow. coalesce=True collapses several missed runs into a single one.
SCHEDULER_COALESCE = getenv('SCHEDULER_COALESCE', True)
SCHEDULER_MAX_INSTANCES = int(getenv('SCHEDULER_MAX_INSTANCES', 10))
SCHEDULER_MISFIRE_GRACE_TIME = int(getenv('SCHEDULER_MISFIRE_GRACE_TIME', 3600))

ADMINS = [
    'admin'
]
