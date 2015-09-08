# Copyright 2015 Google Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import time
import threading

from perfkitbenchmarker import virtual_machine, linux_virtual_machine, disk
from perfkitbenchmarker import flags
from perfkitbenchmarker import vm_util
from perfkitbenchmarker.openstack import os_disk
from perfkitbenchmarker.openstack import utils as os_utils

UBUNTU_IMAGE = 'ubuntu-14.04'

FLAGS = flags.FLAGS

flags.DEFINE_boolean('openstack_config_drive', False,
                     'Add possibilities to get metadata from external drive')

flags.DEFINE_boolean('openstack_boot_from_volume', False,
                     'Boot from volume instead of an image')

flags.DEFINE_integer('openstack_volume_size', 20,
                     'Size of the volume (GB)')

flags.DEFINE_string('openstack_zone', 'nova',
                    'Default zone to use when booting instances')

class OpenStackVirtualMachine(virtual_machine.BaseVirtualMachine):
    """Object representing an OpenStack Virtual Machine"""

    DEFAULT_MACHINE_TYPE = 'm1.small'
    DEFAULT_USERNAME = 'ubuntu'
    # Subclasses should override the default image.
    DEFAULT_IMAGE = None

    _floating_ip_lock = threading.Lock()

    def __init__(self, vm_spec):
        super(OpenStackVirtualMachine, self).__init__(vm_spec)
        self.name = 'perfkit_vm_%d_%s' % (self.instance_number, FLAGS.run_uri)
        self.key_name = 'perfkit_key_%d_%s' % (self.instance_number,
                                               FLAGS.run_uri)
        self.client = os_utils.NovaClient()
        self.id = None
        self.pk = None
        self.user_name = self.DEFAULT_USERNAME
        self.boot_wait_time = None
        self.boot_volume = None
        self.floating_ip = None
        self.pickler = os_utils._Pickler('client',pk='keypairs',floating_ip='floating_ips')


    def __getstate__(self):
        state = super(OpenStackVirtualMachine,self).__getstate__()
        return self.pickler.post_get(state)

    def __setstate__(self,dictionary):
        state = pickler.pre_set(dictionary)
        super(OpenStackVirtualMachine,self).__setstate__(state)

    @classmethod
    def SetVmSpecDefaults(cls, vm_spec):
      """Updates the VM spec with cloud specific defaults."""
      if vm_spec.machine_type is None:
        vm_spec.machine_type = cls.DEFAULT_MACHINE_TYPE
      if vm_spec.zone is None:
        vm_spec.zone = FLAGS.openstack_zone
      if vm_spec.image is None:
        vm_spec.image = cls.DEFAULT_IMAGE

    def _Create(self):
        flavor = self.client.flavors.findall(name=self.machine_type)[0]

        network = self.client.networks.find(
            label=FLAGS.openstack_private_network)
        nics = [{'net-id': network.id}]
        image_id = None
        boot_from_vol = []

        if FLAGS.openstack_boot_from_volume:
            boot_from_vol = [{'boot_index': 0,
                              'uuid': self.boot_volume._disk.id,
                              'volume_size': self.boot_volume.disk_size,
                              'source_type': 'volume',
                              'destination_type': 'volume',
                              'delete_on_termination': True}]
        else:
            image = self.client.images.findall(name=self.image)[0]
            image_id = image.id

        vm = self.client.servers.create(
            name=self.name,
            image=image_id,
            flavor=flavor.id,
            key_name=self.key_name,
            security_groups=['perfkit_sc_group'],
            nics=nics,
            availability_zone=self.zone,
            block_device_mapping_v2=boot_from_vol,
            config_drive=FLAGS.openstack_config_drive)
        self.id = vm.id

    @vm_util.Retry(max_retries=4, poll_interval=2)
    def _PostCreate(self):
        instance = None
        sleep = 1
        while True:
            instance = self.client.servers.get(self.id)
            if instance.addresses:
                break
            time.sleep(5)

        with self._floating_ip_lock:
            floating_ips = self.client.floating_ips.findall(fixed_ip=None,pool=FLAGS.openstack_public_network)
            if floating_ips:
                self.floating_ip = floating_ips[0]
            else:
                self.floating_ip = self.client.floating_ips.create(
                    pool=FLAGS.openstack_public_network)

            instance.add_floating_ip(self.floating_ip)
            is_attached = False
            while not is_attached:
                is_attached = self.client.floating_ips.get(self.floating_ip.id).fixed_ip != None
                if not is_attached:
                    time.sleep(sleep)

        self.ip_address = self.floating_ip.ip
        self.internal_ip = instance.networks[
            FLAGS.openstack_private_network][0]

    @os_utils.retry_authorization(max_retries=4)
    def _Delete(self):
        return
        try:
            self.client.servers.delete(self.id)
        except os_utils.NotFound:
            logging.info('Instance already deleted')

        while self.client.servers.findall(name=self.name):
            time.sleep(5)

        if self.floating_ip and not self.client.floating_ips.get(self.floating_ip.id).fixed_ip:
            with self._floating_ip_lock:
                if not self.client.floating_ips.get(self.floating_ip.id).fixed_ip:
                    self.client.floating_ips.delete(self.floating_ip)
                    while self.client.floating_ips.findall(id=self.floating_ip.id):
                        time.sleep(1)

    @os_utils.retry_authorization(max_retries=4)
    def _Exists(self):
        try:
            if self.client.servers.findall(name=self.name):
                return True
            else:
                return False
        except os_utils.NotFound:
            return False

    @vm_util.Retry(log_errors=False, poll_interval=1)
    def WaitForBootCompletion(self):
        # Do one longer sleep, then check at shorter intervals.
        if self.boot_wait_time is None:
          self.boot_wait_time = 15
        time.sleep(self.boot_wait_time)
        self.boot_wait_time = 5
        resp, _ = self.RemoteCommand('hostname', retries=1)
        if self.bootable_time is None:
            self.bootable_time = time.time()
        if self.hostname is None:
            self.hostname = resp[:-1]

    def CreateScratchDisk(self, disk_spec):
        #name = '%s-scratch-%s' % (self.name, len(self.scratch_disks))
        #scratch_disk = os_disk.OpenStackDisk(disk_spec, name, self.zone,
        #                                     self.project)
        #self.scratch_disks.append(scratch_disk)

        #scratch_disk.Create()
        #scratch_disk.Attach(self)

        #self.FormatDisk(scratch_disk.GetDevicePath())
        #self.MountDisk(scratch_disk.GetDevicePath(), disk_spec.mount_point)
        mount_point  = disk_spec.mount_point
        tmp_dir = "/tmp/disks/%s" % mount_point.replace("/","_")
        mkdir = "mkdir -p %s" % tmp_dir
        self.RemoteCommand(mkdir)
        disk_spec.mount_point=tmp_dir
        self.scratch_disks.append(disk_spec)

    def _CreateDependencies(self):
        self.ImportKeyfile()

        if FLAGS.openstack_boot_from_volume:
            image = self.client.images.findall(name=self.image)[0]
            disk_spec = disk.BaseDiskSpec(FLAGS.openstack_volume_size, disk.STANDARD, None)
            self.boot_volume = os_disk.OpenStackDisk(disk_spec,
                                             self.name +'-boot-volume',
                                             self.zone, self.project,image.id)
            self.boot_volume.Create()

    def _DeleteDependencies(self):
        return
        self.DeleteKeyfile()

        if self.boot_volume:
            self.boot_volume.Delete()

    def ImportKeyfile(self):
        if not (self.client.keypairs.findall(name=self.key_name)):
            cat_cmd = ['cat',
                       vm_util.GetPublicKeyPath()]
            key_file, _ = vm_util.IssueRetryableCommand(cat_cmd)
            pk = self.client.keypairs.create(self.key_name,
                                             public_key=key_file)
        else:
            pk = self.client.keypairs.findall(name=self.key_name)[0]
        self.pk = pk

    @os_utils.retry_authorization(max_retries=4)
    def DeleteKeyfile(self):
        return
        try:
            self.client.keypairs.delete(self.pk)
        except os_utils.NotFound:
            logging.info("Deleting key doesn't exists")


class DebianBasedOpenStackVirtualMachine(OpenStackVirtualMachine,
                                         linux_virtual_machine.DebianMixin):
    DEFAULT_IMAGE = UBUNTU_IMAGE
