from collections import namedtuple
from ovn_context import Context, get_current_iteration
from ovn_ext_cmd import ExtCmd
from ovn_workload import create_namespace

NpCrossNsCfg = namedtuple('NpCrossNsCfg',
                          ['n_ns',
                           'pods_ns_ratio',
                           'queries_per_second'])


class NetpolCrossNs(ExtCmd):
    def __init__(self, config, central_node, worker_nodes):
        super(NetpolCrossNs, self).__init__(
                config, central_node, worker_nodes)
        test_config = config.get('netpol_cross', dict())
        self.config = NpCrossNsCfg(
            n_ns=test_config.get('n_ns', 0),
            pods_ns_ratio=test_config.get('pods_ns_ratio', 0),
            queries_per_second=test_config.get('queries_per_second', 20),
        )

    async def run(self, ovn, global_cfg):
        all_ns = []

        with Context('netpol_cross_ns_startup', brief_report=True) as ctx:
            ports = await ovn.provision_ports(
                    self.config.pods_ns_ratio*self.config.n_ns)
            for i in range(self.config.n_ns):
                ns = await create_namespace(ovn,
                                            f'NS_netpol_cross_ns_startup_{i}')
                await ns.add_ports(ports[i*self.config.pods_ns_ratio:
                                   (i + 1) * self.config.pods_ns_ratio])
                await ns.default_deny()
                all_ns.append(ns)

        with Context('netpol_cross_ns', self.config.n_ns, test=self) as ctx:
            await ctx.qps_test(self.config.queries_per_second,
                               self.tester, all_ns)

        if not global_cfg.cleanup:
            return
        with Context('netpol_cross_ns_cleanup', brief_report=True) as ctx:
            for ns in all_ns:
                await ns.unprovision()

    async def tester(self, all_ns):
        iter_num = get_current_iteration().num
        ns = all_ns[iter_num]
        ext_ns = all_ns[(iter_num + 1) % self.config.n_ns]
        await ns.allow_cross_namespace(ext_ns)
        await ns.check_enforcing_cross_ns(ext_ns)
