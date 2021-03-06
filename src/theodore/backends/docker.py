import os
import time
import json
import glob
import yaml
import docker
import shutil
import tempfile
import hashlib
import uuid
import copy

from base64 import b64decode, b64encode

from ..utils import string_types
from ..scheduler import Schedule
from .backend import Backend, Result, FileResult

from tornado import httpclient


class Docker(Backend):

    tag = 'nightly'

    def __init__(self, scheduler):
        self.client = docker.from_env()
        try:
            self.client.ping()
        except docker.errors.APIError:
            raise "Could not connect to Docker"
        self.scheduler = scheduler

    def schedule(self, pipeline, data_config):
        self.scheduler.schedule(DockerSchedule(self, pipeline, data_config))


class DockerRun(object):

    def __init__(self, container):
        self.container = container

    @property
    def status(self):
        try:
            self.container.reload()
        except Exception as e:
            return 'stopped'
        status = self.container.status
        status_map = {
            'created': 'starting',
            'restarting': 'running',
            'running': 'running',
            'removing': 'running',
            'paused': 'running',
            'exited': 'success',
            'dead': 'failed'
        }
        if status in status_map:
            return status_map[status]

        return 'unknown'


class DockerSchedule(Schedule):

    def __init__(self, backend, pipeline=None, data_config=None, parent=None):
        super(DockerSchedule, self).__init__(backend=backend, parent=parent)
        self.pipeline = pipeline
        self.data_config = data_config
        self._uid = str(uuid.uuid4())
        self._results = {}

    @property
    def uid(self):
        return self._uid

    def __repr__(self):
        return self._uid

    def __hash__(self):
        return hash(str(self))

    @property
    def logs(self):
        return [{
            'id': 'schedule',
            'hash': 'schedule',
        }]

    @property
    def available_results(self):
        return list(self._results.keys())

    @property
    def results(self):
        return self._results

    def result(self, key):
        r = self._results
        keys = key.split('/')
        for k in keys:
            if type(k) == dict:
                r = r[k]
            elif type(k) == list:
                r = r[int(k)]
        return r

    def run(self):
        if self.data_config:
            yield (
                'data_config',
                DockerDataConfigSchedule(
                    self.backend,
                    self.pipeline,
                    self.data_config,
                    parent=self
                )
            )


class DockerSubjectSchedule(DockerSchedule):

    def __init__(self, backend, pipeline, subject, parent=None):
        super(DockerSubjectSchedule, self).__init__(backend=backend, parent=parent)
        self.pipeline = pipeline
        self.subject = subject
        self._run = None

    @staticmethod
    def _remap_files(subject):
        mapping = {}
        subject = copy.deepcopy(subject)

        if isinstance(subject, string_types):
            if '/' not in subject:
                return subject, mapping

            if subject.startswith('s3://'):
                return subject, mapping
            else:
                subject = os.path.abspath(os.path.realpath(subject))
                md5 = hashlib.md5()
                md5.update(os.path.dirname(subject).encode())
                mapping[os.path.dirname(subject)] = '/' + md5.hexdigest()
                return '/' + md5.hexdigest() + '/' + os.path.basename(subject), mapping

        elif isinstance(subject, dict):
            for key, val in subject.items():
                subject[key], submapping = DockerSubjectSchedule._remap_files(val)
                mapping.update(submapping)
            return subject, mapping

        elif isinstance(subject, list):
            for key, val in enumerate(subject):
                subject[key], submapping = DockerSubjectSchedule._remap_files(val)
                mapping.update(submapping)
            return subject, mapping

    def run(self):
        config_folder = tempfile.mkdtemp()
        output_folder = tempfile.mkdtemp()

        if self.pipeline is not None:
            new_pipeline = os.path.join(config_folder, 'pipeline.yml')
            shutil.copy(self.pipeline, new_pipeline)
            pipeline = new_pipeline

        volumes = {
            '/tmp': {'bind': '/scratch', 'mode': 'rw'},
            config_folder: {'bind': '/config', 'mode':'ro'},
            output_folder: {'bind': '/output', 'mode':'rw'},
        }

        subject = 'data:text/plain;base64,' + \
            b64encode(yaml.dump([self.subject], default_flow_style=False).encode("utf-8")).decode("utf-8")

        # TODO handle local databases, transverse subject dict to get folder mappings
        command = [
            '/', '/output', 'participant',
            '--monitoring',
            '--skip_bids_validator',
            '--save_working_dir',
            '--data_config_file',
            subject
        ]

        if self.pipeline:
            command += ['--pipeline_file', '/config/pipeline.yml']

        self._run = DockerRun(self.backend.client.containers.run(
            'fcpindi/c-pac:' + self.backend.tag,
            command=command,
            detach=True,
            ports={'8080/tcp': None},
            volumes=volumes,
            working_dir='/pwd'
        ))

        self._run.container.wait()

    @property
    def status(self):
        if not self._run:
            return "unstarted"
        else:
            return self._run.status

    @property
    def logs(self):

        if not self._run:
            return []

        try:
            self._run.container.reload()
        except Exception as e:
            return []

        if '8080/tcp' not in self._run.container.attrs['NetworkSettings']['Ports']:
            return []

        port = int(self._run.container.attrs['NetworkSettings']['Ports']['8080/tcp'][0]['HostPort'])

        http_client = httpclient.HTTPClient()

        try:
            response = json.loads(http_client.fetch("http://localhost:%d/" % port).body.decode('utf-8'))
        except Exception as e:
            print(e)
        http_client.close()

        return []


class DockerDataConfigSchedule(DockerSchedule):

    _start = None
    _finish = None

    def __init__(self, backend, pipeline, data_config, parent=None):
        super(DockerDataConfigSchedule, self).__init__(backend=backend, parent=parent)
        self.pipeline = pipeline
        self.data_config = data_config
        self._run = None

    def run(self):

        self._start = time.time()

        self._output_folder = tempfile.mkdtemp()

        volumes = {
            self._output_folder: {'bind': '/output_folder', 'mode': 'rw'},
            '/tmp': {'bind': '/scratch', 'mode': 'rw'},
        }

        data_config = None
        data_folder = '/'
        if "\n" in self.data_config:
            data_config = self.data_config
        else:
            data_folder = self.data_config

        if data_folder and not data_folder.startswith('s3://'):
            volumes[data_folder] = {'bind': '/data_folder', 'mode': 'ro'}
            data_folder = '/data_folder'

        container_args = [data_folder, '/output_folder', 'test_config']
        if data_config:
            if data_config.lower().startswith('data:'):
                container_args += ['--data_config_file', data_config]
            else:
                container_args += ['--data_config_file', os.path.basename(data_config)]
                volumes[os.path.dirname(data_config)] = {'bind': '/data_config_file', 'mode': 'ro'}

        self._run = DockerRun(self.backend.client.containers.run(
            'fcpindi/c-pac:' + self.backend.tag,
            command=container_args,
            detach=True,
            volumes=volumes
        ))

        self._run.container.wait()

        try:
            files = glob.glob(os.path.join(self._output_folder, 'cpac_data_config_*.yml'))
            if files:
                with open(files[0]) as f:
                    for subject in yaml.load(f):
                        subject_id = []
                        if 'site_id' in subject:
                            subject_id += [subject['site_id']]
                        if 'subject_id' in subject:
                            subject_id += [subject['subject_id']]
                        if 'unique_id' in subject:
                            subject_id += [subject['unique_id']]

                        yield (
                            '/'.join(subject_id),
                            DockerSubjectSchedule(self.backend, self.pipeline, subject, parent=self)
                        )
        finally:
            shutil.rmtree(self._output_folder)

        self._finish = time.time()

    @property
    def status(self):
        if not self._run:
            return "unstarted"
        else:
            return self._run.status

    @property
    def logs(self):
        log = {
            'id': 'data_config',
            'hash': 'data_config',
        }

        if self._start is not None:
            log['start'] = self._start

        if self._finish is not None:
            log['finish'] = self._finish

        return [log]


class DockerDataSettingsSchedule(DockerSchedule):

    _start = None
    _finish = None

    def __init__(self, backend, data_settings, parent=None):
        super(DockerDataSettingsSchedule, self).__init__(backend=backend, parent=parent)
        self.data_settings = data_settings
        self._run = None

    def run(self):
        self._start = time.time()
        self._output_folder = tempfile.mkdtemp(prefix='theo')

        volumes = {
            self._output_folder: {'bind': '/output_folder', 'mode': 'rw'},
            '/tmp': {'bind': '/scratch', 'mode': 'rw'},
        }

        data_settings = self.data_settings
        shutil.copy(data_settings, os.path.join(self._output_folder, 'data_settings.yml'))

        container_args = [
            '/',
            '/output_folder',
            'cli',
            'utils',
            'data_config',
            'build',
            '/output_folder/data_settings.yml',
        ]

        self._run = DockerRun(self.backend.client.containers.run(
            'fcpindi/c-pac:' + self.backend.tag,
            command=container_args,
            detach=True,
            working_dir='/output_folder',
            volumes=volumes
        ))

        self._run.container.wait()
        
        self._results['data_config'] = FileResult(
            'data_config',
            glob.glob(os.path.join(self._output_folder, 'data_config*.yml'))[0],
            'application/yaml'
        )

        self._finish = time.time()

    @property
    def status(self):
        if not self._run:
            return "unstarted"
        else:
            return self._run.status

    @property
    def logs(self):
        log = {
            'id': 'data_config',
            'hash': 'data_config',
        }

        if self._start is not None:
            log['start'] = self._start

        if self._finish is not None:
            log['finish'] = self._finish

        return [log]
