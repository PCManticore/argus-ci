# Copyright 2014 Cloudbase-init
# All Rights Reserved.
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

import re
import base64
import subprocess
import os
import time
import pdb
import sys

import six
from oslo.config import cfg

from tempest.common.utils import data_utils
from tempest import config
from tempest.openstack.common import log as logging
from tempest.scenario import manager
from tempest.scenario import utils as test_utils

if 'argus' not in sys.modules:
    # TODO(cpopa): use this hack until argus can be a real package.
    # Since we don't know how the test discovery will load us,
    # we inject the current module as 'argus' into sys.modules, so we can
    # import our files as if we were an installed package.
    sys.modules['argus'] = sys.modules[__name__]

from argus import exceptions
from argus import prepare
from argus import util

LOG = logging.getLogger("cbinit")

CONF = config.CONF

# Register the CloudbaseInit options from tempest.conf.
CBINIT_GROUP = cfg.OptGroup(name='cbinit',
                            title="Cloudbase-init Options")
OPTS = [
    cfg.BoolOpt('replace_code',
                default=False,
                help="replace cbinit code, or use the one added by the "
                     "installer"),
    cfg.StrOpt('service_type',
               default='http',
               help="service_type should take value 'http', 'ec2', "
                    "or 'configdrive'"),
    cfg.StrOpt('userdata_path',
               default='',
               help="path to userdata to be used"),
    cfg.StrOpt('default_ci_username',
               default='CiAdmin',
               help="The default CI user for the instances."),
    cfg.StrOpt('default_ci_password',
               default='Passw0rd',
               help="The default password for the CI user."),
    cfg.StrOpt('created_user',
               default='Admin',
               help='The user created by the CloudbaseInit plugins.'),
    cfg.StrOpt('install_script_url',
               default='https://raw.githubusercontent.com/trobert2/'
                       'windows-openstack-imaging-tools/master/'
                       'installCBinit.ps1',
               help="An URL representing the script which will install "
                    "CloudbaseInit."),

]
config.register_opt_group(cfg.CONF, CBINIT_GROUP, OPTS)
# Done registering.


class BaseArgusScenario(manager.ScenarioTest):

    # Various classmethod utilities used in setUpClass and tearDownClass

    @classmethod
    def _wait_until(cls, servers, kwargs):
        for server in servers:
            try:
                cls.servers_client.wait_for_server_status(
                    server['id'], kwargs['wait_until'])
                cls.instance = server
            except Exception as ex:
                if ('preserve_server_on_error' not in kwargs
                        or kwargs['preserve_server_on_error'] is False):
                    for server in servers:
                        try:
                            cls.servers_client.delete_server(server['id'])
                        except Exception:
                            LOG.exception("Failed deleting server %s",
                                          server['id'])
                raise ex

    @classmethod
    def create_test_server(cls, **kwargs):
        """Wrapper utility that returns a test server."""
        # TODO(cpopa): add image_ref so it can be run for different images
        if 'name' in kwargs:
            name = kwargs.pop('name')
        else:
            name = data_utils.rand_name(cls.__name__ + "-instance")
        flavor = CONF.compute.flavor_ref
        image_id = CONF.compute.image_ref

        _, body = cls.servers_client.create_server(
            name, image_id, flavor, **kwargs)

        # handle the case of multiple servers
        servers = [body]
        if 'min_count' in kwargs or 'max_count' in kwargs:
            # Get servers created which name match with name param.
            _, b = cls.servers_client.list_servers()
            servers = [s for s in b['servers'] if s['name'].startswith(name)]

        if 'wait_until' in kwargs:
            cls._wait_until(servers, kwargs)

        cls.servers.extend(servers)

    @classmethod
    def create_keypair(cls):
        _, cls.keypair = cls.keypairs_client.create_keypair(
            cls.__name__ + "-key")
        with open(CONF.compute.path_to_private_key, 'w') as stream:
            stream.write(cls.keypair['private_key'])

    @classmethod
    def _assign_floating_ip(cls):
        # Obtain a floating IP
        _, cls.floating_ip = cls.floating_ips_client.create_floating_ip()

        cls.floating_ips_client.associate_floating_ip_to_server(
            cls.floating_ip['ip'], cls.instance['id'])

    # Instance creation and termination.

    @classmethod
    def setUpClass(cls):
        super(BaseArgusScenario, cls).setUpClass()

        cls.security_groups = []
        cls.subnets = []
        cls.servers = []
        cls.routers = []
        cls.floating_ips = {}

        cls.create_keypair()
        metadata = {'network_config': str({'content_path':
                                           'random_value_test_random'})}

        with open(CONF.cbinit.userdata_path, 'r') as h:
            data = h.read()
            encoded_data = base64.encodestring(data)

        cls.create_test_server(wait_until='ACTIVE',
                               key_name=cls.keypair['name'],
                               disk_config='AUTO',
                               user_data=encoded_data,
                               meta=metadata)
        cls._assign_floating_ip()

    @classmethod
    def tearDownClass(cls):
        cls.servers_client.delete_server(cls.instance['id'])
        cls.servers_client.wait_for_server_termination(cls.instance['id'])
        cls.floating_ips_client.delete_floating_ip(cls.floating_ip['id'])
        cls.keypairs_client.delete_keypair(cls.keypair['name'])

        os.remove(CONF.compute.path_to_private_key)

        super(BaseArgusScenario, cls).tearDownClass()

    # Test preparations.

    def setUp(self):
        super(BaseArgusScenario, self).setUp()

        # Setup image and flavor the test instance
        # Support both configured and injected values
        if not hasattr(self, 'image_ref'):
            self.image_ref = CONF.compute.image_ref
        if not hasattr(self, 'flavor_ref'):
            self.flavor_ref = CONF.compute.flavor_ref
        self.image_utils = test_utils.ImageUtils()

        if not self.image_utils.is_flavor_enough(self.flavor_ref,
                                                 self.image_ref):
            raise self.skipException(
                '{image} does not fit in {flavor}'.format(
                    image=self.image_ref, flavor=self.flavor_ref
                )
            )
        self.change_security_group(self.instance['id'])
        self.private_network = self.get_private_network()

        self.remote_client = util.WinRemoteClient(
            self.floating_ip['ip'],
            CONF.cbinit.default_ci_username,
            CONF.cbinit.default_ci_password)
        self.prepare_instance()

    def tearDown(self):
        for sec_group in self.security_groups:
            try:
                self.servers_client.remove_security_group(
                    self.instance['id'], sec_group['name'])
            except Exception as ex:
                LOG.exception("Failed removing security groups.")

        super(BaseArgusScenario, self).tearDown()

    # Utilities used by setUp.
    def change_security_group(self, server_id):
        security_group = self._create_security_group()
        self.security_groups.append(security_group)

        for sec_group in self.instance['security_groups']:
            try:
                self.servers_client.remove_security_group(server_id,
                                                          sec_group['name'])
            except Exception as ex:
                LOG.exception("Error removing security group.")

        self.servers_client.add_security_group(server_id,
                                               security_group['name'])

    def get_private_network(self):
        networks = self.networks_client.list_networks()[1]
        for network in networks:
            if network['label'] == 'private_cbinit':
                return network

    def password(self):
        _, encoded_password = self.servers_client.get_password(
            self.instance['id'])
        return util.decrypt_password(
            private_key=CONF.compute.path_to_private_key,
            password=encoded_password['password'])

    @util.run_once
    def prepare_instance(self):
        prepare.InstancePreparer(
            self.instance['id'],
            self.servers_client,
            self.remote_client).prepare()


class BaseTest(BaseArgusScenario):
    """The base test class which should be used by tests."""

    # Various helpful APIs

    @property
    def run_verbose_wsman(self):
        return self.remote_client.run_verbose_wsman

    def get_image_ref(self):
        return self.images_client.get_image(CONF.compute.image_ref)

    def instance_server(self):
        return self.servers_client.get_server(self.instance['id'])

