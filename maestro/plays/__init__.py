# Copyright (C) 2013 SignalFuse, Inc.
#
# Docker container orchestration utility.

from __future__ import print_function

import functools
import threading
import sys

from . import tasks
from .. import termoutput
from ..termoutput import color, red, up


class BaseOrchestrationPlay:
    """Base class for orchestration plays.

    Orchestration plays automatically parallelize the orchestration action
    while respecting the dependencies between the containers and the dependency
    order direction.
    """

    HEADER_FMT = '{:>3s}  {:<20s} {:<15s} {:<20s} ' + \
                 tasks.TASK_RESULT_HEADER_FMT
    HEADERS = ['  #', 'INSTANCE', 'SERVICE', 'SHIP', 'CONTAINER', 'STATUS']

    def __init__(self, containers=[], forward=True, respect_dependencies=True):
        self._containers = containers
        self._forward = forward

        self._dependencies = dict(
            (c.name, respect_dependencies and
                self._gather_dependencies(c) or set())
            for c in containers)

        self._om = termoutput.OutputManager(len(containers))
        self._threads = set([])
        self._done = set([])
        self._error = None
        self._cv = threading.Condition()

    def start(self):
        """Start the orchestration play."""
        print(BaseOrchestrationPlay.HEADER_FMT
              .format(*BaseOrchestrationPlay.HEADERS))
        self._om.start()

    def register(self, task):
        """Register an orchestration action for a given container.

        The action is automatically wrapped into a layer that limits the
        concurrency to enforce the dependency order of the orchestration play.
        The action is only performed once the action has been performed for all
        the dependencies (or dependents, depending on the forward parameter).

        Args:
            task (tasks.Task): the task to execute.
        """
        def act(task):
            task.o.pending('waiting...')

            # Wait until we can be released (or if an error occurred for
            # another container).
            self._cv.acquire()
            while not self._satisfied(task.container) and not self._error:
                self._cv.wait(1)
            self._cv.release()

            # Abort if needed
            if self._error:
                task.o.commit(red('aborted!'))
                return

            try:
                task.run()
                self._done.add(task.container)
            except Exception as e:
                self._error = e
            finally:
                self._cv.acquire()
                self._cv.notifyAll()
                self._cv.release()

        t = threading.Thread(target=act, args=(tuple([task])))
        t.daemon = True
        t.start()
        self._threads.add(t)

    def end(self):
        """End the orchestration play by waiting for all the action threads to
        complete."""
        for t in self._threads:
            try:
                while not self._error and t.isAlive():
                    t.join(1)
            except KeyboardInterrupt:
                self._error = 'Manual abort.'
            except Exception as e:
                self._error = e
            finally:
                self._cv.acquire()
                self._cv.notifyAll()
                self._cv.release()
        self._om.end()

        # Display any error that occurred
        if self._error:
            sys.stderr.write('{}\n'.format(self._error))

    def run(self):
        raise NotImplementedError

    def _gather_dependencies(self, container):
        """Transitively gather all containers from the dependencies or
        dependent (depending on the value of the forward parameter) services
        of the service the given container is a member of. This set is limited
        to the containers involved in the orchestration play."""
        containers = set(self._containers)
        result = set([container])

        for container in result:
            deps = container.service.requires if self._forward \
                else container.service.needed_for
            deps = functools.reduce(lambda x, y: x.union(y),
                                    [s.containers for s in deps],
                                    set([]))
            result = result.union(deps.intersection(containers))

        result.remove(container)
        return result

    def _satisfied(self, container):
        """Returns True if all the dependencies of a given container have been
        satisfied by what's been executed so far."""
        missing = self._dependencies[container.name].difference(self._done)
        return len(missing) == 0


class FullStatus(BaseOrchestrationPlay):
    """A Maestro orchestration play that displays the status of the given
    services and/or instance containers.

    This orchestration play does not make use of the concurrent play execution
    features.
    """

    def __init__(self, containers=[]):
        BaseOrchestrationPlay.__init__(self, containers)

    def run(self):
        self.start()
        for order, container in enumerate(self._containers, 1):
            o = termoutput.OutputFormatter(prefix=(
                '{:>3d}. \033[;1m{:<20.20s}\033[;0m {:<15.15s} ' +
                '{:<20.20s}').format(order,
                                     container.name,
                                     container.service.name,
                                     container.ship.address))

            try:
                o.pending('checking container...')
                status = container.status()
                o.commit('\033[{:d};1m{:<15s}\033[;0m'.format(
                    color(status and status['State']['Running']),
                    (status and status['State']['Running']
                        and container.id[:7] or 'down')))

                o.pending('checking service...')
                running = status and status['State']['Running']
                o.commit('\033[{:d};1m{:<4.4s}\033[;0m'.format(color(running),
                                                               up(running)))

                for name, port in container.ports.items():
                    o.commit('\n')
                    o = termoutput.OutputFormatter(prefix='     >>')
                    o.pending('{:>9.9s}:{:s}'.format(port['external'][1],
                                                     name))
                    ping = container.ping_port(name)
                    o.commit('\033[{:d};1m{:>9.9s}\033[;0m:{:s}'.format(
                        color(ping), port['external'][1], name))
            except Exception:
                o.commit(red('{:<15s} {:<10s}'.format('host down', 'down')))
            o.commit('\n')
        self.end()


class Status(BaseOrchestrationPlay):
    """A less advanced, but faster (concurrent) status display orchestration
    play that only looks at the presence and status of the containers."""

    def __init__(self, containers=[]):
        BaseOrchestrationPlay.__init__(self, containers,
                                       respect_dependencies=False)

    def run(self):
        self.start()
        for order, container in enumerate(self._containers):
            o = self._om.get_formatter(order, prefix=(
                '{:>3d}. \033[;1m{:<20.20s}\033[;0m {:<15.15s} ' +
                '{:<20.20s}').format(order + 1,
                                     container.name,
                                     container.service.name,
                                     container.ship.address))
            self.register(tasks.StatusTask(o, container))
        self.end()


class Start(BaseOrchestrationPlay):
    """A Maestro orchestration play that will execute the start sequence of the
    requested services, starting each container for each instance of the
    services, in the given start order, waiting for each container's
    application to become available before moving to the next one."""

    def __init__(self, containers=[], registries={}, refresh_images=False):
        BaseOrchestrationPlay.__init__(self, containers)
        self._registries = registries
        self._refresh_images = refresh_images

    def run(self):
        self.start()
        for order, container in enumerate(self._containers):
            o = self._om.get_formatter(order, prefix=(
                '{:>3d}. \033[;1m{:<20.20s}\033[;0m {:<15.15s} ' +
                '{:<20.20s}').format(order + 1,
                                     container.name,
                                     container.service.name,
                                     container.ship.address))
            self.register(tasks.StartTask(o, container))
        self.end()


class Stop(BaseOrchestrationPlay):
    """A Maestro orchestration play that will stop the containers of the
    requested services. The list of containers should be provided reversed so
    that dependent services are stopped first."""

    def __init__(self, containers=[]):
        BaseOrchestrationPlay.__init__(self, containers, forward=False)

    def run(self):
        self.start()
        for order, container in enumerate(self._containers):
            o = self._om.get_formatter(order, prefix=(
                '{:>3d}. \033[;1m{:<20.20s}\033[;0m {:<15.15s} ' +
                '{:<20.20s}').format(len(self._containers) - order,
                                     container.name,
                                     container.service.name,
                                     container.ship.address))
            self.register(tasks.StopTask(o, container))
        self.end()


class Restart(BaseOrchestrationPlay):

    def __init__(self, containers=[], registries={}, refresh_images=False,
                 concurrency=None):
        BaseOrchestrationPlay.__init__(self, containers)
        self._registries = registries
        self._refresh_images = refresh_images
        self._concurrency = concurrency

    def run(self):
        self.start()
        self.end()
