import logging
import ovn_exceptions
import ovn_sandbox
import ovn_stats
import ovn_utils
from ovn_load_balancer import create_load_balancer
import netaddr
from collections import namedtuple
from collections import defaultdict
from randmac import RandMac
from datetime import datetime
import asyncio

log = logging.getLogger(__name__)

ClusterConfig = namedtuple('ClusterConfig',
                           ['cluster_cmd_path',
                            'monitor_all',
                            'logical_dp_groups',
                            'clustered_db',
                            'datapath_type',
                            'raft_election_to',
                            'northd_probe_interval',
                            'db_inactivity_probe',
                            'node_net',
                            'enable_ssl',
                            'node_remote',
                            'node_timeout_s',
                            'internal_net',
                            'external_net',
                            'gw_net',
                            'cluster_net',
                            'n_workers',
                            'n_relays',
                            'vips',
                            'vip_subnet',
                            'static_vips',
                            'use_ovsdb_etcd'])


BrExConfig = namedtuple('BrExConfig', ['physical_net'])


class Node(ovn_sandbox.Sandbox):
    def __init__(self, phys_node, container, mgmt_net, mgmt_ip):
        super(Node, self).__init__(phys_node, container)
        self.container = container
        self.mgmt_net = mgmt_net
        self.mgmt_ip = mgmt_ip

    def build_cmd(self, cluster_cfg, cmd, *args):
        monitor_all = 'yes' if cluster_cfg.monitor_all else 'no'
        etcd_cmd = 'yes' if cluster_cfg.use_ovsdb_etcd else 'no'
        clustered_db = 'yes' if cluster_cfg.clustered_db else 'no'
        enable_ssl = 'yes' if cluster_cfg.enable_ssl else 'no'
        cmd = \
            f'cd {cluster_cfg.cluster_cmd_path} && ' \
            f'OVN_MONITOR_ALL={monitor_all} OVN_DB_CLUSTER={clustered_db} '\
            f'ENABLE_SSL={enable_ssl} ENABLE_ETCD={etcd_cmd} '\
            f'OVN_DP_TYPE={cluster_cfg.datapath_type} ' \
            f'CREATE_FAKE_VMS=no CHASSIS_COUNT=0 GW_COUNT=0 '\
            f'RELAY_COUNT={cluster_cfg.n_relays} '\
            f'IP_HOST={self.mgmt_net.ip} ' \
            f'IP_CIDR={self.mgmt_net.prefixlen} ' \
            f'IP_START={self.mgmt_ip} ' \
            f'./ovn_cluster.sh {cmd}'
        return cmd + ' ' + ' '.join(args)


class CentralNode(Node):
    def __init__(self, phys_node, db_containers, relay_containers, mgmt_net,
                 mgmt_ip):
        super(CentralNode, self).__init__(phys_node, db_containers[0],
                                          mgmt_net, mgmt_ip)
        self.db_containers = db_containers
        self.relay_containers = relay_containers

    async def start(self, cluster_cfg):
        log.info('Starting central node')
        await self.phys_node.run(self.build_cmd(cluster_cfg, 'start'))
        await asyncio.sleep(5)
        await self.set_raft_election_timeout(cluster_cfg.raft_election_to)
        await self.enable_trim_on_compaction()

    async def set_raft_election_timeout(self, timeout_s):
        for timeout in range(1000, (timeout_s + 1) * 1000, 1000):
            log.info(f'Setting RAFT election timeout to {timeout}ms')
            await self.run(cmd=f'ovs-appctl -t '
                           f'/run/ovn/ovnnb_db.ctl '
                           f'cluster/change-election-timer '
                           f'OVN_Northbound {timeout}')
            await self.run(cmd=f'ovs-appctl -t '
                           f'/run/ovn/ovnsb_db.ctl '
                           f'cluster/change-election-timer '
                           f'OVN_Southbound {timeout}')

    async def enable_trim_on_compaction(self):
        log.info('Setting DB trim-on-compaction')
        for db_container in self.db_containers:
            await self.phys_node.run(f'docker exec {db_container} ovs-appctl '
                                     f'-t /run/ovn/ovnnb_db.ctl '
                                     f'ovsdb-server/memory-trim-on-compaction '
                                     f'on')
            await self.phys_node.run(f'docker exec {db_container} ovs-appctl '
                                     f'-t /run/ovn/ovnsb_db.ctl '
                                     f'ovsdb-server/memory-trim-on-compaction '
                                     f'on')
        for relay_container in self.relay_containers:
            await self.phys_node.run(f'docker exec {relay_container}'
                                     f'ovs-appctl -t '
                                     f'/run/ovn/ovnsb_db.ctl '
                                     f'ovsdb-server/memory-trim-on-compaction '
                                     f'on')


class WorkerNode(Node):
    def __init__(self, phys_node, container, mgmt_net, mgmt_ip,
                 int_net, ext_net, gw_net, unique_id):
        super(WorkerNode, self).__init__(phys_node, container,
                                         mgmt_net, mgmt_ip)
        self.int_net = int_net
        self.ext_net = ext_net
        self.gw_net = gw_net
        self.id = unique_id
        self.switch = None
        self.gw_router = None
        self.ext_switch = None
        self.lports = []
        self.next_lport_index = 0

    async def start(self, cluster_cfg):
        log.info(f'Starting worker {self.container}')
        await self.phys_node.run(self.build_cmd(cluster_cfg, 'add-chassis',
                                                self.container,
                                                'tcp:0.0.0.1:6642'))

    @ovn_stats.timeit
    async def connect(self, cluster_cfg):
        log.info(f'Connecting worker {self.container}')
        await self.phys_node.run(self.build_cmd(cluster_cfg,
                                                'set-chassis-ovn-remote',
                                                self.container,
                                                cluster_cfg.node_remote))

    async def configure_localnet(self, physical_net):
        log.info(f'Creating localnet on {self.container}')
        await self.run(cmd=f'ovs-vsctl -- set open_vswitch . '
                       f'external-ids:ovn-bridge-mappings='
                       f'{physical_net}:br-ex')

    async def configure_external_host(self):
        log.info(f'Adding external host on {self.container}')
        gw_ip = netaddr.IPAddress(self.ext_net.last - 1)
        host_ip = netaddr.IPAddress(self.ext_net.last - 2)

        await self.run(cmd='ip link add veth0 type veth peer name veth1')
        await self.run(cmd='ip netns add ext-ns')
        await self.run(cmd='ip link set netns ext-ns dev veth0')
        await self.run(cmd='ip netns exec ext-ns ip link set dev veth0 up')
        await self.run(cmd=f'ip netns exec ext-ns '
                       f'ip addr add {host_ip}/{self.ext_net.prefixlen} '
                       f'dev veth0')
        await self.run(cmd=f'ip netns exec ext-ns ip route add default via '
                       f'{gw_ip}')
        await self.run(cmd='ip link set dev veth1 up')
        await self.run(cmd='ovs-vsctl add-port br-ex veth1')

    async def configure(self, physical_net):
        await self.configure_localnet(physical_net)
        await self.configure_external_host()

    @ovn_stats.timeit
    async def wait(self, sbctl, timeout_s):
        for _ in range(timeout_s * 10):
            if await sbctl.chassis_bound(self.container):
                return
            await asyncio.sleep(0.1)
        raise ovn_exceptions.OvnChassisTimeoutException()

    @ovn_stats.timeit
    async def provision(self, cluster):
        await self.connect(cluster.cluster_cfg)
        await self.wait(cluster.sbctl, cluster.cluster_cfg.node_timeout_s)

        # Create a node switch and connect it to the cluster router.
        self.switch = await cluster.nbctl.ls_add(f'lswitch-{self.container}',
                                                 cidr=self.int_net)
        lrp_name = f'rtr-to-node-{self.container}'
        ls_rp_name = f'node-to-rtr-{self.container}'
        lrp_ip = netaddr.IPAddress(self.int_net.last - 1)
        self.rp = await cluster.nbctl.lr_port_add(
            cluster.router, lrp_name, RandMac(), lrp_ip,
            self.int_net.prefixlen
        )
        self.ls_rp = await cluster.nbctl.ls_port_add(
            self.switch, ls_rp_name, self.rp
        )

        # Make the lrp as distributed gateway router port.
        await cluster.nbctl.lr_port_set_gw_chassis(self.rp, self.container)

        # Create a gw router and connect it to the cluster join switch.
        self.gw_router = await cluster.nbctl.lr_add(
            f'gwrouter-{self.container}'
        )
        await cluster.nbctl.run(f'set Logical_Router {self.gw_router.name} '
                                f'options:chassis={self.container}')
        join_grp_name = f'gw-to-join-{self.container}'
        join_ls_grp_name = f'join-to-gw-{self.container}'
        gr_gw = netaddr.IPAddress(self.gw_net.last - 2 - self.id)
        self.gw_rp = await cluster.nbctl.lr_port_add(
            self.gw_router, join_grp_name, RandMac(), gr_gw,
            self.gw_net.prefixlen
        )
        self.join_gw_rp = await cluster.nbctl.ls_port_add(
            cluster.join_switch, join_ls_grp_name, self.gw_rp
        )

        # Create an external switch connecting the gateway router to the
        # physnet.
        self.ext_switch = await cluster.nbctl.ls_add(f'ext-{self.container}',
                                                     cidr=self.ext_net)
        ext_lrp_name = f'gw-to-ext-{self.container}'
        ext_ls_rp_name = f'ext-to-gw-{self.container}'
        lrp_ip = netaddr.IPAddress(self.ext_net.last - 1)
        self.ext_rp = await cluster.nbctl.lr_port_add(
            self.gw_router, ext_lrp_name, RandMac(), lrp_ip,
            self.ext_net.prefixlen
        )
        self.ext_gw_rp = await cluster.nbctl.ls_port_add(
            self.ext_switch, ext_ls_rp_name, self.ext_rp
        )

        # Configure physnet.
        self.physnet_port = await cluster.nbctl.ls_port_add(
            self.ext_switch, f'provnet-{self.container}', ip="unknown"
        )
        await cluster.nbctl.ls_port_set_set_type(self.physnet_port, 'localnet')
        await cluster.nbctl.ls_port_set_set_options(
            self.physnet_port,
            f'network_name={cluster.brex_cfg.physical_net}'
        )

        # Route for traffic entering the cluster.
        rp_gw = netaddr.IPAddress(self.gw_net.last - 1)
        await cluster.nbctl.route_add(self.gw_router, cluster.net, str(rp_gw))

        # Default route to get out of cluster via physnet.
        gr_def_gw = netaddr.IPAddress(self.ext_net.last - 2)
        await cluster.nbctl.route_add(self.gw_router, gw=str(gr_def_gw))

        # Force return traffic to return on the same node.
        await cluster.nbctl.run(f'set Logical_Router {self.gw_router.name} '
                                f'options:lb_force_snat_ip={gr_gw}')

        # Route for traffic that needs to exit the cluster
        # (via gw router).
        await cluster.nbctl.route_add(cluster.router, str(self.int_net),
                                      str(gr_gw), policy="src-ip")

        # SNAT traffic leaving the cluster.
        await cluster.nbctl.nat_add(self.gw_router, external_ip=str(gr_gw),
                                    logical_ip=cluster.net)

    @ovn_stats.timeit
    async def provision_port(self, cluster, passive=False):
        name = f'lp-{self.id}-{self.next_lport_index}'
        ip = netaddr.IPAddress(self.int_net.first + self.next_lport_index + 1)
        plen = self.int_net.prefixlen
        gw = netaddr.IPAddress(self.int_net.last - 1)
        ext_gw = netaddr.IPAddress(self.ext_net.last - 2)

        log.info(f'Creating lport {name}')
        lport = await cluster.nbctl.ls_port_add(self.switch, name,
                                                mac=str(RandMac()), ip=ip,
                                                plen=plen, gw=gw,
                                                ext_gw=ext_gw, metadata=self,
                                                passive=passive, security=True)
        self.lports.append(lport)
        self.next_lport_index += 1
        return lport

    @ovn_stats.timeit
    async def unprovision_port(self, cluster, port):
        await cluster.nbctl.ls_port_del(port)
        await self.unbind_port(port)
        self.lports.remove(port)

    @ovn_stats.timeit
    async def provision_load_balancers(self, cluster, ports):
        # Add one port IP as a backend to the cluster load balancer.
        port_ips = (
            f'{port.ip}:{DEFAULT_BACKEND_PORT}'
            for port in ports if port.ip is not None
        )
        cluster_vips = cluster.cluster_cfg.vips.keys()
        await cluster.load_balancer.add_backends_to_vip(port_ips,
                                                        cluster_vips)
        await cluster.load_balancer.add_to_switches([self.switch.name])
        await cluster.load_balancer.add_to_routers([self.gw_router.name])

        # GW Load balancer has no VIPs/backends configured on it, since
        # this load balancer is used for hostnetwork services. We're not
        # using those right now so the load blaancer is empty.
        self.gw_load_balancer = await create_load_balancer(
            f'lb-{self.gw_router.name}', cluster.nbctl)
        await self.gw_load_balancer.add_to_routers([self.gw_router.name])

    @ovn_stats.timeit
    async def bind_port(self, port):
        vsctl = ovn_utils.OvsVsctl(self)
        await vsctl.add_port(port, 'br-int', internal=True, ifaceid=port.name)
        # Skip creating a netns for "passive" ports, we won't be sending
        # traffic on those.
        if not port.passive:
            await vsctl.bind_vm_port(port)

    @ovn_stats.timeit
    async def unbind_port(self, port):
        vsctl = ovn_utils.OvsVsctl(self)
        if not port.passive:
            await vsctl.unbind_vm_port(port)
        await vsctl.del_port(port)

    async def provision_ports(self, cluster, n_ports, passive=False):
        ports = [await self.provision_port(cluster, passive)
                 for i in range(n_ports)]
        for port in ports:
            await self.bind_port(port)
        return ports

    async def run_ping(self, cluster, src, dest):
        log.info(f'Pinging from {src} to {dest}')
        cmd = f'ip netns exec {src} ping -q -c 1 -W 0.1 {dest}'
        start_time = datetime.now()
        while True:
            try:
                await self.run(cmd=cmd, raise_on_error=True)
                break
            except ovn_exceptions.SSHError:
                pass

            duration = (datetime.now() - start_time).seconds
            if (duration > cluster.cluster_cfg.node_timeout_s):
                log.error(f'Timeout waiting for {src} '
                          f'to be able to ping {dest}')
                raise ovn_exceptions.OvnPingTimeoutException()

    @ovn_stats.timeit
    async def ping_port(self, cluster, port, dest=None):
        if not dest:
            dest = port.ext_gw
        await self.run_ping(cluster, port.name, dest)

    @ovn_stats.timeit
    async def ping_external(self, cluster, port):
        await self.run_ping(cluster, 'ext-ns', port.ip)

    async def ping_ports(self, cluster, ports):
        for port in ports:
            await self.ping_port(cluster, port)


ACL_DEFAULT_DENY_PRIO = 1
ACL_DEFAULT_ALLOW_ARP_PRIO = 2
ACL_NETPOL_ALLOW_PRIO = 3
DEFAULT_NS_VIP_SUBNET = netaddr.IPNetwork('30.0.0.0/16')
DEFAULT_VIP_PORT = 80
DEFAULT_BACKEND_PORT = 8080


async def create_namespace(cluster, name):
    ns = Namespace(cluster, name)
    ns.pg_def_deny_igr = \
        await ns.nbctl.port_group_create(f'pg_deny_igr_{name}')
    ns.pg_def_deny_egr = \
        await ns.nbctl.port_group_create(f'pg_deny_egr_{name}')
    ns.pg = await ns.nbctl.port_group_create(f'pg_{name}')
    ns.addr_set = await ns.nbctl.address_set_create(f'as_{name}')
    return ns


class Namespace(object):
    def __init__(self, cluster, name):
        self.cluster = cluster
        self.nbctl = cluster.nbctl
        self.ports = []
        self.enforcing = False
        self.pg_def_deny_igr = None
        self.pg_def_deny_egr = None
        self.pg = None
        self.addr_set = None
        self.sub_as = []
        self.sub_pg = []
        self.load_balancer = None
        self.cluster.n_ns += 1
        self.name = name

    @ovn_stats.timeit
    async def add_ports(self, ports):
        self.ports.extend(ports)
        # Always add port IPs to the address set but not to the PGs.
        # Simulate what OpenShift does, which is: create the port groups
        # when the first network policy is applied.
        await self.nbctl.address_set_add_addrs(self.addr_set,
                                               [str(p.ip) for p in ports])
        if self.enforcing:
            await self.nbctl.port_group_add_ports(self.pg_def_deny_igr, ports)
            await self.nbctl.port_group_add_ports(self.pg_def_deny_egr, ports)
            await self.nbctl.port_group_add_ports(self.pg, ports)

    async def unprovision(self):
        # ACLs are garbage collected by OVSDB as soon as all the records
        # referencing them are removed.
        await self.cluster.unprovision_ports(self.ports)
        await self.nbctl.port_group_del(self.pg_def_deny_igr)
        await self.nbctl.port_group_del(self.pg_def_deny_egr)
        await self.nbctl.port_group_del(self.pg)
        await self.nbctl.address_set_del(self.addr_set)
        for pg in self.sub_pg:
            await self.nbctl.port_group_del(pg)
        for addr_set in self.sub_as:
            await self.nbctl.address_set_del(addr_set)

    async def unprovision_ports(self, ports):
        '''Unprovision a subset of ports in the namespace without having to
        unprovision the entire namespace or any of its network policies.'''

        for port in ports:
            self.ports.remove(port)

        await self.cluster.unprovision_ports(ports)

    async def enforce(self):
        if self.enforcing:
            return
        self.enforcing = True
        await self.nbctl.port_group_add_ports(self.pg_def_deny_igr, self.ports)
        await self.nbctl.port_group_add_ports(self.pg_def_deny_egr, self.ports)
        await self.nbctl.port_group_add_ports(self.pg, self.ports)

    async def create_sub_ns(self, ports):
        n_sub_pgs = len(self.sub_pg)
        suffix = f'{self.name}_{n_sub_pgs}'
        pg = await self.nbctl.port_group_create(f'sub_pg_{suffix}')
        await self.nbctl.port_group_add_ports(pg, ports)
        self.sub_pg.append(pg)
        addr_set = await self.nbctl.address_set_create(f'sub_as_{suffix}')
        await self.nbctl.address_set_add_addrs(addr_set,
                                               [str(p.ip) for p in ports])
        self.sub_as.append(addr_set)
        return n_sub_pgs

    @ovn_stats.timeit
    async def default_deny(self):
        await self.enforce()
        await self.nbctl.acl_add(
            self.pg_def_deny_igr.name,
            'to-lport', ACL_DEFAULT_DENY_PRIO, 'port-group',
            f'ip4.src == \\${self.addr_set.name} && '
            f'outport == @{self.pg_def_deny_igr.name}',
            'drop')
        await self.nbctl.acl_add(
            self.pg_def_deny_egr.name,
            'to-lport', ACL_DEFAULT_DENY_PRIO, 'port-group',
            f'ip4.dst == \\${self.addr_set.name} && '
            f'inport == @{self.pg_def_deny_egr.name}',
            'drop')
        await self.nbctl.acl_add(
            self.pg_def_deny_igr.name,
            'to-lport', ACL_DEFAULT_ALLOW_ARP_PRIO, 'port-group',
            f'outport == @{self.pg_def_deny_igr.name} && arp',
            'allow')
        await self.nbctl.acl_add(
            self.pg_def_deny_egr.name,
            'to-lport', ACL_DEFAULT_ALLOW_ARP_PRIO, 'port-group',
            f'inport == @{self.pg_def_deny_egr.name} && arp',
            'allow')

    @ovn_stats.timeit
    async def allow_within_namespace(self):
        await self.enforce()
        await self.nbctl.acl_add(
            self.pg.name, 'to-lport', ACL_NETPOL_ALLOW_PRIO, 'port-group',
            f'ip4.src == \\${self.addr_set.name} && '
            f'outport == @{self.pg.name}',
            'allow-related'
        )
        await self.nbctl.acl_add(
            self.pg.name, 'to-lport', ACL_NETPOL_ALLOW_PRIO, 'port-group',
            f'ip4.dst == \\${self.addr_set.name} && '
            f'inport == @{self.pg.name}',
            'allow-related'
        )

    @ovn_stats.timeit
    async def allow_cross_namespace(self, ns):
        await self.enforce()
        await self.nbctl.acl_add(
            self.pg.name, 'to-lport', ACL_NETPOL_ALLOW_PRIO, 'port-group',
            f'ip4.src == \\${self.addr_set.name} && '
            f'outport == @{ns.pg.name}',
            'allow-related'
        )
        await self.nbctl.acl_add(
            self.pg.name, 'to-lport', ACL_NETPOL_ALLOW_PRIO, 'port-group',
            f'ip4.dst == \\${ns.addr_set.name} && '
            f'inport == @{self.pg.name}',
            'allow-related'
        )

    @ovn_stats.timeit
    async def allow_sub_namespace(self, src, dst):
        await self.nbctl.acl_add(
            self.pg.name, 'to-lport', ACL_NETPOL_ALLOW_PRIO, 'port-group',
            f'ip4.src == \\${self.sub_as[src].name} && '
            f'outport == @{self.sub_pg[dst].name}',
            'allow-related'
        )
        await self.nbctl.acl_add(
            self.pg.name, 'to-lport', ACL_NETPOL_ALLOW_PRIO, 'port-group',
            f'ip4.dst == \\${self.sub_as[dst].name} && '
            f'inport == @{self.sub_pg[src].name}',
            'allow-related'
        )

    @ovn_stats.timeit
    async def allow_from_external(self, external_ips, include_ext_gw=False):
        await self.enforce()
        # If requested, include the ext-gw of the first port in the namespace
        # so we can check that this rule is enforced.
        if include_ext_gw:
            assert(len(self.ports) > 0)
            external_ips.append(self.ports[0].ext_gw)
        ips = [str(ip) for ip in external_ips]
        await self.nbctl.acl_add(
            self.pg.name, 'to-lport', ACL_NETPOL_ALLOW_PRIO, 'port-group',
            f'ip4.src == {{{",".join(ips)}}} && outport == @{self.pg.name}',
            'allow-related'
        )

    @ovn_stats.timeit
    async def check_enforcing_internal(self):
        # "Random" check that first pod can reach last pod in the namespace.
        if len(self.ports) > 1:
            src = self.ports[0]
            dst = self.ports[-1]
            worker = src.metadata
            await worker.ping_port(self.cluster, src, dst.ip)

    @ovn_stats.timeit
    async def check_enforcing_external(self):
        if len(self.ports) > 0:
            dst = self.ports[0]
            worker = dst.metadata
            await worker.ping_external(self.cluster, dst)

    @ovn_stats.timeit
    async def check_enforcing_cross_ns(self, ns):
        if len(self.ports) > 0 and len(ns.ports) > 0:
            dst = ns.ports[0]
            src = self.ports[0]
            worker = src.metadata
            await worker.ping_port(self.cluster, src, dst.ip)

    async def create_load_balancer(self):
        self.load_balancer = await create_load_balancer(f'lb_{self.name}',
                                                        self.nbctl)

    @ovn_stats.timeit
    async def provision_vips_to_load_balancers(self, backend_lists):
        vip_net = DEFAULT_NS_VIP_SUBNET.next(self.cluster.n_ns)
        n_vips = len(self.load_balancer.vips.keys())
        vip_ip = vip_net.ip.__add__(n_vips + 1)

        vips = {
            f'{vip_ip + i}:{DEFAULT_VIP_PORT}':
                [f'{p.ip}:{DEFAULT_BACKEND_PORT}' for p in ports]
            for i, ports in enumerate(backend_lists)
        }
        await self.load_balancer.add_vips(vips)


class Cluster(object):
    def __init__(self, central_node, worker_nodes, cluster_cfg, brex_cfg):
        # In clustered mode use the first node for provisioning.
        self.central_node = central_node
        self.worker_nodes = worker_nodes
        self.cluster_cfg = cluster_cfg
        self.brex_cfg = brex_cfg
        self.nbctl = ovn_utils.OvnNbctl(self.central_node)
        self.sbctl = ovn_utils.OvnSbctl(self.central_node)
        self.net = cluster_cfg.cluster_net
        self.router = None
        self.load_balancer = None
        self.join_switch = None
        self.last_selected_worker = 0
        self.n_ns = 0

    async def start(self):
        await self.central_node.start(self.cluster_cfg)
        for w in self.worker_nodes:
            await w.start(self.cluster_cfg)
            await w.configure(self.brex_cfg.physical_net)

        if self.cluster_cfg.clustered_db:
            nb_cluster_ips = [str(self.central_node.mgmt_ip),
                              str(self.central_node.mgmt_ip + 1),
                              str(self.central_node.mgmt_ip + 2)]
        else:
            nb_cluster_ips = [str(self.central_node.mgmt_ip)]
        await self.nbctl.start_daemon(nb_cluster_ips,
                                      self.cluster_cfg.enable_ssl)
        await self.nbctl.set_global(
            'use_logical_dp_groups',
            self.cluster_cfg.logical_dp_groups
        )
        await self.nbctl.set_global(
            'northd_probe_interval',
            self.cluster_cfg.northd_probe_interval
        )
        await self.nbctl.set_inactivity_probe(
            self.cluster_cfg.db_inactivity_probe
        )
        await self.sbctl.set_inactivity_probe(
            self.cluster_cfg.db_inactivity_probe
        )

    async def create_cluster_router(self, rtr_name):
        self.router = await self.nbctl.lr_add(rtr_name)

    async def create_cluster_load_balancer(self, lb_name):
        self.load_balancer = await create_load_balancer(lb_name, self.nbctl,
                                                        self.cluster_cfg.vips)
        await self.load_balancer.add_vips(self.cluster_cfg.static_vips)

    async def create_cluster_join_switch(self, sw_name):
        self.join_switch = await self.nbctl.ls_add(sw_name,
                                                   self.cluster_cfg.gw_net)

        lrp_ip = netaddr.IPAddress(self.cluster_cfg.gw_net.last - 1)
        self.join_rp = await self.nbctl.lr_port_add(
            self.router, 'rtr-to-join', RandMac(), lrp_ip,
            self.cluster_cfg.gw_net.prefixlen
        )
        self.join_ls_rp = await self.nbctl.ls_port_add(
            self.join_switch, 'join-to-rtr', self.join_rp
        )

    async def provision_ports(self, n_ports, passive=False):
        ret_list = []
        for _ in range(n_ports):
            worker = self.select_worker_for_port()
            ports = await worker.provision_ports(self, 1, passive)
            ret_list.append(ports[0])
        return ret_list

    async def unprovision_ports(self, ports):
        for port in ports:
            worker = port.metadata
            await worker.unprovision_port(self, port)

    async def ping_ports(self, ports):
        ports_per_worker = defaultdict(list)
        for p in ports:
            ports_per_worker[p.metadata].append(p)
        for w, ports in ports_per_worker.items():
            await w.ping_ports(self, ports)

    @ovn_stats.timeit
    async def provision_vips_to_load_balancers(self, backend_lists):
        n_vips = len(self.load_balancer.vips.keys())
        vip_ip = self.cluster_cfg.vip_subnet.ip.__add__(n_vips + 1)

        vips = {
            f'{vip_ip + i}:{DEFAULT_VIP_PORT}':
                [f'{p.ip}:{DEFAULT_BACKEND_PORT}' for p in ports]
            for i, ports in enumerate(backend_lists)
        }
        await self.load_balancer.add_vips(vips)

    async def unprovision_vips(self):
        await self.load_balancer.clear_vips()
        await self.load_balancer.add_vips(self.cluster_cfg.static_vips)

    def select_worker_for_port(self):
        self.last_selected_worker += 1
        self.last_selected_worker %= len(self.worker_nodes)
        return self.worker_nodes[self.last_selected_worker]

    async def provision_lb(self, lb):
        await lb.add_to_switches([w.switch.name for w in self.worker_nodes])
        await lb.add_to_routers([w.gw_router.name for w in self.worker_nodes])
