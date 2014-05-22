# -*- coding: utf-8 -*-

#    Copyright (C) 2012 Yahoo! Inc. All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import threading

from taskflow.engines.action_engine import executor
from taskflow.engines.action_engine import graph_action
from taskflow.engines.action_engine import graph_analyzer
from taskflow.engines.action_engine import retry_action
from taskflow.engines.action_engine import task_action
from taskflow.engines import base

from taskflow import exceptions as exc
from taskflow.openstack.common import excutils
from taskflow import retry
from taskflow import states
from taskflow import storage as t_storage

from taskflow.utils import flow_utils
from taskflow.utils import lock_utils
from taskflow.utils import misc
from taskflow.utils import reflection


class ActionEngine(base.EngineBase):
    """Generic action-based engine.

    This engine flattens the flow (and any subflows) into a execution graph
    which contains the full runtime definition to be executed and then uses
    this graph in combination with the action classes & storage to attempt to
    run your flow (and any subflows & contained tasks) to completion.

    During this process it is permissible and valid to have a task or multiple
    tasks in the execution graph fail, which will cause the process of
    reversion to commence. See the valid states in the states module to learn
    more about what other states the tasks & flow being ran can go through.
    """
    _graph_action_factory = graph_action.FutureGraphAction
    _graph_analyzer_factory = graph_analyzer.GraphAnalyzer
    _task_action_factory = task_action.TaskAction
    _task_executor_factory = executor.SerialTaskExecutor
    _retry_action_factory = retry_action.RetryAction

    def __init__(self, flow, flow_detail, backend, conf):
        super(ActionEngine, self).__init__(flow, flow_detail, backend, conf)
        self._analyzer = None
        self._root = None
        self._compiled = False
        self._lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._storage_ensured = False

    def __str__(self):
        return "%s: %s" % (reflection.get_class_name(self), id(self))

    def suspend(self):
        if not self._compiled:
            raise exc.InvalidState("Can not suspend an engine"
                                   " which has not been compiled")
        self._change_state(states.SUSPENDING)

    @property
    def execution_graph(self):
        """The graph of nodes to be executed.

        NOTE(harlowja): Only accessible after compilation has completed.
        """
        g = None
        if self._compiled and self._analyzer:
            g = self._analyzer.execution_graph
        return g

    def run(self):
        with lock_utils.try_lock(self._lock) as was_locked:
            if not was_locked:
                raise exc.ExecutionFailure("Engine currently locked, please"
                                           " try again later")
            for _state in self.run_iter():
                pass

    def run_iter(self, timeout=None):
        """Runs the engine using iteration (or die trying).

        :param timeout: timeout to wait for any tasks to complete (this timeout
            will be used during the waiting period that occurs after the
            waiting state is yielded when unfinished tasks are being waited
            for).

        Instead of running to completion in a blocking manner, this will
        return a generator which will yield back the various states that the
        engine is going through (and can be used to run multiple engines at
        once using a generator per engine). the iterator returned also
        responds to the send() method from pep-0342 and will attempt to suspend
        itself if a truthy value is sent in (the suspend may be delayed until
        all active tasks have finished).

        NOTE(harlowja): using the run_iter method will **not** retain the
        engine lock while executing so the user should ensure that there is
        only one entity using a returned engine iterator (one per engine) at a
        given time.
        """
        self.compile()
        self.prepare()
        self._task_executor.start()
        state = None
        try:
            self._change_state(states.RUNNING)
            for state in self._root.execute_iter(timeout=timeout):
                try:
                    try_suspend = yield state
                except GeneratorExit:
                    break
                else:
                    if try_suspend:
                        self.suspend()
        except Exception:
            with excutils.save_and_reraise_exception():
                self._change_state(states.FAILURE)
        else:
            ignorable_states = getattr(self._root, 'ignorable_states', [])
            if state and state not in ignorable_states:
                self._change_state(state)
                if state != states.SUSPENDED and state != states.SUCCESS:
                    failures = self.storage.get_failures()
                    misc.Failure.reraise_if_any(failures.values())
        finally:
            self._task_executor.stop()

    def _change_state(self, state):
        with self._state_lock:
            old_state = self.storage.get_flow_state()
            if not states.check_flow_transition(old_state, state):
                return
            self.storage.set_flow_state(state)
        try:
            flow_uuid = self._flow.uuid
        except AttributeError:
            # NOTE(harlowja): if the flow was just a single task, then it
            # will not itself have a uuid, but the constructed flow_detail
            # will.
            if self._flow_detail is not None:
                flow_uuid = self._flow_detail.uuid
            else:
                flow_uuid = None
        details = dict(engine=self,
                       flow_name=self._flow.name,
                       flow_uuid=flow_uuid,
                       old_state=old_state)
        self.notifier.notify(state, details)

    def _ensure_storage_for(self, execution_graph):
        # NOTE(harlowja): signal to the tasks that exist that we are about to
        # resume, if they have a previous state, they will now transition to
        # a resuming state (and then to suspended).
        self._change_state(states.RESUMING)  # does nothing in PENDING state
        for node in execution_graph.nodes_iter():
            version = misc.get_version_string(node)
            if isinstance(node, retry.Retry):
                self.storage.ensure_retry(node.name, version, node.save_as)
            else:
                self.storage.ensure_task(node.name, version, node.save_as)
            if node.inject:
                self.storage.inject_task_args(node.name, node.inject)

        self._change_state(states.SUSPENDED)  # does nothing in PENDING state

    @lock_utils.locked
    def prepare(self):
        if not self._compiled:
            raise exc.InvalidState("Can not prepare an engine"
                                   " which has not been compiled")
        if not self._storage_ensured:
            self._ensure_storage_for(self.execution_graph)
            self._storage_ensured = True
        # At this point we can check to ensure all dependencies are either
        # flow/task provided or storage provided, if there are still missing
        # dependencies then this flow will fail at runtime (which we can avoid
        # by failing at preparation time).
        external_provides = set(self.storage.fetch_all().keys())
        missing = self._flow.requires - external_provides
        if missing:
            raise exc.MissingDependencies(self._flow, sorted(missing))
        # Reset everything back to pending (if we were previously reverted).
        if self.storage.get_flow_state() == states.REVERTED:
            self._root.reset_all()
            self._change_state(states.PENDING)

    @misc.cachedproperty
    def _retry_action(self):
        return self._retry_action_factory(self.storage, self.task_notifier)

    @misc.cachedproperty
    def _task_executor(self):
        return self._task_executor_factory()

    @misc.cachedproperty
    def _task_action(self):
        return self._task_action_factory(self.storage, self._task_executor,
                                         self.task_notifier)

    @lock_utils.locked
    def compile(self):
        if self._compiled:
            return
        execution_graph = flow_utils.flatten(self._flow)
        if execution_graph.number_of_nodes() == 0:
            raise exc.Empty("Flow %s is empty." % self._flow.name)
        self._analyzer = self._graph_analyzer_factory(execution_graph,
                                                      self.storage)
        self._root = self._graph_action_factory(self._analyzer,
                                                self.storage,
                                                self._task_action,
                                                self._retry_action)
        self._compiled = True
        return


class SingleThreadedActionEngine(ActionEngine):
    """Engine that runs tasks in serial manner."""
    _storage_factory = t_storage.SingleThreadedStorage


class MultiThreadedActionEngine(ActionEngine):
    """Engine that runs tasks in parallel manner."""
    _storage_factory = t_storage.MultiThreadedStorage

    def _task_executor_factory(self):
        return executor.ParallelTaskExecutor(self._executor)

    def __init__(self, flow, flow_detail, backend, conf, **kwargs):
        super(MultiThreadedActionEngine, self).__init__(
            flow, flow_detail, backend, conf)
        self._executor = kwargs.get('executor')
