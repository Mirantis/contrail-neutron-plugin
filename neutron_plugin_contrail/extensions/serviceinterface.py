try:
    from neutron.api.v2.attributes import ATTR_NOT_SPECIFIED
except Exception:
    from neutron_lib.constants import ATTR_NOT_SPECIFIED
try:
    from neutron.api.extensions import ExtensionDescriptor
except ImportError:
    from neutron_lib.api.extensions import ExtensionDescriptor

EXTENDED_ATTRIBUTES_2_0 = {
    'ports': {
        'binding:service_interface_type': {'allow_post': True,
                             'allow_put': False,
                             'default': ATTR_NOT_SPECIFIED,
                             'is_visible': True},
    },
}


class Serviceinterface(ExtensionDescriptor):

    @classmethod
    def get_name(cls):
        return "service-interface"

    @classmethod
    def get_alias(cls):
        return "service-interface"

    @classmethod
    def get_description(cls):
        return ("Service Interface")

    @classmethod
    def get_namespace(cls):
        return "http://docs.openstack.org/TODO"

    @classmethod
    def get_updated(cls):
        return "2014-08-12T10:00:00-00:00"

    @classmethod
    def get_resources(cls):
        """ Returns Ext Resources """
        exts = []
        return exts

    def get_extended_resources(self, version):
        if version == "2.0":
            return EXTENDED_ATTRIBUTES_2_0
        else:
            return {}
# end class ServiceInterface
