# -*- coding: utf-8 -*-

# Automatic provisioning of AWS EBS volumes.


import nixops.util
import nixops_aws.ec2_utils
import nixops.resources
import botocore.exceptions
from . import ec2_common

from .types.ebs_volume import EbsVolumeOptions


class EBSVolumeDefinition(nixops.resources.ResourceDefinition):
    """Definition of an EBS volume."""

    config: EbsVolumeOptions

    @classmethod
    def get_type(cls):
        return "ebs-volume"

    @classmethod
    def get_resource_type(cls):
        return "ebsVolumes"

    def show_type(self):
        return "{0}".format(self.get_type())


class EBSVolumeState(nixops.resources.ResourceState, ec2_common.EC2CommonState):
    """State of an EBS volume."""

    definition_type = EBSVolumeDefinition

    state = nixops.util.attr_property(
        "state", nixops.resources.ResourceState.MISSING, int
    )
    access_key_id = nixops.util.attr_property("ec2.accessKeyId", None)
    region = nixops.util.attr_property("ec2.region", None)
    zone = nixops.util.attr_property("ec2.zone", None)
    volume_id = nixops.util.attr_property("ec2.volumeId", None)
    size = nixops.util.attr_property("ec2.size", None, int)
    iops = nixops.util.attr_property("ec2.iops", None, int)
    volume_type = nixops.util.attr_property("ec2.volumeType", None)

    @classmethod
    def get_type(cls):
        return "ebs-volume"

    def __init__(self, depl, name, id):
        nixops.resources.ResourceState.__init__(self, depl, name, id)
        self._conn = None
        self._conn_boto3 = None

    def _exists(self):
        return self.state != self.MISSING

    def show_type(self):
        s = super(EBSVolumeState, self).show_type()
        if self._exists():
            s = "{0} [{1}; {2} GiB]".format(s, self.zone, self.size)
        return s

    @property
    def resource_id(self):
        return self.volume_id

    def _connect(self, region):
        if self._conn:
            return self._conn
        self._conn = nixops_aws.ec2_utils.connect(region, self.access_key_id)
        return self._conn

    def _connect_boto3(self, region):
        if self._conn_boto3:
            return self._conn_boto3
        self._conn_boto3 = nixops_aws.ec2_utils.connect_ec2_boto3(
            region, self.access_key_id
        )
        return self._conn_boto3

    def _get_vol(self, config: EbsVolumeOptions):
        try:
            _vol = self._connect_boto3(config.region).describe_volumes(
                VolumeIds=[config.volumeId]
            )["Volumes"][0]
        except botocore.exceptions.ClientError as error:
            raise error
        if _vol["VolumeType"] == "io1" or _vol["VolumeType"] == "io2":
            iops = _vol["Iops"]
        else:
            iops = config.iops
        with self.depl._db:
            self.state = self.STARTING
            self.region = config.region
            self.zone = _vol["AvailabilityZone"]
            self.size = _vol["Size"]
            self.volume_id = config.volumeId
            self.iops = iops
            self.volume_type = _vol["VolumeType"]

    def create(self, defn: EBSVolumeDefinition, check, allow_reboot, allow_recreate):

        self.access_key_id = (
            defn.config.accessKeyId or nixops_aws.ec2_utils.get_access_key_id()
        )
        if not self.access_key_id:
            raise Exception(
                "please set ‘accessKeyId’, $EC2_ACCESS_KEY or $AWS_ACCESS_KEY_ID"
            )

        if self._exists():
            if self.region != defn.config.region or self.zone != defn.config.zone:
                raise Exception(
                    "changing the region or availability zone of an EBS volume is not supported"
                )

            if defn.config.size != 0 and self.size != defn.config.size:
                raise Exception(
                    "changing the size an EBS volume is currently not supported"
                )

            if (
                self.volume_type is not None
                and defn.config.volumeType != self.volume_type
            ):
                raise Exception(
                    "changing the type of an EBS volume is currently not supported"
                )

            if defn.config.iops != self.iops:
                raise Exception(
                    "changing the IOPS of an EBS volume is currently not supported"
                )

        if self.state == self.MISSING:
            if defn.config.volumeId:
                self.log(
                    "Using provided EBS volume ‘{0}’...".format(defn.config.volumeId)
                )
                self._get_vol(defn.config)
            else:
                if defn.config.size == 0 and defn.config.snapshot != "":
                    snapshots = self._connect(defn.config.region).get_all_snapshots(
                        snapshot_ids=[defn.config.snapshot]
                    )
                    assert len(snapshots) == 1
                    defn.config.size = snapshots[0].volume_size

                if defn.config.snapshot:
                    self.log(
                        "creating EBS volume of {0} GiB from snapshot ‘{1}’...".format(
                            defn.config.size, defn.config.snapshot
                        )
                    )
                else:
                    self.log(
                        "creating EBS volume of {0} GiB...".format(defn.config.size)
                    )

                if defn.config.zone is None:
                    raise Exception(
                        "please set a zone where the volume will be created"
                    )

                volume = self._connect(defn.config.region).create_volume(
                    zone=defn.config.zone,
                    size=defn.config.size,
                    snapshot=defn.config.snapshot,
                    iops=defn.config.iops,
                    volume_type=defn.config.volumeType,
                )
                # FIXME: if we crash before the next step, we forget the
                # volume we just created.  Doesn't seem to be anything we
                # can do about this.

                with self.depl._db:
                    self.state = self.STARTING
                    self.region = defn.config.region
                    self.zone = defn.config.zone
                    self.size = defn.config.size
                    self.volume_id = volume.id
                    self.iops = defn.config.iops
                    self.volume_type = defn.config.volumeType

                self.log("volume ID is ‘{0}’".format(self.volume_id))

        if self.state == self.STARTING or check:
            # ensure the connection has been established before calling
            # update_tags
            self._connect(self.region)

            self.update_tags(self.volume_id, user_tags=defn.config.tags, check=check)
            nixops_aws.ec2_utils.wait_for_volume_available(
                self._connect(self.region),
                self.volume_id,
                self.logger,
                states=["available", "in-use"],
            )
            self.state = self.UP

    def check(self):
        volume = nixops_aws.ec2_utils.get_volume_by_id(
            self._connect(self.region), self.volume_id
        )
        if volume is None:
            self.state = self.MISSING

    def destroy(self, wipe=False):
        if not self._exists():
            return True

        if wipe:
            self.warn("wipe is not supported")

        volume = nixops_aws.ec2_utils.get_volume_by_id(
            self._connect(self.region), self.volume_id, allow_missing=True
        )
        if not volume:
            return True
        if not self.depl.logger.confirm(
            "are you sure you want to destroy EBS volume ‘{0}’?".format(self.name)
        ):
            return False
        self.log("destroying EBS volume ‘{0}’...".format(self.volume_id))
        self._retry(lambda: volume.delete())
        return True
