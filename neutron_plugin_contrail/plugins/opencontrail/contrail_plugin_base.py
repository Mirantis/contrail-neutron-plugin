# Copyright 2014 Juniper Networks.  All rights reserved.
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
#
# @author: Hampapur Ajay, Praneet Bachheti, Rudra Rugge, Atul Moghe

import os.path as path
from urlparse import urlparse

try:
    from neutron_lib.plugins import directory
except ImportError:
    directory = None
from neutron import manager as neutron_manager
from neutron._i18n import _
try:
    from neutron.api.v2.attributes import ATTR_NOT_SPECIFIED
except:
    from neutron_lib.constants import ATTR_NOT_SPECIFIED
try:
    from neutron.common.exceptions import ServiceUnavailable
except ImportError:
    from neutron_lib.exceptions import ServiceUnavailable
try:
    from neutron.common.exceptions import InvalidInput
except ImportError:
    from neutron_lib.exceptions import InvalidInput
try:
    from neutron.common.exceptions import NeutronException
except ImportError:
    from neutron_lib.exceptions import NeutronException
from neutron.common import exceptions as neutron_exc
try:
    from neutron_lib import exceptions as neutron_lib_exc
except ImportError:
    neutron_lib_exc = None
from neutron.common.config import cfg
from neutron.db import portbindings_base
from neutron.db import quota_db  # noqa
from neutron.extensions import allowedaddresspairs
from neutron.extensions import external_net
from neutron.extensions import l3
try:
    from neutron_lib.api.definitions import portbindings
except ImportError:
    from neutron.extensions import portbindings
from neutron.extensions import securitygroup
from neutron_plugin_contrail.extensions import serviceinterface
from neutron_plugin_contrail.extensions import vfbinding
from neutron_plugin_contrail.extensions import baremetal_vif
from neutron import neutron_plugin_base_v2
try:
    from neutron.openstack.common import importutils
except ImportError:
    from oslo_utils import importutils

try:
    from neutron.openstack.common import log as logging
except ImportError:
    from oslo_log import log as logging


# Constant for max length of network interface names
# eg 'bridge' in the Network class or 'devname' in
# the VIF class
NIC_NAME_LEN = 14

VIF_TYPE_VROUTER = 'vrouter'

LOG = logging.getLogger(__name__)

NEUTRON_CONTRAIL_PREFIX = 'NEUTRON'

vnc_opts = [
    cfg.StrOpt('api_server_ip', default='127.0.0.1',
               help='IP address to connect to VNC controller'),
    cfg.StrOpt('api_server_port', default='8082',
               help='Port to connect to VNC controller'),
    cfg.DictOpt('contrail_extensions',
                default={'contrail': None,
                         'service-interface': None,
                         'vf-binding': None},
                help='Enable Contrail extensions(policy, ipam)'),
    cfg.BoolOpt('use_ssl', default=False,
               help='Use SSL to connect with VNC controller'),
    cfg.BoolOpt('insecure', default=False,
               help='Insecurely connect to VNC controller'),
    cfg.StrOpt('certfile', default='',
               help='certfile to connect securely to VNC controller'),
    cfg.StrOpt('keyfile', default='',
               help='keyfile to connect securely to  VNC controller'),
    cfg.StrOpt('cafile', default='',
               help='cafile to connect securely to VNC controller'),
    cfg.StrOpt('auth_token_url',
               help='Full URL path to request Keystone tokens. This should '
                    'not be use and determined from keystone_authtoken '
                    'configuration section.'),
]

analytics_opts = [
    cfg.StrOpt('analytics_api_ip', default='127.0.0.1',
               help='IP address to connect to VNC collector'),
    cfg.StrOpt('analytics_api_port', default='8081',
               help='Port to connect to VNC collector'),
]

vrouter_opts = [
    cfg.StrOpt('vhostuser_sockets_dir', default='/var/run/vrouter',
               help='Path to dir where vhostuser socket are placed'),
]


def _raise_contrail_error(info, obj_name):
        exc_name = info.get('exception')
        if exc_name:
            LOG.exception(str(exc_name) + str(info) + str(obj_name))
            if exc_name == 'BadRequest' and 'resource' not in info:
                info['resource'] = obj_name
            if exc_name == 'VirtualRouterNotFound':
                raise HttpResponseError(info)
            if hasattr(neutron_exc, exc_name):
                raise getattr(neutron_exc, exc_name)(**info)
            if hasattr(l3, exc_name):
                raise getattr(l3, exc_name)(**info)
            if hasattr(securitygroup, exc_name):
                raise getattr(securitygroup, exc_name)(**info)
            if hasattr(allowedaddresspairs, exc_name):
                raise getattr(allowedaddresspairs, exc_name)(**info)
            if neutron_lib_exc and hasattr(neutron_lib_exc, exc_name):
                raise getattr(neutron_lib_exc, exc_name)(**info)
        raise NeutronException(**info)


def get_keystone_info():
    defaults = ("http", "127.0.0.1", "35357")
    try:
        urlparts = urlparse(cfg.CONF.keystone_authtoken.auth_url)
        protocol = urlparts.scheme
        try:
            host, port = urlparts.netloc.split(':')
        except ValueError:
            host, port = urlparts.netloc.split(':') + [defaults[2]]
    except cfg.NoSuchOptError:
        protocol, host, port = defaults

    try:
        auth_host = cfg.CONF.keystone_authtoken.auth_host
    except cfg.NoSuchOptError:
        auth_host = host

    try:
        auth_protocol = cfg.CONF.keystone_authtoken.auth_protocol
    except cfg.NoSuchOptError:
        auth_protocol = protocol

    try:
        auth_port = cfg.CONF.keystone_authtoken.auth_port
    except cfg.NoSuchOptError:
        auth_port = port

    return (auth_protocol, auth_host, auth_port)


def get_keystone_auth_info():
    try:
        admin_user = cfg.CONF.keystone_authtoken.username
    except cfg.NoSuchOptError:
        admin_user = cfg.CONF.keystone_authtoken.admin_user
    try:
        admin_password = cfg.CONF.keystone_authtoken.password
    except cfg.NoSuchOptError:
        admin_password = cfg.CONF.keystone_authtoken.admin_password
    try:
        admin_tenant_name = cfg.CONF.keystone_authtoken.project_name
    except cfg.NoSuchOptError:
        admin_tenant_name = cfg.CONF.keystone_authtoken.admin_tenant_name

    return (admin_user, admin_password, admin_tenant_name)


class InvalidContrailExtensionError(ServiceUnavailable):
    message = _("Invalid Contrail Extension: %(ext_name) %(ext_class)")


class HttpResponseError(Exception):
    def __init__(self, resp_info):
        self.response_info = resp_info


# Monkey patch Neutron manager method to load extension
def _monkey_patched_neutron_manager_load_service_plugins(n_manager):
    """Loads service plugins.

    Starts from the core plugin and checks if it supports
    advanced services then loads classes provided in configuration.
    """
    plugin_providers = cfg.CONF.service_plugins
    plugin_providers.extend(n_manager._get_default_service_plugins())
    LOG.debug("Loading service plugins: %s", plugin_providers)
    for provider in plugin_providers:
        if provider == '':
            continue

        LOG.info("Loading Plugin: %s", provider)
        if provider in n_manager._get_default_service_plugins():
            try:
                plugin_inst = n_manager._get_plugin_instance(
                    'neutron.service_plugins.contrail', provider)
            except ImportError:
                continue
        else:
            plugin_inst = n_manager._get_plugin_instance(
                'neutron.service_plugins', provider)
        # only one implementation of svc_type allowed
        # specifying more than one plugin
        # for the same type is a fatal exception
        # TODO(armax): simplify this by moving the conditional into the
        # directory itself.
        plugin_type = plugin_inst.get_plugin_type()
        if directory.get_plugin(plugin_type):
            raise ValueError(_("Multiple plugins for service "
                               "%s were configured") % plugin_type)

        directory.add_plugin(plugin_type, plugin_inst)

        # search for possible agent notifiers declared in service plugin
        # (needed by agent management extension)
        plugin = directory.get_plugin()
        if (hasattr(plugin, 'agent_notifiers') and
                hasattr(plugin_inst, 'agent_notifiers')):
            plugin.agent_notifiers.update(plugin_inst.agent_notifiers)

        LOG.debug("Successfully loaded %(type)s plugin. "
                  "Description: %(desc)s",
                  {"type": plugin_type,
                   "desc": plugin_inst.get_plugin_description()})


class NeutronPluginContrailCoreBase(neutron_plugin_base_v2.NeutronPluginBaseV2,
                                    securitygroup.SecurityGroupPluginBase,
                                    portbindings_base.PortBindingBaseMixin,
                                    external_net.External_net,
                                    serviceinterface.Serviceinterface,
                                    vfbinding.Vfbinding,
                                    baremetal_vif.BaremetalVIF):

    supported_extension_aliases = [
        "security-group",
        "router",
        "port-security",
        "binding",
        "agent",
        "quotas",
        "external-net",
        "allowed-address-pairs",
        "extra_dhcp_opt",
        "provider",
    ]

    __native_bulk_support = False

    # TODO(md): This should be added in upstream (neutron portbindings
    # extension) instead of patching it here. This constants are in newer
    # versions of neutron, but not in the Kilo verion.
    portbindings.__dict__['VIF_TYPE_VHOST_USER'] = 'vhostuser'

    def _parse_class_args(self):
        """Parse the contrailplugin.ini file.

        Opencontrail supports extension such as ipam, policy, these extensions
        can be configured in the plugin configuration file as shown below.
        Plugin then loads the specified extensions.
        contrail_extensions=ipam:<classpath>,policy:<classpath>
        """

        contrail_extensions = cfg.CONF.APISERVER.contrail_extensions
        # If multiple class specified for same extension, last one will win
        # according to DictOpt behavior
        for ext_name, ext_class in contrail_extensions.items():
            try:
                if not ext_class or ext_class == 'None':
                    self.supported_extension_aliases.append(ext_name)
                    continue
                ext_class = importutils.import_class(ext_class)
                ext_instance = ext_class()
                ext_instance.set_core(self)
                for method in dir(ext_instance):
                    for prefix in ['get', 'update', 'delete', 'create']:
                        if method.startswith('%s_' % prefix):
                            setattr(self, method,
                                    ext_instance.__getattribute__(method))
                self.supported_extension_aliases.append(ext_name)
            except Exception:
                LOG.exception(_("Contrail Backend Error"))
                # Converting contrail backend error to Neutron Exception
                raise InvalidContrailExtensionError(
                    ext_name=ext_name, ext_class=ext_class)
        self._build_auth_details()

    def _build_auth_details(self):
        pass

    def __init__(self):
        if directory is not None:
            neutron_manager.NeutronManager._load_service_plugins =\
                _monkey_patched_neutron_manager_load_service_plugins
        super(NeutronPluginContrailCoreBase, self).__init__()
        if hasattr(portbindings_base, 'register_port_dict_function'):
            portbindings_base.register_port_dict_function()
        cfg.CONF.register_opts(vnc_opts, 'APISERVER')
        cfg.CONF.register_opts(analytics_opts, 'COLLECTOR')
        cfg.CONF.register_opts(vrouter_opts, 'VROUTER')
        self._parse_class_args()

    def get_agents(self, context, filters=None, fields=None):
        # This method is implemented so that horizon is happy
        return []

    def _create_resource(self, res_type, context, res_data):
        pass

    def _get_resource(self, res_type, context, id, fields):
        pass

    def _update_resource(self, res_type, context, id, res_data):
        pass

    def _delete_resource(self, res_type, context, id):
        pass

    def _list_resource(self, res_type, context, filters, fields):
        pass

    def _count_resource(self, res_type, context, filters):
        pass

    def _get_network(self, context, id, fields=None):
        return self._get_resource('network', context, id, fields)

    def create_network(self, context, network):
        """Creates a new Virtual Network."""
        return self._create_resource('network', context, network)

    def get_network(self, context, network_id, fields=None):
        """Get the attributes of a particular Virtual Network."""

        return self._get_network(context, network_id, fields)

    def update_network(self, context, network_id, network):
        """Updates the attributes of a particular Virtual Network."""

        return self._update_resource('network', context, network_id,
                                     network)

    def delete_network(self, context, network_id):
        """Creates a new Virtual Network.

        Deletes the network with the specified network identifier
        belonging to the specified tenant.
        """

        self._delete_resource('network', context, network_id)

    def get_networks(self, context, filters=None, fields=None):
        """Get the list of Virtual Networks."""

        return self._list_resource('network', context, filters,
                                   fields)

    def get_networks_count(self, context, filters=None):
        """Get the count of Virtual Network."""

        networks_count = self._count_resource('network', context, filters)
        return networks_count['count']

    def create_subnet(self, context, subnet):
        """Creates a new subnet, and assigns it a symbolic name."""

        if subnet['subnet']['gateway_ip'] is None:
            gateway = '0.0.0.0'
            if subnet['subnet']['ip_version'] == 6:
                gateway = '::'
            subnet['subnet']['gateway_ip'] = gateway

        if subnet['subnet']['host_routes'] != ATTR_NOT_SPECIFIED:
            if (len(subnet['subnet']['host_routes']) >
                    cfg.CONF.max_subnet_host_routes):
                raise neutron_exc.HostRoutesExhausted(subnet_id=subnet[
                    'subnet'].get('id', _('new subnet')),
                    quota=cfg.CONF.max_subnet_host_routes)

        subnet_created = self._create_resource('subnet', context, subnet)
        return self._make_subnet_dict(subnet_created)

    def _make_subnet_dict(self, subnet):
        return subnet

    def _get_subnet(self, context, subnet_id, fields=None):
        subnet = self._get_resource('subnet', context, subnet_id, fields)
        return self._make_subnet_dict(subnet)

    def get_subnet(self, context, subnet_id, fields=None):
        """Get the attributes of a particular subnet."""

        return self._get_subnet(context, subnet_id, fields)

    def update_subnet(self, context, subnet_id, subnet):
        """Updates the attributes of a particular subnet."""

        subnet = self._update_resource('subnet', context, subnet_id, subnet)
        return self._make_subnet_dict(subnet)

    def delete_subnet(self, context, subnet_id):
        """
        Deletes the subnet with the specified subnet identifier
        belonging to the specified tenant.
        """

        self._delete_resource('subnet', context, subnet_id)

    def get_subnets(self, context, filters=None, fields=None):
        """Get the list of subnets."""

        return [self._make_subnet_dict(s)
                for s in self._list_resource(
                    'subnet', context, filters, fields)]

    def get_subnets_count(self, context, filters=None):
        """Get the count of subnets."""

        subnets_count = self._count_resource('subnet', context, filters)
        return subnets_count['count']

    def _extend_port_dict_security_group(self, port_res, port_db):
        # Security group bindings will be retrieved from the sqlalchemy
        # model. As they're loaded eagerly with ports because of the
        # joined load they will not cause an extra query.
        port_res[securitygroup.SECURITYGROUPS] = port_db.get(
            'security_groups', []) or []
        return port_res

    def _make_port_dict(self, port, fields=None):
        """filters attributes of a port based on fields."""

        if portbindings.VIF_TYPE in port and \
            port[portbindings.VIF_TYPE] == portbindings.VIF_TYPE_VHOST_USER:
            vhostuser = True
        else:
            vhostuser = False

        if not fields:
            port.update(self.base_binding_dict)
        else:
            for key in self.base_binding_dict:
                if key in fields:
                    port[key] = self.base_binding_dict[key]

        # Update bindings for vhostuser vif support
        if vhostuser:
            self._update_vhostuser_cfg_for_port(port)

        return port

    def _get_port(self, context, id, fields=None):
        return self._get_resource('port', context, id, fields)

    def _update_ips_for_port(self, context, network_id, port_id, original_ips,
                             new_ips):
        """Add or remove IPs from the port."""

        # These ips are still on the port and haven't been removed
        prev_ips = []

        # the new_ips contain all of the fixed_ips that are to be updated
        if len(new_ips) > cfg.CONF.max_fixed_ips_per_port:
            msg = _('Exceeded maximim amount of fixed ips per port')
            raise InvalidInput(error_message=msg)

        # Remove all of the intersecting elements
        for original_ip in original_ips[:]:
            for new_ip in new_ips[:]:
                if ('ip_address' in new_ip and
                        original_ip['ip_address'] == new_ip['ip_address']):
                    original_ips.remove(original_ip)
                    new_ips.remove(new_ip)
                    prev_ips.append(original_ip)

        return new_ips, prev_ips

    def _get_vrouter_config(self, context, id, fields=None):
        return self._get_resource('virtual_router', context, id, fields)

    def _list_vrouters(self, context, filters=None, fields=None):
        return self._list_resource('virtual_router', context, filters, fields)

    def create_port(self, context, port):
        """Creates a port on the specified Virtual Network."""

        port = self._create_resource('port', context, port)

        if self.is_port_baremetal(port):
            self.bind_baremetal_port(port)

        return port

    def get_port(self, context, port_id, fields=None):
        """Get the attributes of a particular port."""

        return self._get_port(context, port_id, fields)

    def update_port(self, context, port_id, port):
        """Updates a port.

        Updates the attributes of a port on the specified Virtual
        Network.
        """

        original = self._get_port(context, port_id)
        if 'fixed_ips' in port['port']:
            added_ips, prev_ips = self._update_ips_for_port(
                context, original['network_id'], port_id,
                original['fixed_ips'], port['port']['fixed_ips'])
            port['port']['fixed_ips'] = prev_ips + added_ips

        if self.is_port_baremetal(port['port']):
            port['port']['id'] = port_id
            if self.should_bind_port(port['port']):
                self.bind_baremetal_port(port['port'])
            else:
                self.unbind_baremetal_port(port['port'])

        return self._update_resource('port', context, port_id, port)

    def delete_port(self, context, port_id):
        """Deletes a port.

        Deletes a port on a specified Virtual Network,
        if the port contains a remote interface attachment,
        the remote interface is first un-plugged and then the port
        is deleted.
        """

        original = self._get_port(context, port_id)

        if self.is_port_baremetal(original):
            self.unbind_baremetal_port(original)

        self._delete_resource('port', context, port_id)

    def get_ports(self, context, filters=None, fields=None):
        """Get all ports.

        Retrieves all port identifiers belonging to the
        specified Virtual Network with the specfied filter.
        """

        return self._list_resource('port', context, filters, fields)

    def get_ports_count(self, context, filters=None):
        """Get the count of ports."""

        ports_count = self._count_resource('port', context, filters)
        return ports_count['count']

    # Router API handlers
    def create_router(self, context, router):
        """Creates a router.

        Creates a new Logical Router, and assigns it
        a symbolic name.
        """

        return self._create_resource('router', context, router)

    def get_router(self, context, router_id, fields=None):
        """Get the attributes of a router."""

        return self._get_resource('router', context, router_id, fields)

    def update_router(self, context, router_id, router):
        """Updates the attributes of a router."""

        return self._update_resource('router', context, router_id,
                                     router)

    def delete_router(self, context, router_id):
        """Deletes a router."""

        self._delete_resource('router', context, router_id)

    def get_routers(self, context, filters=None, fields=None):
        """Retrieves all router identifiers."""

        return self._list_resource('router', context, filters, fields)

    def get_routers_count(self, context, filters=None):
        """Get the count of routers."""

        routers_count = self._count_resource('router', context, filters)
        return routers_count['count']

    def add_router_interface(self, context, router_id, interface_info):
        pass

    def remove_router_interface(self, context, router_id, interface_info):
        pass

    # Floating IP API handlers
    def create_floatingip(self, context, floatingip):
        """Creates a floating IP."""

        return self._create_resource('floatingip', context, floatingip)

    def update_floatingip(self, context, fip_id, floatingip):
        """Updates the attributes of a floating IP."""

        return self._update_resource('floatingip', context, fip_id,
                                     floatingip)

    def get_floatingip(self, context, fip_id, fields=None):
        """Get the attributes of a floating ip."""

        return self._get_resource('floatingip', context, fip_id, fields)

    def delete_floatingip(self, context, fip_id):
        """Deletes a floating IP."""

        self._delete_resource('floatingip', context, fip_id)

    def get_floatingips(self, context, filters=None, fields=None):
        """Retrieves all floating ips identifiers."""

        return self._list_resource('floatingip', context, filters, fields)

    def get_floatingips_count(self, context, filters=None):
        """Get the count of floating IPs."""

        fips_count = self._count_resource('floatingip', context, filters)
        return fips_count['count']

    # Security Group handlers
    def create_security_group(self, context, security_group):
        """Creates a Security Group."""

        return self._create_resource('security_group', context,
                                     security_group)

    def get_security_group(self, context, sg_id, fields=None, tenant_id=None):
        """Get the attributes of a security group."""

        return self._get_resource('security_group', context, sg_id, fields)

    def update_security_group(self, context, sg_id, security_group):
        """Updates the attributes of a security group."""

        return self._update_resource('security_group', context, sg_id,
                                     security_group)

    def delete_security_group(self, context, sg_id):
        """Deletes a security group."""

        self._delete_resource('security_group', context, sg_id)

    def get_security_groups(self, context, filters=None, fields=None,
                            sorts=None, limit=None, marker=None,
                            page_reverse=False):
        """Retrieves all security group identifiers."""

        return self._list_resource('security_group', context,
                                   filters, fields)

    def get_security_groups_count(self, context, filters=None):
        return 0

    def get_security_group_rules_count(self, context, filters=None):
        return 0

    def create_security_group_rule(self, context, security_group_rule):
        """Creates a security group rule."""

        return self._create_resource('security_group_rule', context,
                                     security_group_rule)

    def delete_security_group_rule(self, context, sg_rule_id):
        """Deletes a security group rule."""

        self._delete_resource('security_group_rule', context, sg_rule_id)

    def get_security_group_rule(self, context, sg_rule_id, fields=None):
        """Get the attributes of a security group rule."""

        return self._get_resource('security_group_rule', context,
                                  sg_rule_id, fields)

    def get_security_group_rules(self, context, filters=None, fields=None,
                                 sorts=None, limit=None, marker=None,
                                 page_reverse=False):
        """Retrieves all security group rules."""

        return self._list_resource('security_group_rule', context,
                                   filters, fields)
