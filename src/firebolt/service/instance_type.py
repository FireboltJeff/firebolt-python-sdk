from typing import Dict, List, NamedTuple, Optional

from firebolt.common.urls import INSTANCE_TYPES_URL
from firebolt.common.util import cached_property
from firebolt.model.instance_type import InstanceType, InstanceTypeKey
from firebolt.service.base import BaseService


class InstanceTypeLookup(NamedTuple):
    """Helper tuple for looking up instance types by names"""

    region_name: str
    instance_type_name: str


class InstanceTypeService(BaseService):
    @cached_property
    def instance_types(self) -> List[InstanceType]:
        """List of instance types available on Firebolt."""

        response = self.client.get(url=INSTANCE_TYPES_URL, params={"page.first": 5000})
        return [InstanceType.parse_obj(i["node"]) for i in response.json()["edges"]]

    @cached_property
    def instance_types_by_key(self) -> Dict[InstanceTypeKey, InstanceType]:
        """Dict of {InstanceTypeKey to InstanceType}"""

        return {i.key: i for i in self.instance_types}

    @cached_property
    def instance_types_by_name(self) -> Dict[InstanceTypeLookup, InstanceType]:
        """Dict of {InstanceTypeLookup to InstanceType}"""

        return {
            InstanceTypeLookup(
                region_name=self.resource_manager.regions.get_by_id(
                    id_=i.key.region_id
                ).name,
                instance_type_name=i.name,
            ): i
            for i in self.instance_types
        }

    def get_by_key(self, instance_type_key: InstanceTypeKey) -> InstanceType:
        """Get an instance type by key."""

        return self.instance_types_by_key[instance_type_key]

    def get_by_name(
        self,
        instance_type_name: str,
        region_name: Optional[str] = None,
    ) -> InstanceType:
        """
        Get an instance type by name.

        Args:
            instance_type_name: Name of the instance (eg. "i3.4xlarge").
            region_name:
                Name of the AWS region from which to get the instance.
                If not provided, use the default region name from the client.

        Returns:
            The requested instance type.
        """

        # Will raise an error if neither set
        region_name = region_name or self.resource_manager.regions.default_region.name
        return self.instance_types_by_name[
            InstanceTypeLookup(
                region_name=region_name,
                instance_type_name=instance_type_name,
            )
        ]
