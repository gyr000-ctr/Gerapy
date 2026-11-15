import re
from pathlib import Path
from urllib.parse import unquote
import base64
import json
import os
import requests
import pytz
import pymongo
from shutil import rmtree
from requests.exceptions import ConnectionError
from os.path import join, exists
from django.shortcuts import render
from django.core.serializers import serialize
from django.http import HttpResponse
from django.forms.models import model_to_dict
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from subprocess import Popen, PIPE
from gerapy import get_logger
from gerapy.server.core.response import JsonResponse
from gerapy.cmd.init import PROJECTS_FOLDER
from gerapy.server.server.settings import TIME_ZONE
from gerapy.server.core.models import Client, Project, Deploy, Monitor, Task, DeploymentRecord
from gerapy.server.core.build import build_project, find_egg
from gerapy.server.core.deployment import (
    VersionStore, DeploymentEngine, DeployLogger, ConcurrentDeploymentError,
    DeploymentError, sanitize as sanitize_text, truncate as truncate_text,
    diff_manifests, read_manifest,
    check_daemon_health, get_running_jobs, existing_versions,
    DEFAULT_RUNNING_POLICY, POLICY_ABORT, POLICY_CONTINUE, POLICY_WAIT,
    prune_records, rotate_deploy_logs,
)
from gerapy.server.core.utils import IGNORES, is_in_curdir, scrapyd_url, log_url, get_tree, get_scrapyd, process_html, bytes2str, \
    clients_of_task, get_job_id, log_exception, is_safe_url
from django_apscheduler.models import DjangoJob, DjangoJobExecution
from django.core.files.storage import FileSystemStorage
import zipfile

logger = get_logger(__name__)

# fields a client is allowed to set through the API; prevents mass assignment
# of arbitrary model attributes from the raw request body
CLIENT_ALLOWED_FIELDS = {'name', 'ip', 'port', 'description', 'auth', 'username', 'password'}


@log_exception()
@api_view(['GET'])
# @permission_classes([IsAuthenticated])
def index(request):
    """
    render index page
    :param request: request object
    :return: page
    """
    return render(request, 'index.html')


@log_exception()
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def index_status(request):
    """
    index statistics
    :param request: request object
    :return: json
    """
    if request.method == 'GET':
        clients = Client.objects.all()
        data = {
            'success': 0,
            'error': 0,
            'project': 0,
        }
        # clients info
        for client in clients:
            try:
                requests.get(scrapyd_url(client.ip, client.port), timeout=1)
                data['success'] += 1
            except ConnectionError:
                data['error'] += 1
        path = os.path.abspath(join(os.getcwd(), PROJECTS_FOLDER))
        files = os.listdir(path)
        # projects info
        for file in files:
            if os.path.isdir(join(path, file)) and not file in IGNORES:
                data['project'] += 1
        return JsonResponse(data)


@log_exception()
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def client_index(request):
    """
    get client list
    :param request: request object
    :return: client list
    """
    return HttpResponse(serialize('json', Client.objects.order_by('-id')))


@log_exception()
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def client_info(request, client_id):
    """
    get client info
    :param request: request object
    :param id: client id
    :return: json
    """
    if request.method == 'GET':
        return JsonResponse(model_to_dict(Client.objects.get(id=client_id)))


@log_exception()
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def client_status(request, client_id):
    """
    get client status
    :param request: request object
    :param client_id: client id
    :return: json
    """
    if request.method == 'GET':
        # get client object
        client = Client.objects.get(id=client_id)
        requests.get(scrapyd_url(client.ip, client.port), timeout=3)
        return JsonResponse({'result': '1'})


@log_exception()
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def client_update(request, client_id):
    """
    update client info
    :param request: request object
    :param client_id: client id
    :return: json
    """
    if request.method == 'POST':
        client = Client.objects.filter(id=client_id)
        data = {k: v for k, v in json.loads(request.body).items() if k in CLIENT_ALLOWED_FIELDS}
        client.update(**data)
        return JsonResponse(model_to_dict(Client.objects.get(id=client_id)))


@log_exception()
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def client_create(request):
    """
    create a client
    :param request: request object
    :return: json
    """
    if request.method == 'POST':
        data = {k: v for k, v in json.loads(request.body).items() if k in CLIENT_ALLOWED_FIELDS}
        client = Client.objects.create(**data)
        return JsonResponse(model_to_dict(client))


@log_exception()
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def client_remove(request, client_id):
    """
    remove a client
    :param request: request object
    :param client_id: client id
    :return: json
    """
    if request.method == 'POST':
        client = Client.objects.get(id=client_id)
        # delete deploy
        Deploy.objects.filter(client=client).delete()
        # delete client
        Client.objects.filter(id=client_id).delete()
        return JsonResponse({'result': '1'})


@log_exception()
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def spider_list(request, client_id, project_name):
    """
    get spider list from one client
    :param request: request Object
    :param client_id: client id
    :param project_name: project name
    :return: json
    """
    if request.method == 'GET':
        client = Client.objects.get(id=client_id)
        scrapyd = get_scrapyd(client)
        spiders = scrapyd.list_spiders(project_name)
        spiders = [{'name': spider, 'id': index + 1}
                   for index, spider in enumerate(spiders)]
        return JsonResponse(spiders)


@log_exception()
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def spider_start(request, client_id, project_name, spider_name):
    """
    start a spider
    :param request: request object
    :param client_id: client id
    :param project_name: project name
    :param spider_name: spider name
    :return: json
    """
    if request.method == 'GET':
        client = Client.objects.get(id=client_id)
        scrapyd = get_scrapyd(client)
        job = scrapyd.schedule(project_name, spider_name)
        return JsonResponse({'job': job})


@log_exception()
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def project_list(request, client_id):
    """
    project deployed list on one client
    :param request: request object
    :param client_id: client id
    :return: json
    """
    if request.method == 'GET':
        client = Client.objects.get(id=client_id)
        scrapyd = get_scrapyd(client)
        projects = scrapyd.list_projects()
        return JsonResponse(projects)


@log_exception()
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def project_index(request):
    """
    project index list
    :param request: request object
    :return: json
    """
    if request.method == 'GET':
        path = os.path.abspath(join(os.getcwd(), PROJECTS_FOLDER))
        files = os.listdir(path)
        project_list = []
        for file in files:
            if os.path.isdir(join(path, file)) and not file in IGNORES:
                project_list.append({'name': file})
        return JsonResponse(project_list)


@log_exception()
@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def project_configure(request, project_name):
    """
    get configuration
    :param request: request object
    :param project_name: project name
    :return: json
    """
    # get configuration
    if request.method == 'GET':
        project = Project.objects.get(name=project_name)
        project = model_to_dict(project)
        project['configuration'] = json.loads(
            project['configuration']) if project['configuration'] else None
        return JsonResponse(project)

    # update configuration
    elif request.method == 'POST':
        project = Project.objects.filter(name=project_name)
        data = json.loads(request.body)
        configuration = json.dumps(
            data.get('configuration'), ensure_ascii=False)
        project.update(**{'configuration': configuration})
        # for safe protection
        project_name = re.sub(
            '[\s\!\@\#\$\;\&\*\~\"\'\{\}\]\[\-\+\%\^]+', '', project_name)
        # execute generate cmd
        cmd = ['gerapy', 'generate', project_name]
        p = Popen(cmd, shell=False, stdin=PIPE, stdout=PIPE, stderr=PIPE)
        stdout, stderr = bytes2str(p.stdout.read()), bytes2str(p.stderr.read())

        if not stderr:
            return JsonResponse({'status': '1'})
        else:
            return JsonResponse({'status': '0', 'message': stderr})


@log_exception()
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def project_tree(request, project_name):
    """
    get file tree of project
    :param request: request object
    :param project_name: project name
    :return: json of tree
    """
    if request.method == 'GET':
        path = os.path.abspath(join(os.getcwd(), PROJECTS_FOLDER))
        # get tree data
        tree = get_tree(join(path, project_name))
        return JsonResponse(tree)


@log_exception()
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def project_create(request):
    """
    create a configurable project
    :param request: request object
    :return: json
    """
    if request.method == 'POST':
        data = json.loads(request.body)
        data['configurable'] = 1
        project, result = Project.objects.update_or_create(**data)
        # generate a single project folder
        path = join(os.path.abspath(
            join(os.getcwd(), PROJECTS_FOLDER)), data['name'])
        os.mkdir(path)
        return JsonResponse(model_to_dict(project))


@log_exception()
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def project_upload(request):
    """
    upload project
    :param request: request object
    :return: json
    """
    if request.method == 'POST':
        file = request.FILES['file']
        file_name = file.name
        fs = FileSystemStorage(PROJECTS_FOLDER)
        zip_file_name = fs.save(file_name, file)
        logger.debug('zip file name %s', zip_file_name)
        # extract zip file safely to prevent path traversal (a.k.a. Zip Slip)
        projects_dir = os.path.realpath(PROJECTS_FOLDER)
        zip_path = join(PROJECTS_FOLDER, zip_file_name)
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            for member in zip_ref.namelist():
                member_path = os.path.realpath(join(projects_dir, member))
                if member_path != projects_dir and not member_path.startswith(projects_dir + os.sep):
                    logger.error('rejected zip entry with path traversal: %s', member)
                    return JsonResponse(
                        {'status': False, 'message': 'Invalid file path in archive'}, status=400)
            zip_ref.extractall(projects_dir)
        logger.debug('extracted files to %s', PROJECTS_FOLDER)
        return JsonResponse({'status': True})


@log_exception()
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def project_clone(request):
    """
    clone project from github
    :param request: request object
    :return: json
    """
    if request.method == 'POST':
        data = json.loads(request.body)
        address = data.get('address')
        if not address.startswith('http'):
            return JsonResponse({'status': False})
        address = address + '.git' if not address.endswith('.git') else address
        cmd = ['git', 'clone', address, join(PROJECTS_FOLDER, Path(address).stem)]
        logger.debug('clone cmd %s', cmd)
        p = Popen(cmd, shell=False, stdin=PIPE, stdout=PIPE, stderr=PIPE)
        stdout, stderr = bytes2str(p.stdout.read()), bytes2str(p.stderr.read())
        logger.debug('clone run result %s', stdout)
        if stderr:
            logger.error(stderr)
        return JsonResponse({'status': True}) if not stderr else JsonResponse({'status': False})


@log_exception()
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def project_remove(request, project_name):
    """
    remove project from disk and db
    :param request: request object
    :param project_name: project name
    :return: result of remove
    """
    if request.method == 'POST':
        # delete deployments
        project = Project.objects.get(name=project_name)
        Deploy.objects.filter(project=project).delete()
        # delete project
        result = Project.objects.filter(name=project_name).delete()
        # get project path
        path = join(os.path.abspath(os.getcwd()), PROJECTS_FOLDER)
        project_path = join(path, project_name)
        # delete project file tree
        if exists(project_path):
            rmtree(project_path)
        return JsonResponse({'result': result})


@log_exception()
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def project_version(request, client_id, project_name):
    """
    get project deploy version
    :param request: request object
    :param client_id: client id
    :param project_name: project name
    :return: deploy version of project
    """
    if request.method == 'GET':
        # get client and project model
        client = Client.objects.get(id=client_id)
        project = Project.objects.get(name=project_name)
        scrapyd = get_scrapyd(client)
        # if deploy info exists in db, return it
        if Deploy.objects.filter(client=client, project=project):
            deploy = Deploy.objects.get(client=client, project=project)
        # if deploy info does not exists in db, create deploy info
        else:
            try:
                versions = scrapyd.list_versions(project_name)
            except ConnectionError:
                logger.exception('cannot connect to scrapyd', exc_info=True)
                return JsonResponse({'message': 'Connect Error'}, status=500)
            if len(versions) > 0:
                version = versions[-1]
                deployed_at = timezone.datetime.fromtimestamp(
                    int(version), tz=pytz.timezone(TIME_ZONE))
            else:
                deployed_at = None
            deploy, result = Deploy.objects.update_or_create(
                client=client, project=project, deployed_at=deployed_at)
        # return deploy json info
        return JsonResponse(model_to_dict(deploy))


@log_exception()
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def project_deploy(request, client_id, project_name):
    """
    deploy project operation

    Backward-compatible endpoint: by default it keeps the historical
    single-version behaviour (keep_versions=1, running jobs continue on their
    loaded egg). The deployment itself now goes through the versioned engine:
    an immutable build is created, health-checked under a staging version and
    atomically switched; any failure rolls back automatically and leaves the
    previous version active.
    """
    if request.method == 'POST':
        payload = parse_json_body(request)
        # default deployment keeps exactly one active version, as before
        keep_versions = int(payload.get('keep_versions', 1))
        running_policy = payload.get('running_policy', DEFAULT_RUNNING_POLICY)
        description = payload.get('description')
        return _execute_deploy(request, client_id, project_name,
                               keep_versions=keep_versions,
                               running_policy=running_policy,
                               description=description,
                               version=payload.get('version'))


def parse_json_body(request):
    """tolerant JSON body parser; never raises on empty/non-json input"""
    if not request.body:
        return {}
    try:
        data = json.loads(request.body)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def current_operator(request):
    """the authenticated user behind a deployment, for the audit trail"""
    user = getattr(request, 'user', None)
    if user and getattr(user, 'is_authenticated', False):
        return user.get_username()
    return None


def _execute_deploy(request, client_id, project_name, *, keep_versions, running_policy,
                    description, version=None, expected_spiders=None,
                    enforce_data_compatibility=True):
    """
    shared deploy path used by both the legacy endpoint and the versioned API.
    Creates the audit row up front so even rejected/failed attempts are
    recorded, then hands control to the DeploymentEngine.
    """
    try:
        client = Client.objects.get(id=client_id)
    except Client.DoesNotExist:
        return JsonResponse({'message': 'client not found'}, status=404)
    # a project may exist on disk before it has a Project row (the historical
    # flow), so this is a soft warning logged on the record rather than a 404
    if not Project.objects.filter(name=project_name):
        Project.objects.get_or_create(name=project_name)

    record = DeploymentRecord.objects.create(
        client=client, client_name=client.name, project=project_name,
        status=DeploymentRecord.DEPLOYING, operator=current_operator(request),
        running_task_policy=running_policy)
    store = VersionStore()
    engine = DeploymentEngine(store)
    try:
        # build (or reuse an immutable version) unless a specific ready version
        # was requested
        if not version:
            project_version, _created = store.build(
                project_name, description=description,
                build_logger=DeployLogger(record.id))
            version = project_version.version
        else:
            project_version = store.get(project_name, version)
            if not project_version:
                record.status = DeploymentRecord.FAILED
                record.reason = DeploymentRecord.REASON_BUILD_FAILED
                record.rollback_reason = 'requested version %s not found' % version
                record.finished_at = timezone.now()
                record.save()
                prune_records()
                return JsonResponse({'message': 'version not found', 'record_id': record.id}, status=404)
        result = engine.deploy(
            client, project_name, version,
            operator=current_operator(request),
            running_policy=running_policy,
            keep_versions=keep_versions,
            expected_spiders=expected_spiders,
            enforce_data_compatibility=enforce_data_compatibility,
            record=record)
    except ConcurrentDeploymentError as e:
        record.status = DeploymentRecord.REJECTED
        record.reason = DeploymentRecord.REASON_CONCURRENT
        record.rollback_reason = truncate_text(sanitize_text(str(e)))
        record.finished_at = timezone.now()
        record.save()
        prune_records()
        return JsonResponse({'message': str(e), 'status': DeploymentRecord.REJECTED,
                             'record_id': record.id}, status=409)
    except DeploymentError as e:
        # engine normally records failures itself; this covers failures raised
        # before the engine took ownership (e.g. during build)
        if record.status == DeploymentRecord.DEPLOYING:
            record.status = DeploymentRecord.FAILED
            record.reason = e.reason
            record.rollback_reason = truncate_text(sanitize_text(str(e)))
            record.finished_at = timezone.now()
            record.save()
            prune_records()
        return JsonResponse({'message': str(e), 'status': record.status,
                             'reason': record.reason, 'record_id': record.id}, status=500)

    if result.status == DeploymentRecord.SUCCESS:
        deploy = Deploy.objects.filter(client=client, project=Project.objects.get(name=project_name)).first()
        data = model_to_dict(deploy) if deploy else {'record_id': result.id}
        data['record_id'] = result.id
        data['version'] = result.version
        data['status'] = result.status
        return JsonResponse(data)
    return JsonResponse(
        {'message': result.rollback_reason or 'deployment failed',
         'status': result.status, 'reason': result.reason,
         'record_id': result.id, 'previous_version': result.previous_version},
        status=500)


@log_exception()
@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def project_build(request, project_name):
    """
    get build info or execute build operation
    :param request: request object
    :param project_name: project name
    :return: json
    """
    # get project folder
    path = os.path.abspath(join(os.getcwd(), PROJECTS_FOLDER))
    project_path = join(path, project_name)

    # get build version
    if request.method == 'GET':
        egg = find_egg(project_path)
        # if built, save or update project to db
        if egg:
            built_at = timezone.datetime.fromtimestamp(os.path.getmtime(join(project_path, egg)),
                                                       tz=pytz.timezone(TIME_ZONE))
            if not Project.objects.filter(name=project_name):
                Project(name=project_name, built_at=built_at, egg=egg).save()
                model = Project.objects.get(name=project_name)
            else:
                model = Project.objects.get(name=project_name)
                model.built_at = built_at
                model.egg = egg
                model.save()
        # if not built, just save project name to db
        else:
            if not Project.objects.filter(name=project_name):
                Project(name=project_name).save()
            model = Project.objects.get(name=project_name)
        # transfer model to dict then dumps it to json
        data = model_to_dict(model)
        return JsonResponse(data)

    # build operation manually by clicking button
    elif request.method == 'POST':
        data = json.loads(request.body)
        description = data['description']
        build_project(project_name)
        egg = find_egg(project_path)
        if not egg:
            return JsonResponse({'message': 'egg not found'}, status=500)
        # update built_at info
        built_at = timezone.now()
        # if project does not exists in db, create it
        if not Project.objects.filter(name=project_name):
            Project(name=project_name, description=description,
                    built_at=built_at, egg=egg).save()
            model = Project.objects.get(name=project_name)
        # if project exists, update egg, description, built_at info
        else:
            model = Project.objects.get(name=project_name)
            model.built_at = built_at
            model.egg = egg
            model.description = description
            model.save()
        # transfer model to dict then dumps it to json
        data = model_to_dict(model)
        return JsonResponse(data)


@log_exception()
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def project_parse(request, project_name):
    """
    parse project
    :param request: request object
    :param project_name: project name
    :return: requests, items, response
    """
    if request.method == 'POST':
        project_path = join(PROJECTS_FOLDER, project_name)
        data = json.loads(request.body)
        logger.debug('post data %s', data)
        spider_name = data.get('spider')
        args = {
            'start': data.get('start', False),
            'method': data.get('method', 'GET'),
            'url': data.get('url'),
            'callback': data.get('callback'),
            'cookies': "'" + json.dumps(data.get('cookies', {}), ensure_ascii=False) + "'",
            'headers': "'" + json.dumps(data.get('headers', {}), ensure_ascii=False) + "'",
            'meta': "'" + json.dumps(data.get('meta', {}), ensure_ascii=False) + "'",
            'dont_filter': data.get('dont_filter', False),
            'priority': data.get('priority', 0),
        }
        # set request body
        body = data.get('body', '')
        if args.get('method').lower() != 'get':
            args['body'] = "'" + json.dumps(body, ensure_ascii=False) + "'"

        args_array = []
        for arg, value in args.items():
            args_array.append(f'--{arg}')
            args_array.append(f'{value}')
        cmd = ['gerapy', 'parse'] + args_array + [project_path] + [spider_name]
        logger.debug('parse cmd %s', cmd)
        p = Popen(cmd, shell=False, stdin=PIPE,
                         stdout=PIPE, stderr=PIPE, close_fds=True)
        stdout, stderr = bytes2str(p.stdout.read()), bytes2str(p.stderr.read())
        logger.debug('stdout %s, stderr %s', stdout, stderr)
        if not stderr:
            return JsonResponse({'status': True, 'result': json.loads(stdout)})
        else:
            return JsonResponse({'status': False, 'message': stderr})


@log_exception()
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def project_file_read(request):
    """
    get content of project file
    :param request: request object
    :return: file content
    """
    if request.method == 'POST':
        data = json.loads(request.body)
        path = join(data['path'], data['label'])
        if not is_in_curdir(path):
            return JsonResponse({'result': '0'})
        # binary file
        with open(path, 'rb') as f:
            return HttpResponse(f.read().decode('utf-8'))


@log_exception()
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def project_file_update(request):
    """
    update project file
    :param request: request object
    :return: result of update
    """
    if request.method == 'POST':
        data = json.loads(request.body)
        path = join(data['path'], data['label'])
        code = data['code']
        if not is_in_curdir(path):
            return JsonResponse({'result': '0'})
        with open(path, 'w', encoding='utf-8') as f:
            f.write(code)
            return JsonResponse({'result': '1'})


@log_exception()
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def project_file_create(request):
    """
    create project file
    :param request: request object
    :return: result of create
    """
    if request.method == 'POST':
        data = json.loads(request.body)
        path = join(data['path'], data['name'])
        if not is_in_curdir(path):
            return JsonResponse({'result': '0'})
        open(path, 'w', encoding='utf-8').close()
        return JsonResponse({'result': '1'})


@log_exception()
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def project_file_delete(request):
    """
    delete project file
    :param request: request object
    :return: result of delete
    """
    if request.method == 'POST':
        data = json.loads(request.body)
        path = join(data['path'], data['label'])
        if not is_in_curdir(path):
            return JsonResponse({'result': '0'})
        result = os.remove(path)
        return JsonResponse({'result': result})


@log_exception()
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def project_file_rename(request):
    """
    rename file name
    :param request: request object
    :return: result of rename
    """
    if request.method == 'POST':
        data = json.loads(request.body)
        pre = join(data['path'], data['pre'])
        new = join(data['path'], data['new'])
        if not is_in_curdir(pre) or not is_in_curdir(new):
            return JsonResponse({'result': '0'})
        os.rename(pre, new)
        return JsonResponse({'result': '1'})


@log_exception()
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def job_list(request, client_id, project_name):
    """
    get job list of project from one client
    :param request: request object
    :param client_id: client id
    :param project_name: project name
    :return: list of jobs
    """
    if request.method == 'GET':
        client = Client.objects.get(id=client_id)
        scrapyd = get_scrapyd(client)
        result = scrapyd.list_jobs(project_name)
        jobs = []
        statuses = ['pending', 'running', 'finished']
        for status in statuses:
            for job in result.get(status):
                job['status'] = status
                jobs.append(job)
        return JsonResponse(jobs)


@log_exception()
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def job_log(request, client_id, project_name, spider_name, job_id):
    """
    get log of jog
    :param request: request object
    :param client_id: client id
    :param project_name: project name
    :param spider_name: spider name
    :param job_id: job id
    :return: log of job
    """
    if request.method == 'GET':
        client = Client.objects.get(id=client_id)
        # get log url
        url = log_url(client.ip, client.port,
                      project_name, spider_name, job_id)
        # get last 1000 bytes of log
        response = requests.get(url, timeout=5, headers={
            'Range': 'bytes=-1000'
        }, auth=(client.username, client.password) if client.auth else None)
        # Get encoding
        encoding = response.apparent_encoding
        # log not found
        if response.status_code == 404:
            return JsonResponse({'message': 'Log Not Found'}, status=404)
        # bytes to string
        text = response.content.decode(encoding, errors='replace')
        return HttpResponse(text)


@log_exception()
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def job_cancel(request, client_id, project_name, job_id):
    """
    cancel a job
    :param request: request object
    :param client_id: client id
    :param project_name: project name
    :param job_id: job id
    :return: json of cancel
    """
    if request.method == 'GET':
        client = Client.objects.get(id=client_id)
        scrapyd = get_scrapyd(client)
        result = scrapyd.cancel(project_name, job_id)
        return JsonResponse(result)


@log_exception()
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def del_version(request, client_id, project, version):
    if request.method == 'GET':
        client = Client.objects.get(id=client_id)
        scrapyd = get_scrapyd(client)
        result = scrapyd.delete_version(project=project, version=version)
        return JsonResponse(result)


@log_exception()
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def del_project(request, client_id, project):
    if request.method == 'GET':
        client = Client.objects.get(id=client_id)
        scrapyd = get_scrapyd(client)
        result = scrapyd.delete_project(project=project)
        return JsonResponse(result)


@log_exception()
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def monitor_db_list(request):
    """
    get monitor db list
    :param request: request object
    :return: json of db list
    """
    if request.method == 'POST':
        data = json.loads(request.body)
        url = data['url']
        type = data['type']
        if type == 'MongoDB':
            client = pymongo.MongoClient(url)
            dbs = client.list_database_names()
            return JsonResponse(dbs)


@log_exception()
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def monitor_collection_list(request):
    """
    get monitor collection list
    :param request: request object
    :return: json of collection list
    """
    if request.method == 'POST':
        data = json.loads(request.body)
        url = data['url']
        db = data['db']
        type = data['type']
        if type == 'MongoDB':
            client = pymongo.MongoClient(url)
            db = client[db]
            collections = db.collection_names()
            return JsonResponse(collections)


@log_exception()
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def monitor_create(request):
    """
    create a monitor
    :param request: request object
    :return: json of create
    """
    if request.method == 'POST':
        data = json.loads(request.body)
        data = data['form']
        data['configuration'] = json.dumps(
            data['configuration'], ensure_ascii=False)
        monitor = Monitor.objects.create(**data)
        return JsonResponse(model_to_dict(monitor))


@log_exception()
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def task_create(request):
    """
    add task
    :param request: request object
    :return: Bool
    """
    if request.method == 'POST':
        data = json.loads(request.body)
        task = Task.objects.create(clients=json.dumps(data.get('clients'), ensure_ascii=False),
                                   project=data.get('project'),
                                   name=data.get('name'),
                                   spider=data.get('spider'),
                                   trigger=data.get('trigger'),
                                   configuration=json.dumps(
                                       data.get('configuration'), ensure_ascii=False),
                                   modified=1)
        return JsonResponse({'result': '1', 'data': model_to_dict(task)})


@log_exception()
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def task_update(request, task_id):
    """
    update task info
    :param request: request object
    :param task_id: task id
    :return: json
    """
    if request.method == 'POST':
        task = Task.objects.filter(id=task_id)
        data = json.loads(request.body)
        data['clients'] = json.dumps(data.get('clients'), ensure_ascii=False)
        data['configuration'] = json.dumps(
            data.get('configuration'), ensure_ascii=False)
        data['modified'] = 1
        task.update(**data)
        return JsonResponse(model_to_dict(Task.objects.get(id=task_id)))


@log_exception()
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def task_remove(request, task_id):
    """
    remove task by task_id
    :param request:
    :return:
    """
    if request.method == 'POST':
        # delete job from DjangoJob
        task = Task.objects.get(id=task_id)
        clients = clients_of_task(task)
        for client in clients:
            job_id = get_job_id(client, task)
            DjangoJob.objects.filter(id=job_id).delete()
        # delete task
        Task.objects.filter(id=task_id).delete()
        return JsonResponse({'result': '1'})


@log_exception()
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def task_info(request, task_id):
    """
    get task info
    :param request: request object
    :param task_id: task id
    :return: json
    """
    if request.method == 'GET':
        task = Task.objects.get(id=task_id)
        data = model_to_dict(task)
        data['clients'] = json.loads(data.get('clients'))
        data['configuration'] = json.loads(data.get('configuration'))
        return JsonResponse({'data': data})


@log_exception()
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def task_index(request):
    """
    get all tasks
    :param request:
    :return:
    """
    if request.method == 'GET':
        tasks = Task.objects.values()
        return JsonResponse({'result': '1', 'data': tasks})


@log_exception()
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def task_status(request, task_id):
    """
    get task status info
    :param request: request object
    :param task_id: task id
    :return:
    """
    if request.method == 'GET':
        result = []
        task = Task.objects.get(id=task_id)
        clients = clients_of_task(task)
        for client in clients:
            job_id = get_job_id(client, task)
            jobs = DjangoJob.objects.filter(id=job_id)
            logger.debug('jobs from djangojob %s', jobs)
            # if job does not exist, for date mode exceed time
            if not jobs:
                continue
            job = DjangoJob.objects.get(id=job_id)
            executions = serialize(
                'json', DjangoJobExecution.objects.filter(job=job))
            result.append({
                'client': model_to_dict(client),
                'next': job.next_run_time,
                'executions': json.loads(executions)
            })
        return JsonResponse({'data': result})


@log_exception()
@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def project_version_build(request, project_name):
    """
    build (or reuse) an immutable version of a project, or list existing
    versions. A build never mutates the active deployment: it only produces a
    content-addressed artifact under VERSIONS_FOLDER.
    """
    store = VersionStore()
    if request.method == 'GET':
        versions = [
            {'id': v.id, 'project': v.project, 'version': v.version,
             'checksum': v.checksum, 'size': v.size, 'description': v.description,
             'build_status': v.build_status, 'built_at': v.built_at,
             'created_at': v.created_at}
            for v in store.list_versions(project_name)
        ]
        return JsonResponse({'result': '1', 'data': versions})

    data = parse_json_body(request)
    description = data.get('description')
    record = DeploymentRecord.objects.create(
        project=project_name, status=DeploymentRecord.DEPLOYING,
        operator=current_operator(request))
    log = DeployLogger(record.id)
    try:
        project_version, created = store.build(project_name, description=description, build_logger=log)
    except DeploymentError as e:
        record.status = DeploymentRecord.FAILED
        record.reason = e.reason
        record.version = None
        record.rollback_reason = truncate_text(sanitize_text(str(e)))
        record.log = log.content()
        record.log_path = log.path
        record.finished_at = timezone.now()
        record.save()
        rotate_deploy_logs()
        prune_records()
        return JsonResponse({'message': str(e), 'reason': e.reason,
                             'record_id': record.id}, status=500)
    record.version = project_version.version
    record.status = DeploymentRecord.SUCCESS
    record.reason = None
    record.rollback_reason = 'build only, no deployment' if not created else 'new immutable version built'
    record.log = log.content()
    record.log_path = log.path
    record.finished_at = timezone.now()
    record.save()
    rotate_deploy_logs()
    prune_records()
    return JsonResponse({'result': '1', 'reused': not created,
                         'version': project_version.version,
                         'checksum': project_version.checksum, 'size': project_version.size,
                         'record_id': record.id})


@log_exception()
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def project_version_upload(request, project_name):
    """
    register an uploaded egg as an immutable, content-addressed version
    without touching the active deployment
    """
    if 'file' not in request.FILES:
        return JsonResponse({'message': 'egg file is required'}, status=400)
    egg_bytes = request.FILES['file'].read()
    if not egg_bytes:
        return JsonResponse({'message': 'uploaded egg is empty'}, status=400)
    data = parse_json_body(request)
    description = data.get('description') or request.POST.get('description')
    store = VersionStore()
    record = DeploymentRecord.objects.create(
        project=project_name, status=DeploymentRecord.DEPLOYING,
        operator=current_operator(request))
    log = DeployLogger(record.id)
    try:
        project_version, created = store.build(
            project_name, description=description, egg_bytes=egg_bytes, build_logger=log)
    except DeploymentError as e:
        record.status = DeploymentRecord.FAILED
        record.reason = e.reason
        record.rollback_reason = truncate_text(sanitize_text(str(e)))
        record.log = log.content()
        record.log_path = log.path
        record.finished_at = timezone.now()
        record.save()
        rotate_deploy_logs()
        prune_records()
        return JsonResponse({'message': str(e), 'reason': e.reason,
                             'record_id': record.id}, status=500)
    record.version = project_version.version
    record.status = DeploymentRecord.SUCCESS
    record.rollback_reason = 'egg uploaded, no deployment'
    record.log = log.content()
    record.log_path = log.path
    record.finished_at = timezone.now()
    record.save()
    rotate_deploy_logs()
    prune_records()
    return JsonResponse({'result': '1', 'reused': not created,
                         'version': project_version.version,
                         'checksum': project_version.checksum,
                         'size': project_version.size, 'record_id': record.id})


@log_exception()
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def project_version_deploy(request, client_id, project_name, version):
    """
    deploy a specific immutable version with explicit retention and running-job
    policy. Body options:
      keep_versions: int (default 1, the historical single-version behaviour)
      running_policy: wait | continue | abort (default continue)
      expected_spiders: optional list used as an extra health assertion
      enforce_data_compatibility: bool (default true)
    """
    data = parse_json_body(request)
    keep_versions = int(data.get('keep_versions', 1))
    running_policy = data.get('running_policy', DEFAULT_RUNNING_POLICY)
    if running_policy not in (POLICY_WAIT, POLICY_CONTINUE, POLICY_ABORT):
        return JsonResponse({'message': 'invalid running_policy %s' % running_policy}, status=400)
    expected = data.get('expected_spiders')
    if expected is not None and not isinstance(expected, list):
        return JsonResponse({'message': 'expected_spiders must be a list'}, status=400)
    return _execute_deploy(
        request, client_id, project_name,
        keep_versions=max(keep_versions, 1),
        running_policy=running_policy,
        description=data.get('description'), version=version,
        expected_spiders=expected,
        enforce_data_compatibility=bool(data.get('enforce_data_compatibility', True)))


@log_exception()
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def project_version_rollback(request, client_id, project_name, version):
    """
    roll a client back to a previously built version. The target egg is
    re-uploaded with its original (immutable) version id, so the audit trail
    stays stable. Health checks run exactly like a forward deploy.
    """
    try:
        client = Client.objects.get(id=client_id)
    except Client.DoesNotExist:
        return JsonResponse({'message': 'client not found'}, status=404)
    record = DeploymentRecord.objects.create(
        client=client, client_name=client.name, project=project_name,
        status=DeploymentRecord.DEPLOYING, operator=current_operator(request))
    engine = DeploymentEngine(VersionStore())
    try:
        result = engine.rollback(client, project_name, version,
                                 operator=current_operator(request), record=record)
    except ConcurrentDeploymentError as e:
        record.refresh_from_db()
        return JsonResponse({'message': str(e), 'status': DeploymentRecord.REJECTED,
                             'record_id': record.id}, status=409)
    except DeploymentError as e:
        if record.status == DeploymentRecord.DEPLOYING:
            record.status = DeploymentRecord.FAILED
            record.reason = e.reason
            record.rollback_reason = truncate_text(sanitize_text(str(e)))
            record.finished_at = timezone.now()
            record.save()
        return JsonResponse({'message': str(e), 'reason': record.reason,
                             'record_id': record.id}, status=500)
    if result.status != DeploymentRecord.ROLLBACK:
        return JsonResponse(
            {'message': result.rollback_reason or 'rollback failed',
             'status': result.status, 'reason': result.reason,
             'record_id': result.id}, status=500)
    return JsonResponse({'result': '1', 'status': result.status,
                         'version': result.version, 'record_id': result.id})


@log_exception()
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def project_version_diff(request, project_name):
    """
    file-level diff between two immutable versions (?from=&to=)
    """
    from_version = request.GET.get('from')
    to_version = request.GET.get('to')
    if not from_version or not to_version:
        return JsonResponse({'message': 'both from and to query params are required'}, status=400)
    old_manifest = read_manifest(project_name, from_version)
    new_manifest = read_manifest(project_name, to_version)
    if not old_manifest or not new_manifest:
        return JsonResponse({'message': 'one or both versions were pruned or never built'},
                            status=404)
    return JsonResponse(diff_manifests(old_manifest, new_manifest))


@log_exception()
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def client_preflight(request, client_id):
    """
    standalone pre-flight health check of a scrapyd client (reachability plus
    the currently deployed versions)
    """
    try:
        client = Client.objects.get(id=client_id)
    except Client.DoesNotExist:
        return JsonResponse({'message': 'client not found'}, status=404)
    project_name = request.GET.get('project')
    checks = [check_daemon_health(client)]
    remote = []
    jobs = []
    if project_name:
        scrapyd = get_scrapyd(client)
        remote = existing_versions(scrapyd, project_name)
        jobs = get_running_jobs(scrapyd, project_name)
    return JsonResponse({'passed': all(c['passed'] for c in checks),
                         'checks': checks, 'versions': remote,
                         'running_jobs': len(jobs), 'jobs': jobs})


@log_exception()
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def deployment_audit(request):
    """
    audit query over deployment attempts. Filters: project, client_id, status,
    version, operator; newest first, paginated with limit/offset.
    """
    queryset = DeploymentRecord.objects.all().order_by('-id')
    filters = (('project', 'project'), ('client_id', 'client_id'), ('status', 'status'),
               ('version', 'version'), ('operator', 'operator'), ('reason', 'reason'))
    for param, field in filters:
        value = request.GET.get(param)
        if value:
            queryset = queryset.filter(**{field: value})
    try:
        limit = min(int(request.GET.get('limit', 50)), 500)
        offset = max(int(request.GET.get('offset', 0)), 0)
    except ValueError:
        return JsonResponse({'message': 'limit/offset must be integers'}, status=400)
    total = queryset.count()
    rows = list(queryset[offset:offset + limit])
    data = [{
        'id': row.id, 'client_id': row.client_id, 'client_name': row.client_name,
        'project': row.project, 'version': row.version,
        'previous_version': row.previous_version, 'status': row.status,
        'reason': row.reason, 'diff': json.loads(row.diff) if row.diff else None,
        'health_checks': json.loads(row.health_checks) if row.health_checks else None,
        'rollback_reason': row.rollback_reason,
        'running_task_policy': row.running_task_policy,
        'running_jobs': row.running_jobs, 'operator': row.operator,
        'log_path': row.log_path, 'started_at': row.started_at,
        'finished_at': row.finished_at, 'created_at': row.created_at,
    } for row in rows]
    return JsonResponse({'result': '1', 'total': total, 'limit': limit,
                         'offset': offset, 'data': data})


@log_exception()
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def deployment_audit_detail(request, record_id):
    """single audit record including the full (bounded, sanitized) log"""
    try:
        row = DeploymentRecord.objects.get(id=record_id)
    except DeploymentRecord.DoesNotExist:
        return JsonResponse({'message': 'record not found'}, status=404)
    data = model_to_dict(row)
    for key in ('diff', 'health_checks'):
        if data.get(key):
            try:
                data[key] = json.loads(data[key])
            except (ValueError, TypeError):
                pass
    return JsonResponse(data)


@log_exception()
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def render_html(request):
    """
    render html with url
    :param request:
    :return:
    """
    if request.method == 'GET':
        url = request.GET.get('url')
        url = unquote(base64.b64decode(url).decode('utf-8'))
        if not is_safe_url(url):
            return JsonResponse({'message': 'Invalid or forbidden URL'}, status=400)
        js = request.GET.get('js', 0)
        script = request.GET.get('script')
        response = requests.get(url, timeout=5)
        response.encoding = response.apparent_encoding
        html = process_html(response.text)
        return HttpResponse(html)
