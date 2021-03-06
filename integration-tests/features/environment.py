import json
import datetime
import subprocess
import os.path
import contextlib

from behave.log_capture import capture
import docker
import requests
import time
from elasticsearch import Elasticsearch


_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_DIR = os.path.dirname(os.path.dirname(_THIS_DIR))
def _make_compose_name(suffix='.yml'):
    return os.path.join(_REPO_DIR, 'docker-compose' + suffix)

def _set_default_compose_path(context):
    base_compose = _make_compose_name()
    # Extra containers are added as needed by integration setup commands
    context.docker_compose_path = [base_compose]

### make sure behave uses pytest improved asserts
# Behave runner uses behave.runner.exec_file function to read, compile
# and exec code of environment file and step files *in this order*.
# Therefore we provide a new implementation here, which uses pytest's
# _pytest.assertion.rewrite to rewrite the bytecode with pytest's
# improved asserts.
# This means that when behave tries to load steps, it will use our exec_file.
# => SUCCESS
# Don't ask how long it took me to figure this out.
import behave.runner


def exec_file(filename, globals=None, locals=None):
    if globals is None:
        globals = {}
    if locals is None:
        locals = globals
    locals['__file__'] = filename
    from py import path
    from _pytest import config
    from _pytest.assertion import rewrite
    f = path.local(filename)
    filename2 = os.path.relpath(filename, os.getcwd())
    config = config._prepareconfig([], [])
    _, code = rewrite._rewrite_test(config, f)
    exec(code, globals, locals)

behave.runner.exec_file = exec_file
### end this madness


def _make_compose_command(context, *args):
    cmd = ['docker-compose']
    for compose_file in context.docker_compose_path:
        cmd.append('-f')
        cmd.append(compose_file)
    cmd.extend(args)
    print(cmd)
    return cmd

def _start_system(context):
    if context.docker_compose_path:
        cmd = _make_compose_command(context, 'up', '--no-build', '-d')
    else:
        cmd = ['kubectl', 'create', '-f', context.kubernetes_dir_path]

    subprocess.check_output(cmd, stderr=subprocess.STDOUT)


def _make_compose_teardown_callback(context, services):
    cmds = []
    cmds.append(_make_compose_command(context, 'kill', *services))
    cmds.append(_make_compose_command(context, 'rm', '-fv', *services))

    def teardown_services():
        for cmd in cmds:
            subprocess.check_output(cmd, stderr=subprocess.STDOUT)
    return teardown_services

def _start_local_indexer(context):
    if context.docker_compose_path:
        es_indexer_compose = _make_compose_name('.es_indexer.yml')
        context.docker_compose_path.append(es_indexer_compose)
        services = ('elasticsearch', 'es_indexer')
        cmd = _make_compose_command(context, 'up', '--no-build', '-d')
        cleanup = _make_compose_teardown_callback(context, services)
        context.resource_manager.callback(cleanup)
    else:
        msg = 'Local ElasticSearch is not currently available under Kubernetes'
        context.scenario.skip(reason=msg)
        # Previous line throws an exception to skip the scenario

    subprocess.check_output(cmd, stderr=subprocess.STDOUT)

    es_hosts = [
        {
            'host': 'localhost',
            'port': '9200',
        },
    ]
    context.es_client = es_client = Elasticsearch(hosts=es_hosts,
                                                  timeout=60,
                                                  retry_on_timeout=True,
                                                  max_retries=5)
    retry_count = 3
    retry_interval = 1
    time.sleep(30) # Initial sleep to reduce noise in logs
    try:
        cluster_up = es_client.ping()
    except Exception:
        cluster_up = False
    while not cluster_up:
        # Give the cluster an opportunity to start
        retry_count -= 1
        time.sleep(retry_interval)
        try:
            cluster_up = es_client.ping()
        except Exception:
            cluster_up = False

    if not cluster_up:
        raise RuntimeError("Local ElasticSearch instance failed to start")


def _run_command_in_service(context, service, command):
    """
    run command in specified service via `docker-compose run`; command is list of strs
    """
    if context.docker_compose_path:
        cmd = _make_compose_command(context, 'run', '--rm', '-d', service)
        cmd.extend(command)
    else:
        raise Exception("not implemented")

    try:
        # universal_newlines decodes output on Python 3.x
        output = subprocess.check_output(cmd, universal_newlines=True).strip()
        print(output)
        return output
    except subprocess.CalledProcessError as ex:
        print(ex.output)
        raise


def _exec_command_in_container(client, container, command):
    """
    equiv of `docker exec`, command is str
    """
    exec_id = client.exec_create(container, command)
    output = client.exec_start(exec_id).decode('utf-8')
    print(output)
    return output


def _get_k8s_volumes_to_delete():
    # universal_newlines decodes output on Python 3.x
    out = subprocess.check_output(['kubectl', 'get', 'pods', '-o', 'json'], universal_newlines=True)
    j = json.loads(out)
    volumes = []
    for pod in j['items']:
        pod_vols = pod['spec'].get('volumes', [])
        for pod_vol in pod_vols:
            if 'hostPath' in pod_vol:
                volumes.append(pod_vol['hostPath']['path'])
    return volumes


def _dump_server_logs(context, tail=None):
    if context.docker_compose_path:
        cmd = _make_compose_command(context, 'logs')
        if tail is not None:
            cmd.append('--tail={:d}'.format(tail))
        subprocess.check_call(cmd, stderr=subprocess.STDOUT)
    else:
        pass # No current support for dumping logs under k8s


def _teardown_system(context):
    cmds = []
    if context.docker_compose_path:
        cmds.append(_make_compose_command(context, 'kill'))
        cmds.append(_make_compose_command(context, 'rm', '-fv'))
        if hasattr(context, "container"):
            cmds.append(['docker', "kill", context.container])
            cmds.append(['docker', "rm", "-fv", "--rm-all", context.container])
        _set_default_compose_path(context)
    else:
        cmds.append(['kubectl', 'delete', '--ignore-not-found', '-f', context.kubernetes_dir_path])
        volumes = _get_k8s_volumes_to_delete()
        for volume in volumes:
            # TODO: the sudo thing is not very nice, but...
            cmds.append(['sudo', 'rm', '-rf', volume])
            cmds.append(['sudo', 'mkdir', volume])

    for cmd in cmds:
        subprocess.check_output(cmd, stderr=subprocess.STDOUT)

def _wait_for_system(context, wait_for_server=60):
    start = datetime.datetime.now()
    wait_till = start + datetime.timedelta(seconds=wait_for_server)
    # try to wait for server to start for some time
    while datetime.datetime.now() < wait_till:
        time.sleep(1)
        started_all = False
        if context.kubernetes_dir_path:
            res = json.loads(subprocess.check_output(['kubectl', 'get', 'pods', '-o', 'json']))
            for pod in res['items']:
                status = pod.get('status', {})
                conditions = status.get('conditions', [])
                phase = status.get('phase', '')
                if status == {}:
                    continue
                if phase != 'Running':
                    continue
                for condition in conditions:
                    if condition['type'] == 'Ready' and condition['status'] != 'True':
                        continue
                # if we got here, then everything is running
                started_all = True
                break
        else:
            if _is_running(context):
                started_all = True
                break
    if started_all:
        # let's give the whole system a while to breathe
        time.sleep(float(context.config.userdata.get('breath_time', 5)))
    else:
        raise Exception('Server failed to start in under {s} seconds'.
                        format(s=wait_for_server))



def _restart_system(context, wait_for_server=60):
    try:
        _teardown_system(context)
        _start_system(context)
        _wait_for_system(context, wait_for_server)
    except subprocess.CalledProcessError as e:
        raise Exception('Failed to restart system. Command "{c}" failed:\n{o}'.
                        format(c=' '.join(e.cmd), o=e.output))


def _is_running(context):
    try:
        res = requests.get(context.coreapi_url + 'api/v1/analyses/')
        if res.status_code == 200:
            return True
    except requests.exceptions.ConnectionError:
        pass
    return False

def _read_boolean_setting(context, setting_name):
    setting = context.config.userdata.get(setting_name, '').lower()
    if setting in ('1', 'yes', 'true', 'on'):
        return True
    if setting in ('', '0', 'no', 'false', 'off'):
        return False
    msg = '{!r} is not a valid option for boolean setting {!r}'
    raise ValueError(msg.format(setting, setting_name))

def _add_slash(url):
    if not url.endswith('/'):
        url += '/'
    return url

def before_all(context):
    context.config.setup_logging()
    context.start_system = _start_system
    context.start_local_indexer = _start_local_indexer
    context.teardown_system = _teardown_system
    context.restart_system = _restart_system
    context.run_command_in_service = _run_command_in_service
    context.exec_command_in_container = _exec_command_in_container
    context.is_running = _is_running

    # Configure container logging
    context.dump_logs = _read_boolean_setting(context, 'dump_logs')
    tail_logs = int(context.config.userdata.get('tail_logs', 0))
    dump_errors = _read_boolean_setting(context, 'dump_errors')
    if tail_logs:
        dump_errors = True
    else:
        tail_logs = 50
    context.dump_errors = dump_errors
    context.tail_logs = tail_logs


    # Configure system under test
    context.kubernetes_dir_path = context.config.userdata.get('kubernetes_dir', None)
    if context.kubernetes_dir_path is not None:
        context.docker_compose_path = None
    else:
        # If we're not running Kubernetes, use the local Docker Compose setup
        _set_default_compose_path(context)
    # for now, we just assume we know what compose file looks like (what services need what images)
    context.images = {}
    context.images['bayesian/bayesian-api'] = context.config.userdata.get(
        'coreapi_server_image',
        'docker-registry.usersys.redhat.com/bayesian/bayesian-api')
    context.images['bayesian/cucos-worker'] = context.config.userdata.get(
        'coreapi_worker_image',
        'docker-registry.usersys.redhat.com/bayesian/cucos-worker')
    
    context.coreapi_url = _add_slash(context.config.userdata.get('coreapi_url',
        'http://localhost:32000/'))
    context.anitya_url = _add_slash(context.config.userdata.get('anitya_url',
        'http://localhost:31005/'))
    
    context.client = docker.AutoVersionClient()

    for desired, actual in context.images.items():
        desired = 'docker-registry.usersys.redhat.com/' + desired
        if desired != actual:
            context.client.tag(actual, desired, force=True)

    # Specify the analyses checked for when looking for "complete" results
    def _get_expected_component_analyses(ecosystem):
        common = context.EXPECTED_COMPONENT_ANALYSES
        specific = context.ECOSYSTEM_DEPENDENT_ANALYSES.get(ecosystem, set())
        return common | specific
    context.get_expected_component_analyses = _get_expected_component_analyses

    def _compare_analysis_sets(actual, expected):
        unreliable = context.UNRELIABLE_ANALYSES
        missing = expected - actual - unreliable
        unexpected = actual - expected - unreliable
        return missing, unexpected
    context.compare_analysis_sets = _compare_analysis_sets

    context.EXPECTED_COMPONENT_ANALYSES = {
        'metadata', 'source_licenses',
        'digests', 'redhat_downstream',
        'dependency_snapshot', 'code_metrics'
        # The follower workers are currently disabled by default:
        # 'static_analysis', 'binary_data', 'languages', 'crypto_algorithms'
    }
    # Analyses that are only executed for particular language ecosystems
    context.ECOSYSTEM_DEPENDENT_ANALYSES = {
        "maven": {'blackduck'},
        "npm": {'blackduck'},
    }
    # Results that use a nonstandard format, so we don't check for the
    # standard "status", "summary", and "details" keys
    context.NONSTANDARD_ANALYSIS_FORMATS = set()
    # Analyses that are just plain unreliable and so need to be excluded from
    # consideration when determining whether or not an analysis is complete
    context.UNRELIABLE_ANALYSES = {
        'blackduck',
        'github_details',  # if no github api token provided
        'security_issues'  # needs Snyk vulndb in S3
    }


@capture
def before_scenario(context, scenario):
    context.resource_manager = contextlib.ExitStack()

@capture
def after_scenario(context, scenario):
    if context.dump_logs or context.dump_errors and scenario.status == "failed":
        try:
            _dump_server_logs(context, int(context.tail_logs))
        except subprocess.CalledProcessError as e:
            raise Exception('Failed to dump server logs. Command "{c}" failed:\n{o}'.
                            format(c=' '.join(e.cmd), o=e.output))

    # Clean up resources (which may destroy some container logs)
    context.resource_manager.close()

@capture
def after_all(context):
    try:
        _teardown_system(context)
    except subprocess.CalledProcessError as e:
        raise Exception('Failed to teardown system. Command "{c}" failed:\n{o}'.
                        format(c=' '.join(e.cmd), o=e.output))
