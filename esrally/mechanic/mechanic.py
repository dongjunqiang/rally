import sys
import logging

import thespian.actors

from esrally import actor, paths, config, metrics, exceptions, time, PROGRAM_NAME
from esrally.utils import console, net
from esrally.mechanic import supplier, provisioner, launcher, team

logger = logging.getLogger("rally.mechanic")


##############################
# Public Messages
##############################

class ClusterMetaInfo:
    def __init__(self, nodes, revision, distribution_version):
        self.nodes = nodes
        self.revision = revision
        self.distribution_version = distribution_version

    def as_dict(self):
        return {
            "nodes": [n.as_dict() for n in self.nodes],
            "node-count": len(self.nodes),
            "revision": self.revision,
            "distribution-version": self.distribution_version
        }


class NodeMetaInfo:
    def __init__(self, n):
        self.host_name = n.host_name
        self.node_name = n.node_name
        self.ip = n.ip
        self.os = n.os
        self.jvm = n.jvm
        self.cpu = n.cpu
        self.memory = n.memory
        self.fs = n.fs
        self.plugins = n.plugins

    def as_dict(self):
        return self.__dict__


class StartEngine:
    def __init__(self, cfg, open_metrics_context, cluster_settings, sources, build, distribution, external, docker, ip=None, port=None,
                 node_id=None):
        self.cfg = cfg
        self.open_metrics_context = open_metrics_context
        self.cluster_settings = cluster_settings
        self.sources = sources
        self.build = build
        self.distribution = distribution
        self.external = external
        self.docker = docker
        self.ip = ip
        self.port = port
        self.node_id = node_id

    def for_nodes(self, all_node_ips=None, ip=None, port=None, node_ids=None):
        """

        Creates a StartNodes instance for a concrete IP, port and their associated node_ids.

        :param all_node_ips: The IPs of all nodes in the cluster (including the current one).
        :param ip: The IP to set.
        :param port: The port number to set.
        :param node_ids: A list of node id to set.
        :return: A corresponding ``StartNodes`` message with the specified IP, port number and node ids.
        """
        return StartNodes(self.cfg, self.open_metrics_context, self.cluster_settings, self.sources, self.build, self.distribution,
                          self.external, self.docker, all_node_ips, ip, port, node_ids)


class EngineStarted:
    def __init__(self, cluster_meta_info, system_meta_info):
        self.cluster_meta_info = cluster_meta_info
        self.system_meta_info = system_meta_info


class StopEngine:
    pass


class EngineStopped:
    def __init__(self, system_metrics):
        self.system_metrics = system_metrics


class Failure:
    def __init__(self, message, cause):
        self.message = message
        self.cause = cause


class OnBenchmarkStart:
    def __init__(self, lap):
        self.lap = lap


class BenchmarkStarted:
    pass


class OnBenchmarkStop:
    pass


class BenchmarkStopped:
    def __init__(self, system_metrics):
        self.system_metrics = system_metrics


##############################
# Mechanic internal messages
##############################

class StartNodes:
    def __init__(self, cfg, open_metrics_context, cluster_settings, sources, build, distribution, external, docker,
                 all_node_ips, ip, port, node_ids):
        self.cfg = cfg
        self.open_metrics_context = open_metrics_context
        self.cluster_settings = cluster_settings
        self.sources = sources
        self.build = build
        self.distribution = distribution
        self.external = external
        self.docker = docker
        self.all_node_ips = all_node_ips
        self.ip = ip
        self.port = port
        self.node_ids = node_ids


class NodesStarted:
    def __init__(self, node_meta_infos, system_meta_info):
        """
        Creates a new NodeStarted message.

        :param node_meta_infos: A list of ``NodeMetaInfo`` instances.
        :param system_meta_info:
        """
        self.node_meta_infos = node_meta_infos
        self.system_meta_info = system_meta_info


class StopNodes:
    pass


class NodesStopped:
    def __init__(self, system_metrics):
        self.system_metrics = system_metrics


class ApplyMetricsMetaInfo:
    def __init__(self, meta_info):
        self.meta_info = meta_info


class MetricsMetaInfoApplied:
    pass


def to_ip_port(hosts):
    ip_port_pairs = []
    for host in hosts:
        host = host.copy()
        host_or_ip = host.pop("host")
        port = host.pop("port", 9200)
        if host:
            raise exceptions.SystemSetupError("When specifying nodes to be managed by Rally you can only supply "
                                              "hostname:port pairs (e.g. 'localhost:9200'), any additional options cannot "
                                              "be supported.")
        ip = net.resolve(host_or_ip)
        ip_port_pairs.append((ip, port))
    return ip_port_pairs


def extract_all_node_ips(ip_port_pairs):
    all_node_ips = set()
    for ip, port in ip_port_pairs:
        all_node_ips.add(ip)
    return all_node_ips


def nodes_by_host(ip_port_pairs):
    nodes = {}
    node_id = 0
    for ip_port in ip_port_pairs:
        if ip_port not in nodes:
            nodes[ip_port] = []
        nodes[ip_port].append(node_id)
        node_id += 1
    return nodes


class MechanicActor(actor.RallyActor):
    """
    This actor coordinates all associated mechanics on remote hosts (which do the actual work).
    """
    def __init__(self):
        super().__init__()
        actor.RallyActor.configure_logging(logger)
        self.mechanics = []
        self.node_ids = []
        self.cfg = None
        self.metrics_store = None
        self.race_control = None
        self.cluster_launcher = None
        self.cluster = None
        self.status = None
        self.received_responses = []

    def receiveMessage(self, msg, sender):
        try:
            logger.debug("MechanicActor#receiveMessage(msg = [%s] sender = [%s])" % (str(type(msg)), str(sender)))
            if isinstance(msg, StartEngine):
                self.on_start_engine(msg, sender)
            elif isinstance(msg, NodesStarted):
                self.metrics_store.merge_meta_info(msg.system_meta_info)
                self.transition_when_all_children_responded(sender, msg, "starting", "nodes_started", self.on_all_nodes_started)
            elif isinstance(msg, MetricsMetaInfoApplied):
                self.transition_when_all_children_responded(sender, msg, "apply_meta_info", "cluster_started", self.on_cluster_started)
            elif isinstance(msg, OnBenchmarkStart):
                self.metrics_store.lap = msg.lap
                self.cluster.on_benchmark_start()
                # in the first lap, we are in state "cluster_started", after that in "benchmark_stopped"
                self.send_to_children_and_transition(sender, msg, ["cluster_started", "benchmark_stopped"], "benchmark_starting")
            elif isinstance(msg, BenchmarkStarted):
                self.transition_when_all_children_responded(
                    sender, msg, "benchmark_starting", "benchmark_started", self.on_benchmark_started)
            elif isinstance(msg, Failure):
                self.send(self.race_control, msg)
            elif isinstance(msg, OnBenchmarkStop):
                self.send_to_children_and_transition(sender, msg, "benchmark_started", "benchmark_stopping")
            elif isinstance(msg, BenchmarkStopped):
                self.metrics_store.bulk_add(msg.system_metrics)
                self.transition_when_all_children_responded(
                    sender, msg, "benchmark_stopping", "benchmark_stopped", self.on_benchmark_stopped)
            elif isinstance(msg, StopEngine):
                # detach from cluster and gather all system metrics
                self.cluster_launcher.stop(self.cluster)
                # we might have experienced a launch error, hence we need to allow to stop the cluster also after a launch
                self.send_to_children_and_transition(sender, StopNodes(), ["nodes_started", "benchmark_stopped"], "cluster_stopping")
            elif isinstance(msg, NodesStopped):
                self.metrics_store.bulk_add(msg.system_metrics)
                self.transition_when_all_children_responded(sender, msg, "cluster_stopping", "cluster_stopped", self.on_all_nodes_stopped)
            elif isinstance(msg, thespian.actors.ChildActorExited):
                if self.is_current_status_expected("cluster_stopping"):
                    logger.info("Child actor exited while engine is stopping: [%s]" % msg)
                else:
                    raise exceptions.RallyError("Child actor exited with [%s] while in status [%s]." % (msg, self.status))
            elif isinstance(msg, thespian.actors.PoisonMessage):
                # something went wrong with a child actor
                if isinstance(msg.poisonMessage, StartEngine):
                    raise exceptions.LaunchError("Could not start benchmark candidate. Are Rally daemons on all targeted machines running?")
                else:
                    logger.error("[%s] sent to a child actor has resulted in PoisonMessage" % str(msg.poisonMessage))
                    raise exceptions.RallyError("Could not communicate with benchmark candidate (unknown reason)")
        except BaseException:
            logger.exception("Cannot process message [%s]" % msg)
            # usually, we'll notify the sender but in case a child sent something that caused an exception we'd rather
            # have it bubble up to race control. Otherwise, we could play ping-pong with our child actor.
            recipient = self.race_control if sender in self.mechanics else sender
            ex_type, ex_value, ex_traceback = sys.exc_info()
            # avoid "can't pickle traceback objects"
            import traceback
            self.send(recipient, Failure("Could not execute command (%s)" % ex_value, traceback.format_exc()))

    def transition_when_all_children_responded(self, sender, msg, expected_status, new_status, transition):
        """

        Waits until all children have sent a specific response message and then transitions this actor to a new status.

        :param sender: The child actor that has responded.
        :param msg: The response message.
        :param expected_status: The status in which this actor should be upon calling this method.
        :param new_status: The new status once all child actors have responded.
        :param transition: A parameter-less function to call immediately after changing the status.
        """
        if self.is_current_status_expected(expected_status):
            self.received_responses.append(msg)
            response_count = len(self.received_responses)
            expected_count = len(self.mechanics)

            logger.info("[%d] of [%d] child actors have responded for transition from [%s] to [%s]." %
                        (response_count, expected_count, self.status, new_status))
            if response_count == expected_count:
                logger.info("All [%d] child actors have responded. Transitioning now from [%s] to [%s]." %
                            (expected_count, self.status, new_status))
                # all nodes have responded, change status
                self.status = new_status
                self.received_responses = []
                transition()
            elif response_count > expected_count:
                raise exceptions.RallyAssertionError(
                    "Received [%d] responses but only [%d] were expected to transition from [%s] to [%s]. The responses are: %s" %
                    (response_count, expected_count, self.status, new_status, self.received_responses))
        else:
            raise exceptions.RallyAssertionError("Received [%s] from [%s] but we are in status [%s] instead of [%s]." %
                                                 (type(msg), sender, self.status, expected_status))

    def send_to_children_and_transition(self, sender, msg, expected_status, new_status):
        """

        Sends the provided message to all child actors and immediately transitions to the new status.

        :param sender: The actor from which we forward this message (in case it is message forwarding). Otherwise our own address.
        :param msg: The message to send.
        :param expected_status: The status in which this actor should be upon calling this method.
        :param new_status: The new status.
        """
        if self.is_current_status_expected(expected_status):
            logger.info("Transitioning from [%s] to [%s]." % (self.status, new_status))
            self.status = new_status
            for m in self.mechanics:
                self.send(m, msg)
        else:
            raise exceptions.RallyAssertionError("Received [%s] from [%s] but we are in status [%s] instead of [%s]." %
                                                 (type(msg), sender, self.status, expected_status))

    def is_current_status_expected(self, expected_status):
        # if we don't expect anything, we're always in the right status
        if not expected_status:
            return True
        # do an explicit check for a list here because strings are also iterable and we have very tight control over this code anyway.
        elif isinstance(expected_status, list):
            return self.status in expected_status
        else:
            return self.status == expected_status

    def on_start_engine(self, msg, sender):
        logger.info("Received signal from race control to start engine.")
        self.race_control = sender
        self.cfg = msg.cfg
        self.metrics_store = metrics.InMemoryMetricsStore(self.cfg)
        self.metrics_store.open(ctx=msg.open_metrics_context)

        # In our startup procedure we first create all mechanics. Only if this succeeds we'll continue.
        mechanics_and_start_message = []
        hosts = self.cfg.opts("client", "hosts")
        if len(hosts) == 0:
            raise exceptions.LaunchError("No target hosts are configured.")

        if msg.external:
            logger.info("Cluster will not be provisioned by Rally.")
            # just create one actor for this special case and run it on the coordinator node (i.e. here)
            m = self.createActor(NodeMechanicActor,
                                 globalName="/rally/mechanic/worker/external",
                                 targetActorRequirements={"coordinator": True})
            self.mechanics.append(m)
            mechanics_and_start_message.append((m, msg.for_nodes(ip=hosts)))
        else:
            logger.info("Cluster consisting of %s will be provisioned by Rally." % hosts)
            all_ips_and_ports = to_ip_port(hosts)
            all_node_ips = extract_all_node_ips(all_ips_and_ports)
            for ip_port, nodes in nodes_by_host(all_ips_and_ports).items():
                ip, port = ip_port
                if ip == "127.0.0.1":
                    m = self.createActor(NodeMechanicActor,
                                         globalName="/rally/mechanic/worker/localhost",
                                         targetActorRequirements={"coordinator": True})
                    self.mechanics.append(m)
                    mechanics_and_start_message.append((m, msg.for_nodes(all_node_ips, ip, port, nodes)))
                else:
                    if self.cfg.opts("system", "remote.benchmarking.supported"):
                        logger.info("Benchmarking against %s with external Rally daemon." % hosts)
                    else:
                        logger.error("User tried to benchmark against %s but no external Rally daemon has been started." % hosts)
                        raise exceptions.SystemSetupError("To benchmark remote hosts (e.g. %s) you need to start the Rally daemon "
                                                          "on each machine including this one." % ip)
                    already_running = actor.actor_system_already_running(ip=ip)
                    logger.info("Actor system on [%s] already running? [%s]" % (ip, str(already_running)))
                    if not already_running:
                        console.println("Waiting for Rally daemon on [%s] " % ip, end="", flush=True)
                    while not actor.actor_system_already_running(ip=ip):
                        console.println(".", end="", flush=True)
                        time.sleep(3)
                    if not already_running:
                        console.println(" [OK]")
                    m = self.createActor(NodeMechanicActor,
                                         globalName="/rally/mechanic/worker/%s" % ip,
                                         targetActorRequirements={"ip": ip})
                    mechanics_and_start_message.append((m, msg.for_nodes(all_node_ips, ip, port, nodes)))
                    self.mechanics.append(m)
        self.status = "starting"
        self.received_responses = []
        for mechanic_actor, start_message in mechanics_and_start_message:
            self.send(mechanic_actor, start_message)

    def on_all_nodes_started(self):
        self.cluster_launcher = launcher.ClusterLauncher(self.cfg, self.metrics_store)
        self.cluster = self.cluster_launcher.start()
        # push down all meta data again
        self.send_to_children_and_transition(self.myAddress, ApplyMetricsMetaInfo(self.metrics_store.meta_info), "nodes_started", "apply_meta_info")

    def on_cluster_started(self):
        # We don't need to store the original node meta info when the node started up (NodeStarted message) because we actually gather it
        # in ``on_all_nodes_started`` via the ``ClusterLauncher``.
        self.send(self.race_control,
                  EngineStarted(ClusterMetaInfo([NodeMetaInfo(n) for n in self.cluster.nodes],
                                                self.cluster.source_revision,
                                                self.cluster.distribution_version),
                                self.metrics_store.meta_info))

    def on_benchmark_started(self):
        self.cluster.on_benchmark_start()
        self.send(self.race_control, BenchmarkStarted())

    def on_benchmark_stopped(self):
        self.cluster.on_benchmark_stop()
        self.send(self.race_control, BenchmarkStopped(self.metrics_store.to_externalizable(clear=True)))

    def on_all_nodes_stopped(self):
        self.send(self.race_control, EngineStopped(self.metrics_store.to_externalizable()))
        # clear all state as the mechanic might get reused later
        for m in self.mechanics:
            self.send(m, thespian.actors.ActorExitRequest())
        self.mechanics = []
        # self terminate + slave nodes
        self.send(self.myAddress, thespian.actors.ActorExitRequest())


class NodeMechanicActor(actor.RallyActor):
    """
    One instance of this actor is run on each target host and coordinates the actual work of starting / stopping all nodes that should run
    on this host.
    """
    def __init__(self):
        super().__init__()
        actor.RallyActor.configure_logging(logger)
        self.config = None
        self.metrics_store = None
        self.mechanic = None
        self.running = False

    def receiveMessage(self, msg, sender):
        # at the moment, we implement all message handling blocking. This is not ideal but simple to get started with. Besides, the caller
        # needs to block anyway. The only reason we implement mechanic as an actor is to distribute them.
        # noinspection PyBroadException
        try:
            logger.debug("NodeMechanicActor#receiveMessage(msg = [%s] sender = [%s])" % (str(type(msg)), str(sender)))
            if isinstance(msg, StartNodes):
                if msg.external:
                    logger.info("Connecting to externally provisioned nodes on [%s]." % msg.ip)
                else:
                    logger.info("Starting node(s) %s on [%s]." % (msg.node_ids, msg.ip))

                # Load node-specific configuration
                self.config = config.Config(config_name=msg.cfg.name)
                self.config.load_config()
                self.config.add(config.Scope.application, "node", "rally.root", paths.rally_root())
                # copy only the necessary configuration sections
                self.config.add_all(msg.cfg, "system")
                self.config.add_all(msg.cfg, "distributions")
                self.config.add_all(msg.cfg, "client")
                self.config.add_all(msg.cfg, "track")
                self.config.add_all(msg.cfg, "mechanic")
                # allow metrics store to extract race meta-data
                self.config.add_all(msg.cfg, "race")
                if not msg.external:
                    self.config.add(config.Scope.benchmark, "provisioning", "node.ip", msg.ip)
                    # we need to override the port with the value that the user has specified instead of using the default value (39200)
                    self.config.add(config.Scope.benchmark, "provisioning", "node.http.port", msg.port)
                    self.config.add(config.Scope.benchmark, "provisioning", "node.ids", msg.node_ids)

                self.metrics_store = metrics.InMemoryMetricsStore(self.config)
                self.metrics_store.open(ctx=msg.open_metrics_context)
                # avoid follow-up errors in case we receive an unexpected ActorExitRequest due to an early failure in a parent actor.
                self.metrics_store.lap = 0

                self.mechanic = create(self.config, self.metrics_store, msg.all_node_ips, msg.cluster_settings, msg.sources, msg.build,
                                       msg.distribution, msg.external, msg.docker)
                nodes = self.mechanic.start_engine()
                self.running = True
                self.send(sender, NodesStarted([NodeMetaInfo(node) for node in nodes], self.metrics_store.meta_info))
            elif isinstance(msg, ApplyMetricsMetaInfo):
                self.metrics_store.merge_meta_info(msg.meta_info)
                self.send(sender, MetricsMetaInfoApplied())
            elif isinstance(msg, OnBenchmarkStart):
                self.metrics_store.lap = msg.lap
                self.mechanic.on_benchmark_start()
                self.send(sender, BenchmarkStarted())
            elif isinstance(msg, OnBenchmarkStop):
                self.mechanic.on_benchmark_stop()
                # clear metrics store data to not send duplicate system metrics data
                self.send(sender, BenchmarkStopped(self.metrics_store.to_externalizable(clear=True)))
            elif isinstance(msg, StopNodes):
                logger.info("Stopping nodes %s." % self.mechanic.nodes)
                self.mechanic.stop_engine()
                self.send(sender, NodesStopped(self.metrics_store.to_externalizable()))
                # clear all state as the mechanic might get reused later
                self.running = False
                self.config = None
                self.mechanic = None
                self.metrics_store = None
            elif isinstance(msg, thespian.actors.ActorExitRequest):
                if self.running:
                    logger.info("Stopping nodes %s (due to ActorExitRequest)" % self.mechanic.nodes)
                    self.mechanic.stop_engine()
                    self.running = False
        except BaseException:
            self.running = False
            logger.exception("Cannot process message [%s]" % msg)
            # avoid "can't pickle traceback objects"
            import traceback
            ex_type, ex_value, ex_traceback = sys.exc_info()
            self.send(sender, Failure(ex_value, traceback.format_exc()))


#####################################################
# Internal API (only used by the actor and for tests)
#####################################################

def create(cfg, metrics_store, all_node_ips, cluster_settings=None, sources=False, build=False, distribution=False, external=False,
           docker=False):
    races_root = paths.races_root(cfg)
    challenge_root_path = paths.race_root(cfg)
    node_ids = cfg.opts("provisioning", "node.ids", mandatory=False)
    repo = team.team_repo(cfg)
    # externally provisioned clusters do not support cars / plugins
    if external:
        car = None
        plugins = []
    else:
        car = team.load_car(repo, cfg.opts("mechanic", "car.name"))
        plugins = team.load_plugins(repo, cfg.opts("mechanic", "car.plugins"))

    if sources:
        try:
            src_dir = cfg.opts("source", "local.src.dir")
        except config.ConfigError:
            logger.exception("Cannot determine source directory")
            raise exceptions.SystemSetupError("You cannot benchmark Elasticsearch from sources. Did you install Gradle? Please install"
                                              " all prerequisites and reconfigure Rally with %s configure" % PROGRAM_NAME)

        remote_url = cfg.opts("source", "remote.repo.url")
        revision = cfg.opts("mechanic", "source.revision")
        gradle = cfg.opts("build", "gradle.bin")
        java_home = cfg.opts("runtime", "java8.home")

        if len(plugins) > 0:
            raise exceptions.RallyError("Source builds of plugins are not supported yet. For more details, please "
                                        "check https://github.com/elastic/rally/issues/309 and upgrade Rally in case support has been "
                                        "added in the meantime.")
        s = lambda: supplier.from_sources(remote_url, src_dir, revision, gradle, java_home, challenge_root_path, build)
        p = []
        for node_id in node_ids:
            p.append(provisioner.local_provisioner(cfg, car, plugins, cluster_settings, all_node_ips, challenge_root_path, node_id))
        l = launcher.InProcessLauncher(cfg, metrics_store, races_root, challenge_root_path)
    elif distribution:
        version = cfg.opts("mechanic", "distribution.version")
        repo_name = cfg.opts("mechanic", "distribution.repository")
        distributions_root = "%s/%s" % (cfg.opts("node", "root.dir"), cfg.opts("source", "distribution.dir"))
        distribution_cfg = cfg.all_opts("distributions")

        s = lambda: supplier.from_distribution(version=version, repo_name=repo_name, distribution_config=distribution_cfg,
                                               distributions_root=distributions_root, plugins=plugins)
        p = []
        for node_id in node_ids:
            p.append(provisioner.local_provisioner(cfg, car, plugins, cluster_settings, all_node_ips, challenge_root_path, node_id))
        l = launcher.InProcessLauncher(cfg, metrics_store, races_root, challenge_root_path)
    elif external:
        if cluster_settings:
            logger.warning("Cannot apply challenge-specific cluster settings [%s] for an externally provisioned cluster. Please ensure "
                           "that the cluster settings are present or the benchmark may fail or behave unexpectedly." % cluster_settings)
        if len(plugins) > 0:
            raise exceptions.SystemSetupError("You cannot specify any plugins for externally provisioned clusters. Please remove "
                                              "\"--elasticsearch-plugins\" and try again.")

        s = lambda: None
        p = [provisioner.no_op_provisioner()]
        l = launcher.ExternalLauncher(cfg, metrics_store)
    elif docker:
        if len(plugins) > 0:
            raise exceptions.SystemSetupError("You cannot specify any plugins for Docker clusters. Please remove "
                                              "\"--elasticsearch-plugins\" and try again.")
        s = lambda: None
        p = []
        for node_id in node_ids:
            p.append(provisioner.docker_provisioner(cfg, car, cluster_settings, challenge_root_path, node_id))
        l = launcher.DockerLauncher(cfg, metrics_store)
    else:
        # It is a programmer error (and not a user error) if this function is called with wrong parameters
        raise RuntimeError("One of sources, distribution, docker or external must be True")

    return Mechanic(s, p, l)


class Mechanic:
    """
    Mechanic is responsible for preparing the benchmark candidate (i.e. all benchmark candidate related activities before and after
    running the benchmark).
    """

    def __init__(self, supply, provisioners, l):
        self.supply = supply
        self.provisioners = provisioners
        self.launcher = l
        self.nodes = []

    def start_engine(self):
        binaries = self.supply()
        node_configs = []
        for p in self.provisioners:
            node_configs.append(p.prepare(binaries))
        self.nodes = self.launcher.start(node_configs)
        return self.nodes

    def on_benchmark_start(self):
        for node in self.nodes:
            node.on_benchmark_start()

    def on_benchmark_stop(self):
        for node in self.nodes:
            node.on_benchmark_stop()

    def stop_engine(self):
        self.launcher.stop(self.nodes)
        self.nodes = []
        for p in self.provisioners:
            p.cleanup()
