#!/usr/bin/env python
# encoding: utf-8

# Copyright (C) Alibaba Cloud Computing
# All rights reserved.


import os
import os.path as op
import time
import json
import uuid
import signal
import logging
import traceback
from concurrent.futures import as_completed, ProcessPoolExecutor
from elasticsearch import Elasticsearch
from aliyun.log.logclient import LogClient, LogException
from aliyun.log.util import PrefixLoggerAdapter
from .migration_log import setup_logging
from .migration_task import MigrationTask, MigrationLogstore, Checkpoint
from .index_logstore_mappings import IndexLogstoreMappings
from .mapping_index_converter import MappingIndexConverter


_logger = logging.getLogger(__name__)


class MigrationConfig(object):
    """
    Configuration for migrating data from elasticsearch to aliyun log service (SLS)

    :type cache_path: string
    :param cache_path: file path to store migration cache, which used for resuming migration process when stopped. Please ensure it's clean for new migration task.

    :type hosts: string
    :param hosts: a comma-separated list of source ES nodes. e.g. "localhost:9200,other_host:9200"

    :type project_name: string
    :param project_name: specify the project_name of your log services. e.g. "your_project"

    :type indexes: string
    :param indexes: a comma-separated list of source index names. e.g. "index1,index2"

    :type query: string
    :param query: used to filter docs, so that you can specify the docs you want to migrate. e.g. '{"query": {"match": {"title": "python"}}}'

    :type logstore_index_mappings: string
    :param logstore_index_mappings: specify the mappings of log service logstore and ES index. e.g. '{"logstore1": "my_index*", "logstore2": "index1,index2"}}'

    :type pool_size: int
    :param pool_size: specify the size of migration task process pool. Default is 10 if not set.

    :type time_reference: string
    :param time_reference: specify what ES doc's field to use as log's time field. e.g. "field1"

    :type source: string
    :param source: specify the value of log's source field. e.g. "your_source"

    :type topic: string
    :param topic: specify the value of log's topic field. e.g. "your_topic"

    :type batch_size: int
    :param batch_size: max number of logs written into SLS in a batch. SLS requires that it's no bigger than 512KB in size and 1024 lines in one batch. Default is 1000 if not set.

    :type wait_time_in_secs: int
    :param wait_time_in_secs: specify the waiting time between initialize aliyun log and executing data migration task. Default is 60 if not set.

    :type auto_creation: bool
    :param auto_creation: specify whether to let the tool create logstore and index automatically for you. e.g. True

    """

    default_pool_size = 10
    default_wait_time = 60
    default_batch_size = 1000

    def __init__(
            self,
            cache_path,
            endpoint=None,
            access_key_id=None,
            access_key=None,
            project_name=None,
            hosts=None,
            indexes=None,
            query=None,
            time_reference=None,
            logstore_index_mappings=None,
            source=None,
            topic=None,
            pool_size=None,
            batch_size=None,
            wait_time_in_secs=None,
            auto_creation=True,
    ):
        self.cache_path = cache_path
        self.access_key_id, self.access_key = access_key_id, access_key

        self.ckpt_path = op.join(cache_path, 'ckpt')
        os.makedirs(self.ckpt_path, exist_ok=True)
        self._config_file = op.join(self.cache_path, 'config.json')
        self._valid = True
        if self._load_cache():
            cont = {
                'endpoint': endpoint,
                'project_name': project_name,
                'hosts': hosts,
                'indexes': indexes,
                'query': query,
                'logstore_index_mappings': logstore_index_mappings,
                'source': source,
                'topic': topic,
                'pool_size': pool_size,
                'batch_size': batch_size,
                'wait_time_in_secs': wait_time_in_secs,
                'auto_creation': auto_creation,
            }
            self._cont.update({k: v for k, v in cont.items() if v is not None})
        else:
            if pool_size is None:
                pool_size = self.default_pool_size
            if batch_size is None:
                batch_size = self.default_batch_size
            if wait_time_in_secs is None:
                wait_time_in_secs = self.default_wait_time
            self._cont = {
                'endpoint': endpoint,
                'project_name': project_name,
                'hosts': hosts,
                'indexes': indexes,
                'query': query,
                'time_reference': time_reference,
                'logstore_index_mappings': logstore_index_mappings,
                'source': source,
                'topic': topic,
                'pool_size': pool_size,
                'batch_size': batch_size,
                'wait_time_in_secs': wait_time_in_secs,
                'auto_creation': auto_creation,
            }
        self._dump_cache()

    @property
    def valid(self):
        return self._valid

    def get(self, name):
        return self._cont.get(name)

    def _load_cache(self):
        cached = False
        try:
            with open(self._config_file) as f:
                cont = f.read()
        except FileNotFoundError:
            cont = ''

        if len(cont) > 0:
            try:
                self._cont = json.loads(cont)
                cached = True
            except json.JSONDecodeError:
                raise Exception('Invalid migration configuration cache')
        return cached

    def _dump_cache(self):
        with open(self._config_file, 'w') as f:
            f.write(json.dumps(self._cont, indent=2))


class MigrationManager(object):
    def __init__(self, config: MigrationConfig):
        self._config = config
        _uuid = str(uuid.uuid4())
        self._id = ''.join(_uuid.split('-'))
        self._es = Elasticsearch(
            hosts=self._config.get('hosts'),
            verify_certs=False,
        )
        self._log_client = LogClient(
            endpoint=self._config.get('endpoint'),
            accessKeyId=self._config.access_key_id,
            accessKey=self._config.access_key,
        )
        setup_logging(
            self._id,
            self._config.get('endpoint'),
            self._config.get('project_name'),
            self._config.access_key_id,
            self._config.access_key,
        )
        self._logger = logging.getLogger(__name__)
        self._shutdown_flag = op.join(self._config.cache_path, 'shutdown.lock')
        print('#migration: {}'.format(self._id))

    def migrate(self):
        self._logger.info('Migration starts')
        tasks = self._discover_tasks()
        task_cnt = len(tasks)
        pool_size = min(self._config.get('pool_size'), task_cnt)
        print('#pool_size: {}'.format(pool_size))
        print('#tasks: {}'.format(task_cnt))

        self._prepare()
        futures = []
        state = {
            'total': task_cnt,
            Checkpoint.finished: 0,
            Checkpoint.dropped: 0,
            Checkpoint.failed: 0,
        }
        with ProcessPoolExecutor(max_workers=pool_size) as pool:
            for task in tasks:
                futures.append(
                    pool.submit(
                        _migration_worker,
                        self._config,
                        task,
                        self._shutdown_flag,
                    )
                )
            try:
                for future in as_completed(futures):
                    res = future.result()
                    if res in state:
                        state[res] += 1
                    self._logger.info('State', extra=state)
                    print('>> state:', json.dumps(state))
            except BaseException:
                self._logger.error(
                    'Exception',
                    extra={'traceback': traceback.format_exc()},
                )
                for future in futures:
                    if not future.done():
                        future.cancel()
                list(as_completed(futures, timeout=10))

        if state[Checkpoint.finished] + state[Checkpoint.dropped] >= task_cnt:
            self._logger.info('All migration tasks finished')
        self._logger.info('Migration exits')
        print('exit:', json.dumps(state))
        return state

    def _prepare(self):
        if op.exists(self._shutdown_flag):
            os.unlink(self._shutdown_flag)

        def _handle_term_sig(signum, frame):
            # Raise Ctrl+C
            with open(self._shutdown_flag, 'w') as f:
                f.write('')
            raise KeyboardInterrupt()

        signal.signal(signal.SIGINT, _handle_term_sig)
        signal.signal(signal.SIGTERM, _handle_term_sig)

    def _discover_tasks(self):
        indexes = self._config.get('indexes')
        data = self._es.search_shards(indexes)
        tasks = []
        for shard in data['shards']:
            for item in shard:
                # Ignore internal index
                if not indexes and item['index'].startswith('.'):
                    continue
                if item['state'] == 'STARTED' and item['primary']:
                    tasks.append(
                        {'es_index': item['index'], 'es_shard': item['shard']},
                    )
        return self._handle_cache(tasks)

    def _handle_cache(self, tasks):
        file_tasks = op.join(self._config.cache_path, 'tasks.json')
        if op.exists(file_tasks):
            with open(file_tasks) as f:
                cont = f.read()
        else:
            cont = '[]'

        try:
            old_tasks = json.loads(cont)
        except json.JSONDecodeError:
            self._logger.error('Invalid task cache', extra={'cache': cont})
            old_tasks = []

        task_map = {
            (task['es_index'], task['es_shard']): task['id']
            for task in old_tasks
        }
        _mappings = IndexLogstoreMappings(
            list([task['es_index'] for task in tasks]),
            self._config.get('logstore_index_mappings'),
        )
        cnt, new_tasks = len(old_tasks), []
        for task in tasks:
            _task = (task['es_index'], task['es_shard'])
            if _task not in task_map:
                task['id'] = cnt
                task['logstore'] = _mappings.get_logstore(task['es_index'])
                new_tasks.append(task)
                cnt += 1
        tasks = old_tasks + new_tasks

        with open(file_tasks, 'w') as f:
            f.write(json.dumps(tasks, indent=2))

        if self._config.get('auto_creation'):
            self._setup_aliyun_log(_mappings)
        return tasks

    def _setup_aliyun_log(self, index_logstore_mappings):
        print('setup aliyun log service...')
        self._logger.info('Setup AliyunLog start')
        logstores = index_logstore_mappings.get_all_logstores()
        for logstore in logstores:
            self._logger.info('Setup AliyunLog', extra={'logstore': logstore})
            self._setup_logstore(index_logstore_mappings, logstore)
        self._logger.info('Init AliyunLog wait')
        time.sleep(self._config.get('wait_time_in_secs'))
        self._logger.info('Init AliyunLog finish')

    def _setup_logstore(self, index_logstore_mappings, logstore):
        try:
            self._log_client.create_logstore(
                project_name=self._config.get('project_name'),
                logstore_name=logstore,
                ttl=3650,
            )
        except LogException as exc:
            if exc.get_error_code() == 'LogStoreAlreadyExist':
                self._logger.info(
                    'Logstore already exist, skip creation.',
                    extra={'logstore': logstore},
                )
            else:
                raise
        self._setup_index(index_logstore_mappings, logstore)

    def _setup_index(self, index_logstore_mappings, logstore):
        indexes = index_logstore_mappings.get_indexes(logstore)
        for index in indexes:
            try:
                resp = self._es.indices.get(index=index)
            except FileNotFoundError:
                self._logger.error('Index not found', extra={'es_index': index})
                continue
            mappings = resp[index]['mappings']
            index_config = MappingIndexConverter.to_index_config(mappings)
            try:
                self._log_client.create_index(
                    self._config.get('project_name'),
                    logstore,
                    index_config,
                )
            except LogException as exc:
                if exc.get_error_code() == 'IndexAlreadyExist':
                    self._log_client.update_index(
                        self._config.get('project_name'),
                        logstore,
                        index_config,
                    )
                    continue
                raise


def _migration_worker(config: MigrationConfig, task, shutdown_flag):
    if op.exists(shutdown_flag):
        # Already interrupted
        return Checkpoint.interrupted

    extra = {
        'task_id': task['id'],
        'es_index': task['es_index'],
        'es_shard': task['es_shard'],
        'logstore': task['logstore'],
    }
    print('migrate:', json.dumps(extra))
    logger = PrefixLoggerAdapter('', extra, _logger, {})
    logger.info('Migration worker starts')
    try:
        logstore = MigrationLogstore(
            endpoint=config.get('endpoint'),
            access_id=config.access_key_id,
            access_key=config.access_key,
            project_name=config.get('project_name'),
            logstore_name=task['logstore'],
            topic=config.get('topic'),
            source=config.get('source'),
        )
        task = MigrationTask(
            _id=task['id'],
            es_client=Elasticsearch(config.get('hosts')),
            es_index=task['es_index'],
            es_shard=task['es_shard'],
            logstore=logstore,
            ckpt_path=config.ckpt_path,
            time_reference=config.get('time_reference'),
            batch_size=config.get('batch_size'),
            logger=logger,
        )
        return task.run()
    except BaseException:
        logger.error(
            'Exception in migration worker',
            extra=traceback.format_exc(),
        )
        raise
    finally:
        logger.info('Migration worker exits')
