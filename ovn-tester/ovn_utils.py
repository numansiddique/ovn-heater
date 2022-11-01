import logging
import netaddr
import select
import ovn_exceptions
from collections import namedtuple
from functools import partial
import ovsdbapp.schema.open_vswitch.impl_idl as ovs_impl_idl
import ovsdbapp.schema.ovn_northbound.impl_idl as nb_impl_idl
import ovsdbapp.schema.ovn_southbound.impl_idl as sb_impl_idl
from ovsdbapp.backend import ovs_idl
from ovsdbapp.backend.ovs_idl import connection
from ovsdbapp.backend.ovs_idl import idlutils
from ovsdbapp.backend.ovs_idl import transaction
from ovsdbapp.backend.ovs_idl import vlog
from ovsdbapp import exceptions as ovsdbapp_exceptions
from ovs import poller


log = logging.getLogger(__name__)

LRouter = namedtuple('LRouter', ['uuid', 'name'])
LRPort = namedtuple('LRPort', ['name', 'mac', 'ip'])
LSwitch = namedtuple('LSwitch', ['uuid', 'name', 'cidr', 'cidr6'])
LSPort = namedtuple(
    'LSPort',
    [
        'name',
        'mac',
        'ip',
        'plen',
        'gw',
        'ext_gw',
        'ip6',
        'plen6',
        'gw6',
        'ext_gw6',
        'metadata',
        'passive',
        'uuid',
    ],
)
PortGroup = namedtuple('PortGroup', ['name'])
AddressSet = namedtuple('AddressSet', ['name'])
LoadBalancer = namedtuple('LoadBalancer', ['name', 'uuid'])
LoadBalancerGroup = namedtuple('LoadBalancerGroup', ['name', 'uuid'])

DEFAULT_CTL_TIMEOUT = 60


MAX_RETRY = 5


DualStackIP = namedtuple('DualStackIP', ['ip4', 'plen4', 'ip6', 'plen6'])


vlog.use_python_logger(max_level=vlog.INFO)

# Under the hood, ovsdbapp uses select.select, but it has a hard-coded limit
# on the number of file descriptors that can be selected from. In large-scale
# tests (500 nodes), we exceed this number and run into issues. By switching to
# select.poll, we do not have this limitation.
poller.SelectPoll = select.poll


class PhysCtl:
    def __init__(self, sb):
        self.sb = sb

    def run(self, cmd="", stdout=None, timeout=DEFAULT_CTL_TIMEOUT):
        self.sb.run(cmd=cmd, stdout=stdout, timeout=timeout)

    def external_host_provision(self, ip, gw, netns='ext-ns'):
        log.info(f'Adding external host on {self.sb.container}')
        cmd = (
            f'ip link add veth0 type veth peer name veth1; '
            f'ip netns add {netns}; '
            f'ip link set netns {netns} dev veth0; '
            f'ip netns exec {netns} ip link set dev veth0 up; '
        )

        if ip.ip4:
            cmd += (
                f'ip netns exec ext-ns ip addr add {ip.ip4}/{ip.plen4}'
                f' dev veth0; '
            )
        if gw.ip4:
            cmd += f'ip netns exec ext-ns ip route add default via {gw.ip4}; '

        if ip.ip6:
            cmd += (
                f'ip netns exec ext-ns ip addr add {ip.ip6}/{ip.plen6}'
                f' dev veth0; '
            )
        if gw.ip6:
            cmd += f'ip netns exec ext-ns ip route add default via {gw.ip6}; '

        cmd += 'ip link set dev veth1 up; ' 'ovs-vsctl add-port br-ex veth1'

        # Run as a single invocation:
        cmd = f'bash -c \'{cmd}\''
        self.run(cmd=cmd)


class DualStackSubnet:
    def __init__(self, n4=None, n6=None):
        self.n4 = n4
        self.n6 = n6

    @classmethod
    def next(cls, n, index=0):
        n4 = n.n4.next(index) if n.n4 else None
        n6 = n.n6.next(index) if n.n6 else None
        return cls(n4, n6)

    def forward(self, index=0):
        if self.n4 and self.n6:
            return DualStackIP(
                netaddr.IPAddress(self.n4.first + index),
                self.n4.prefixlen,
                netaddr.IPAddress(self.n6.first + index),
                self.n6.prefixlen,
            )
        if self.n4 and not self.n6:
            return DualStackIP(
                netaddr.IPAddress(self.n4.first + index),
                self.n4.prefixlen,
                None,
                None,
            )
        if not self.n4 and self.n6:
            return DualStackIP(
                None,
                None,
                netaddr.IPAddress(self.n6.first + index),
                self.n6.prefixlen,
            )
        raise ovn_exceptions.OvnInvalidConfigException("invalid configuration")

    def reverse(self, index=1):
        if self.n4 and self.n6:
            return DualStackIP(
                netaddr.IPAddress(self.n4.last - index),
                self.n4.prefixlen,
                netaddr.IPAddress(self.n6.last - index),
                self.n6.prefixlen,
            )
        if self.n4 and not self.n6:
            return DualStackIP(
                netaddr.IPAddress(self.n4.last - index),
                self.n4.prefixlen,
                None,
                None,
            )
        if not self.n4 and self.n6:
            return DualStackIP(
                None,
                None,
                netaddr.IPAddress(self.n6.last - index),
                self.n6.prefixlen,
            )
        raise ovn_exceptions.OvnInvalidConfigException("invalid configuration")


# This override allows for us to connect to multiple DBs
class Backend(ovs_idl.Backend):
    def __init__(self, connection):
        super(Backend, self).__init__(connection)

    @property
    def ovsdb_connection(self):
        return self._ovsdb_connection

    @ovsdb_connection.setter
    def ovsdb_connection(self, connection):
        if self._ovsdb_connection is None:
            self._ovsdb_connection = connection


class VSIdl(ovs_impl_idl.OvsdbIdl, Backend):
    def __init__(self, connection):
        super(VSIdl, self).__init__(connection)


class OvsVsctl:
    def __init__(self, sb, connection_string, inactivity_probe):
        self.sb = sb
        i = connection.OvsdbIdl.from_server(connection_string, "Open_vSwitch")
        c = connection.Connection(i, inactivity_probe)
        self.idl = VSIdl(c)

    def run(
        self,
        cmd="",
        prefix="ovs-vsctl ",
        stdout=None,
        timeout=DEFAULT_CTL_TIMEOUT,
    ):
        self.sb.run(cmd=prefix + cmd, stdout=stdout, timeout=timeout)

    def add_port(self, port, bridge, internal=True, ifaceid=None):
        name = port.name
        with self.idl.transaction(check_error=True) as txn:
            txn.add(self.idl.add_port(bridge, name))
            if internal:
                txn.add(
                    self.idl.db_set("Interface", name, ("type", "internal"))
                )
            if ifaceid:
                txn.add(
                    self.idl.iface_set_external_id(name, "iface-id", ifaceid)
                )

    def del_port(self, port):
        self.idl.del_port(port.name).execute(check_error=True)

    def bind_vm_port(self, lport):
        cmd = (
            f'ip netns add {lport.name}; '
            f'ip link set {lport.name} netns {lport.name}; '
            f'ip netns exec {lport.name} ip link set {lport.name} '
            f'address {lport.mac}; '
            f'ip netns exec {lport.name} ip link set {lport.name} up'
        )
        if lport.ip:
            cmd += (
                f'; ip netns exec {lport.name} ip addr add '
                f'{lport.ip}/{lport.plen} dev {lport.name}'
            )
            cmd += (
                f'; ip netns exec {lport.name} ip route add '
                f'default via {lport.gw}'
            )
        if lport.ip6:
            cmd += (
                f'; ip netns exec {lport.name} ip addr add '
                f'{lport.ip6}/{lport.plen6} dev {lport.name} nodad'
            )
            cmd += (
                f'; ip netns exec {lport.name} ip route add '
                f'default via {lport.gw6}'
            )
        self.run(cmd, prefix="")

    def unbind_vm_port(self, lport):
        self.run(f'ip netns del {lport.name}', prefix='')


# We have to subclass the base Transaction for NB in order to facilitate the
# "sync" command. This is heavily based on ovsdbapp's OvsVsctlTransaction class
# but with some NB-specific modifications, and removal of some OVS-specific
# assumptions.
class NBTransaction(transaction.Transaction):
    def __init__(
        self,
        api,
        ovsdb_connection,
        timeout=None,
        check_error=False,
        log_errors=True,
        wait_type=None,
        **kwargs,
    ):
        super(NBTransaction, self).__init__(
            api,
            ovsdb_connection,
            timeout=timeout,
            check_error=check_error,
            log_errors=log_errors,
            **kwargs,
        )
        self.wait_type = wait_type.lower() if wait_type else None
        if (
            self.wait_type
            and self.wait_type != "sb"
            and self.wait_type != "hv"
        ):
            log.warning(f"Unrecognized wait type {self.wait_type}. Ignoring")
            self.wait_type = None

    def pre_commit(self, txn):
        if self.wait_type:
            self.api._nb.increment('nb_cfg')

    def post_commit(self, txn):
        super().post_commit(txn)
        try:
            self.do_post_commit(txn)
        except ovsdbapp_exceptions.TimeoutException:
            log.exception("Transaction timed out")

    def do_post_commit(self, txn):
        if not self.wait_type:
            return

        next_cfg = txn.get_increment_new_value()
        while not self.timeout_exceeded():
            self.api.idl.run()
            if self.nb_has_completed(next_cfg):
                break
            self.ovsdb_connection.poller.timer_wait(
                self.time_remaining() * 1000
            )
            self.api.idl.wait(self.ovsdb_connection.poller)
            self.ovsdb_connection.poller.block()
        else:
            raise ovsdbapp_exceptions.TimeoutException(
                commands=self.commands,
                timeout=self.timeout,
                cause='nbctl transaction did not end',
            )

    def nb_has_completed(self, next_cfg):
        if not self.wait_type:
            return True
        elif self.wait_type == "sb":
            cur_cfg = self.api._nb.sb_cfg
        else:  # self.wait_type == "hv":
            cur_cfg = min(self.api._nb.sb_cfg, self.api._nb.hv_cfg)

        return cur_cfg >= next_cfg


class NBIdl(nb_impl_idl.OvnNbApiIdlImpl, Backend):
    def __init__(self, connection):
        super(NBIdl, self).__init__(connection)

    def create_transaction(
        self,
        check_error=False,
        log_errors=True,
        timeout=None,
        wait_type=None,
        **kwargs,
    ):
        # Override of Base API method so we create NBTransactions.
        return NBTransaction(
            self,
            self.ovsdb_connection,
            timeout=timeout,
            check_error=check_error,
            log_errors=log_errors,
            wait_type=wait_type,
            **kwargs,
        )

    @property
    def _nb(self):
        return next(iter(self.db_list_rows('NB_Global').execute()))

    @property
    def _connection(self):
        return next(iter(self.db_list_rows('Connection').execute()))


class UUIDTransactionError(Exception):
    pass


class OvnNbctl:
    def __init__(self, sb, connection_string, inactivity_probe):
        i = connection.OvsdbIdl.from_server(
            connection_string, "OVN_Northbound"
        )
        c = connection.Connection(i, inactivity_probe)
        self.idl = NBIdl(c)

    def uuid_transaction(self, func):
        # Occasionally, due to RAFT leadership changes, a transaction can
        # appear to fail. In reality, they succeeded and the error is spurious.
        # When we encounter this sort of error, the result of the command will
        # not have the UUID of the row. Our strategy is to retry the
        # transaction with may_exist=True so that we can get the UUID.
        for _ in range(MAX_RETRY):
            cmd = func(may_exist=True)
            cmd.execute()
            try:
                return cmd.result.uuid
            except AttributeError:
                continue

        raise UUIDTransactionError("Failed to get UUID from transaction")

    def db_create_transaction(self, table, *, get_func, **columns):
        # db_create does not afford the ability to retry with "may_exist". We
        # therefore need to have a method of ensuring that the value was not
        # actually set in the DB before we can retry the transaction.
        for _ in range(MAX_RETRY):
            cmd = self.idl.db_create(table, **columns)
            cmd.execute()
            try:
                return cmd.result
            except AttributeError:
                cmd = get_func()
                cmd.execute()
                try:
                    return cmd.result
                except AttributeError:
                    continue

        raise UUIDTransactionError("Failed to get UUID from transaction")

    def set_global(self, option, value):
        self.idl.db_set(
            "NB_Global", self.idl._nb.uuid, ("options", {option: str(value)})
        ).execute()

    def set_inactivity_probe(self, value):
        self.idl.db_set(
            "Connection",
            self.idl._connection.uuid,
            ("inactivity_probe", value),
        ).execute()

    def lr_add(self, name):
        log.info(f'Creating lrouter {name}')
        uuid = self.uuid_transaction(partial(self.idl.lr_add, name))
        return LRouter(name=name, uuid=uuid)

    def lr_port_add(self, router, name, mac, dual_ip=None):
        networks = []
        if dual_ip.ip4 and dual_ip.plen4:
            networks.append(f'{dual_ip.ip4}/{dual_ip.plen4}')
        if dual_ip.ip6 and dual_ip.plen6:
            networks.append(f'{dual_ip.ip6}/{dual_ip.plen6}')

        self.idl.lrp_add(router.uuid, name, str(mac), networks).execute()
        return LRPort(name=name, mac=mac, ip=dual_ip)

    def lr_port_set_gw_chassis(self, rp, chassis, priority=10):
        log.info(f'Setting gw chassis {chassis} for router port {rp.name}')
        self.idl.lrp_set_gateway_chassis(rp.name, chassis, priority).execute()

    def ls_add(self, name, net_s):
        log.info(f'Creating lswitch {name}')
        uuid = self.uuid_transaction(partial(self.idl.ls_add, name))
        return LSwitch(
            name=name,
            cidr=net_s.n4,
            cidr6=net_s.n6,
            uuid=uuid,
        )

    def ls_port_add(
        self,
        lswitch,
        name,
        router_port=None,
        mac=None,
        ip=None,
        gw=None,
        ext_gw=None,
        metadata=None,
        passive=False,
        security=False,
        localnet=False,
    ):
        columns = dict()
        if router_port:
            columns["type"] = "router"
            columns["addresses"] = "router"
            columns["options"] = {"router-port": router_port.name}
        elif mac or ip or localnet:
            addresses = []
            if mac:
                addresses.append(mac)
            if localnet:
                addresses.append("unknown")
            if ip and ip.ip4:
                addresses.append(str(ip.ip4))
            if ip and ip.ip6:
                addresses.append(str(ip.ip6))

            addresses = " ".join(addresses)

            columns["addresses"] = addresses
            if security:
                columns["port_security"] = addresses

        uuid = self.uuid_transaction(
            partial(self.idl.lsp_add, lswitch.uuid, name, **columns)
        )

        ip4 = "unknown" if localnet else ip.ip4 if ip else None
        plen4 = ip.plen4 if ip else None
        gw4 = gw.ip4 if gw else None
        ext_gw4 = ext_gw.ip4 if ext_gw else None

        ip6 = ip.ip6 if ip else None
        plen6 = ip.plen6 if ip else None
        gw6 = gw.ip6 if gw else None
        ext_gw6 = ext_gw.ip6 if ext_gw else None

        return LSPort(
            name=name,
            mac=mac,
            ip=ip4,
            plen=plen4,
            gw=gw4,
            ext_gw=ext_gw4,
            ip6=ip6,
            plen6=plen6,
            gw6=gw6,
            ext_gw6=ext_gw6,
            metadata=metadata,
            passive=passive,
            uuid=uuid,
        )

    def ls_port_del(self, port):
        self.idl.lsp_del(port.name).execute()

    def ls_port_set_set_options(self, port, options):
        opts = dict(
            (k, v)
            for k, v in (element.split("=") for element in options.split())
        )
        self.idl.lsp_set_options(port.name, **opts).execute()

    def ls_port_set_set_type(self, port, lsp_type):
        self.idl.lsp_set_type(port.name, lsp_type).execute()

    def port_group_create(self, name):
        self.idl.pg_add(name).execute()
        return PortGroup(name=name)

    def port_group_add(self, pg, lport):
        self.idl.pg_add_ports(pg.name, lport.uuid).execute()

    def port_group_add_ports(self, pg, lports):
        MAX_PORTS_IN_BATCH = 500
        for i in range(0, len(lports), MAX_PORTS_IN_BATCH):
            lports_slice = lports[i : i + MAX_PORTS_IN_BATCH]
            port_uuids = [p.uuid for p in lports_slice]
            self.idl.pg_add_ports(pg.name, port_uuids).execute()

    def port_group_del(self, pg):
        self.idl.pg_del(pg.name).execute()

    def address_set_create(self, name):
        self.idl.address_set_add(name).execute()
        return AddressSet(name=name)

    def address_set_add(self, addr_set, addr):
        self.idl.address_set_add_addresses(addr_set.name, addr)

    def address_set_add_addrs(self, addr_set, addrs):
        MAX_ADDRS_IN_BATCH = 500
        for i in range(0, len(addrs), MAX_ADDRS_IN_BATCH):
            addrs_slice = [str(a) for a in addrs[i : i + MAX_ADDRS_IN_BATCH]]
            self.idl.address_set_add_addresses(
                addr_set.name, addrs_slice
            ).execute()

    def address_set_remove(self, addr_set, addr):
        self.idl.address_set_remove_addresses(addr_set.name, addr)

    def address_set_del(self, addr_set):
        self.idl.address_set_del(addr_set.name)

    def acl_add(
        self,
        name="",
        direction="from-lport",
        priority=100,
        entity="switch",
        match="",
        verdict="allow",
    ):
        if entity == "switch":
            self.idl.acl_add(name, direction, priority, match, verdict)
        else:  # "port-group"
            self.idl.pg_acl_add(name, direction, priority, match, verdict)

    def route_add(self, router, network, gw, policy="dst-ip"):
        if network.n4 and gw.ip4:
            self.idl.lr_route_add(
                router.uuid, network.n4, gw.ip4, policy=policy
            ).execute()
        if network.n6 and gw.ip6:
            self.idl.lr_route_add(
                router.uuid, network.n6, gw.ip6, policy=policy
            ).execute()

    def nat_add(self, router, external_ip, logical_net, nat_type="snat"):
        if external_ip.ip4 and logical_net.n4:
            self.idl.lr_nat_add(
                router.uuid, nat_type, external_ip.ip4, logical_net.n4
            ).execute()
        if external_ip.ip6 and logical_net.n6:
            self.idl.lr_nat_add(
                router.uuid, nat_type, external_ip.ip6, logical_net.n6
            ).execute()

    def create_lb(self, name, protocol):
        lb_name = f"{name}-{protocol}"
        # We can't use ovsdbapp's lb_add here because it is not possible to
        # create a load balancer with no VIPs.
        uuid = self.db_create_transaction(
            "Load_Balancer",
            name=lb_name,
            protocol=protocol,
            get_func=partial(self.idl.lb_get, lb_name),
        )
        return LoadBalancer(name=lb_name, uuid=uuid)

    def create_lbg(self, name):
        uuid = self.db_create_transaction(
            "Load_Balancer_Group",
            name=name,
            get_func=partial(
                self.idl.db_get, "Load_Balancer_Group", name, "uuid"
            ),
        )
        return LoadBalancerGroup(name=name, uuid=uuid)

    def lbg_add_lb(self, lbg, lb):
        self.idl.db_add(
            "Load_Balancer_Group", lbg.uuid, "load_balancer", lb.uuid
        ).execute()

    def ls_add_lbg(self, ls, lbg):
        self.idl.db_add(
            "Logical_Switch", ls.uuid, "load_balancer_group", lbg.uuid
        ).execute()

    def lr_add_lbg(self, lr, lbg):
        self.idl.db_add(
            "Logical_Router", lr.uuid, "load_balancer_group", lbg.uuid
        ).execute()

    def lr_set_options(self, router, options):
        str_options = dict((k, str(v)) for k, v in options.items())
        self.idl.db_set(
            "Logical_Router", router.uuid, ("options", str_options)
        ).execute()

    def lb_set_vips(self, lb, vips):
        vips = dict((k, ",".join(v)) for k, v in vips.items())
        self.idl.db_set("Load_Balancer", lb.uuid, ("vips", vips)).execute()

    def lb_clear_vips(self, lb):
        self.idl.db_clear("Load_Balancer", lb.uuid, "vips").execute()

    def lb_add_to_routers(self, lb, routers):
        with self.idl.transaction(check_error=True) as txn:
            for r in routers:
                txn.add(self.idl.lr_lb_add(r, lb.uuid))

    def lb_add_to_switches(self, lb, switches):
        with self.idl.transaction(check_error=True) as txn:
            for s in switches:
                txn.add(self.idl.ls_lb_add(s, lb.uuid))

    def lb_remove_from_routers(self, lb, routers):
        with self.idl.transaction(check_error=True) as txn:
            for r in routers:
                txn.add(self.idl.lr_lb_del(r, lb.uuid))

    def lb_remove_from_switches(self, lb, switches):
        with self.idl.transaction(check_error=True) as txn:
            for s in switches:
                txn.add(self.idl.ls_lb_del(s, lb.uuid))

    def sync(self, wait="hv", timeout=DEFAULT_CTL_TIMEOUT):
        with self.idl.transaction(
            check_error=True, timeout=timeout, wait_type=wait
        ):
            pass


class BaseOvnSbIdl(connection.OvsdbIdl):
    schema = "OVN_Southbound"

    @classmethod
    def from_server(cls, connection_string):
        helper = idlutils.get_schema_helper(connection_string, cls.schema)
        helper.register_table('Chassis')
        helper.register_table('Connection')
        return cls(connection_string, helper)


class SBIdl(sb_impl_idl.OvnSbApiIdlImpl, Backend):
    def __init__(self, connection):
        super(SBIdl, self).__init__(connection)

    @property
    def _connection(self):
        # Shortcut to retrieve the lone Connection record. This is used by
        # NBTransaction for synchronization purposes.
        return next(iter(self.db_list_rows('Connection').execute()))


class OvnSbctl:
    def __init__(self, sb, connection_string, inactivity_probe):
        i = BaseOvnSbIdl.from_server(connection_string)
        c = connection.Connection(i, inactivity_probe)
        self.idl = SBIdl(c)

    def set_inactivity_probe(self, value):
        self.idl.db_set(
            "Connection",
            self.idl._connection.uuid,
            ("inactivity_probe", value),
        ).execute()

    def chassis_bound(self, chassis=""):
        cmd = self.idl.db_find_rows("Chassis", ("name", "=", chassis))
        cmd.execute()
        return len(cmd.result) == 1
