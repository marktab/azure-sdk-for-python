# ---------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# ---------------------------------------------------------

# pylint: disable=protected-access

import os
import json
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional

from marshmallow.exceptions import ValidationError as SchemaValidationError

from azure.ai.ml._utils.utils import is_url
from azure.ai.ml._exception_helper import log_and_raise_error
from azure.ai.ml._restclient.v2023_04_01_preview.models import (
    ListViewType,
    FeaturesetVersion,
    FeaturesetVersionBackfillRequest,
    FeatureWindow,
)
from azure.ai.ml._restclient.v2023_04_01_preview import AzureMachineLearningWorkspaces as ServiceClient042023Preview
from azure.ai.ml._scope_dependent_operations import OperationConfig, OperationScope, _ScopeDependentOperations
from azure.ai.ml.exceptions import ErrorCategory, ErrorTarget, ValidationErrorType, ValidationException
from azure.ai.ml._artifacts._artifact_utilities import _check_and_upload_path
from azure.ai.ml.operations._datastore_operations import DatastoreOperations

# from azure.ai.ml._telemetry import ActivityType, monitor_with_activity
from azure.ai.ml._utils._feature_store_utils import (
    _archive_or_restore,
    read_feature_set_metadata_contents,
    read_remote_feature_set_spec_metadata_contents,
)
from azure.ai.ml._utils._logger_utils import OpsLogger
from azure.ai.ml._utils._experimental import experimental
from azure.ai.ml.entities._assets._artifacts.feature_set import FeatureSet
from azure.ai.ml.entities._feature_set.featureset_spec_metadata import FeaturesetSpecMetadata
from azure.ai.ml.entities._feature_set.materialization_compute_resource import MaterializationComputeResource
from azure.ai.ml.entities._feature_set.feature_set_materialization_response import FeatureSetMaterializationResponse
from azure.ai.ml.entities._feature_set.feature_set_backfill_response import FeatureSetBackfillResponse
from azure.ai.ml.entities._feature_set.feature import Feature
from azure.core.polling import LROPoller
from azure.core.paging import ItemPaged

ops_logger = OpsLogger(__name__)
module_logger = ops_logger.module_logger


@experimental
class FeatureSetOperations(_ScopeDependentOperations):
    """FeatureSetOperations.

    You should not instantiate this class directly. Instead, you should
    create an MLClient instance that instantiates it for you and
    attaches it as an attribute.
    """

    def __init__(
        self,
        operation_scope: OperationScope,
        operation_config: OperationConfig,
        service_client: ServiceClient042023Preview,
        datastore_operations: DatastoreOperations,
        **kwargs: Dict,
    ):
        super(FeatureSetOperations, self).__init__(operation_scope, operation_config)
        ops_logger.update_info(kwargs)
        self._operation = service_client.featureset_versions
        self._container_operation = service_client.featureset_containers
        self._feature_operation = service_client.features
        self._service_client = service_client
        self._datastore_operation = datastore_operations
        self._init_kwargs = kwargs

    # @monitor_with_activity(logger, "FeatureSet.List", ActivityType.PUBLICAPI)
    def list(
        self,
        *,
        name: Optional[str] = None,
        list_view_type: ListViewType = ListViewType.ACTIVE_ONLY,
    ) -> ItemPaged[FeatureSet]:
        """List the FeatureSet assets of the workspace.

        :param name: Name of a specific FeatureSet asset, optional.
        :type name: Optional[str]
        :param list_view_type: View type for including/excluding (for example) archived FeatureSet assets.
        Default: ACTIVE_ONLY.
        :type list_view_type: Optional[ListViewType]
        :return: An iterator like instance of FeatureSet objects
        :rtype: ~azure.core.paging.ItemPaged[FeatureSet]
        """
        if name:
            return self._operation.list(
                workspace_name=self._workspace_name,
                name=name,
                cls=lambda objs: [FeatureSet._from_rest_object(obj) for obj in objs],
                list_view_type=list_view_type,
                **self._scope_kwargs,
            )
        return self._container_operation.list(
            workspace_name=self._workspace_name,
            cls=lambda objs: [FeatureSet._from_container_rest_object(obj) for obj in objs],
            list_view_type=list_view_type,
            **self._scope_kwargs,
        )

    def _get(self, name: str, version: str = None) -> FeaturesetVersion:
        return self._operation.get(
            resource_group_name=self._resource_group_name,
            workspace_name=self._workspace_name,
            name=name,
            version=version,
            **self._init_kwargs,
        )

    # @monitor_with_activity(logger, "FeatureSet.Get", ActivityType.PUBLICAPI)
    def get(self, *, name: str, version: str) -> FeatureSet:
        """Get the specified FeatureSet asset.

        :param name: Name of FeatureSet asset.
        :type name: str
        :param version: Version of FeatureSet asset.
        :type version: str
        :raises ~azure.ai.ml.exceptions.ValidationException: Raised if FeatureSet cannot be successfully
            identified and retrieved. Details will be provided in the error message.
        :return: FeatureSet asset object.
        :rtype: ~azure.ai.ml.entities.FeatureSet
        """
        try:
            featureset_version_resource = self._get(name, version)
            return FeatureSet._from_rest_object(featureset_version_resource)
        except (ValidationException, SchemaValidationError) as ex:
            log_and_raise_error(ex)

    # @monitor_with_activity(logger, "FeatureSet.BeginCreateOrUpdate", ActivityType.PUBLICAPI)
    def begin_create_or_update(self, featureset: FeatureSet) -> LROPoller[FeatureSet]:
        """Create or update FeatureSet

        :param featureset: FeatureSet definition.
        :type featureset: FeatureSet
        :return: An instance of LROPoller that returns a FeatureSet.
        :rtype: ~azure.core.polling.LROPoller[~azure.ai.ml.entities.FeatureSet]
        """

        featureset_spec = self._validate_and_get_feature_set_spec(featureset)
        featureset.properties["featuresetPropertiesVersion"] = "1"
        featureset.properties["featuresetProperties"] = json.dumps(featureset_spec._to_dict())

        sas_uri = None
        featureset, _ = _check_and_upload_path(
            artifact=featureset, asset_operations=self, sas_uri=sas_uri, artifact_type=ErrorTarget.FEATURE_SET
        )

        featureset_resource = FeatureSet._to_rest_object(featureset)

        return self._operation.begin_create_or_update(
            resource_group_name=self._resource_group_name,
            workspace_name=self._workspace_name,
            name=featureset.name,
            version=featureset.version,
            body=featureset_resource,
            cls=lambda response, deserialized, headers: FeatureSet._from_rest_object(deserialized),
        )

    # @monitor_with_activity(logger, "FeatureSet.BeginBackFill", ActivityType.PUBLICAPI)
    def begin_backfill(
        self,
        *,
        name: str,
        version: str,
        feature_window_start_time: datetime,
        feature_window_end_time: datetime,
        display_name: Optional[str] = None,
        description: Optional[str] = None,
        tags: Optional[Dict[str, str]] = None,
        compute_resource: Optional[MaterializationComputeResource] = None,
        spark_configuration: Optional[Dict[str, str]] = None,
        **kwargs,  # pylint: disable=unused-argument
    ) -> LROPoller[FeatureSetBackfillResponse]:
        """Backfill.

        :param name: Feature set name. This is case-sensitive.
        :type name: str
        :param version: Version identifier. This is case-sensitive.
        :type version: str
        :param feature_window_start_time: Start time of the feature window to be materialized.
        :type feature_window_start_time: datetime
        :param feature_window_end_time: End time of the feature window to be materialized.
        :type feature_window_end_time: datetime
        :param display_name: Specifies description.
        :type display_name: str
        :param description: Specifies description.
        :type description: str
        :param tags: A set of tags. Specifies the tags.
        :type tags: dict[str, str]
        :param compute_resource: Specifies the compute resource settings.
        :type compute_resource: ~azure.ai.ml.entities.MaterializationComputeResource
        :param spark_configuration: Specifies the spark compute settings.
        :type spark_configuration: dict[str, str]
        :return: An instance of LROPoller that returns ~azure.ai.ml.entities.FeatureSetBackfillResponse
        """

        request_body: FeaturesetVersionBackfillRequest = FeaturesetVersionBackfillRequest(
            description=description,
            display_name=display_name,
            feature_window=FeatureWindow(
                feature_window_start=feature_window_start_time, feature_window_end=feature_window_end_time
            ),
            resource=compute_resource._to_rest_object() if compute_resource else None,
            spark_configuration=spark_configuration,
            tags=tags,
        )
        return self._operation.begin_backfill(
            resource_group_name=self._resource_group_name,
            workspace_name=self._workspace_name,
            name=name,
            version=version,
            body=request_body,
            cls=lambda response, deserialized, headers: FeatureSetBackfillResponse._from_rest_object(deserialized),
        )

    # @monitor_with_activity(logger, "FeatureSet.ListMaterializationOperation", ActivityType.PUBLICAPI)
    def list_materialization_operation(
        self,
        *,
        name,
        version,
        feature_window_start_time: Optional[str] = None,
        feature_window_end_time: Optional[str] = None,
        filters: Optional[str] = None,
        **kwargs,  # pylint: disable=unused-argument
    ) -> ItemPaged[FeatureSetMaterializationResponse]:
        """List Materialization operation.

        :param name: Feature set name.
        :type name: str
        :param version: Feature set version.
        :type version: str
        :param feature_window_start_time: Start time of the feature window to filter materialization jobs.
        :type feature_window_start_time: datetime
        :param feature_window_end_time: End time of the feature window to filter materialization jobs.
        :type feature_window_end_time: datetime
        :param filters: Comma-separated list of tag names (and optionally values). Example: tag1,tag2=value2.
        :type filters: str
        :return: An iterator like instance of ~azure.ai.ml.entities.FeatureSetMaterializationResponse objects
        :rtype: ~azure.core.paging.ItemPaged[FeatureSetMaterializationResponse]
        """

        materialization_jobs = self._operation.list_materialization_jobs(
            resource_group_name=self._resource_group_name,
            workspace_name=self._workspace_name,
            name=name,
            version=version,
            filters=filters,
            feature_window_start=feature_window_start_time,
            feature_window_end=feature_window_end_time,
            cls=lambda objs: [FeatureSetMaterializationResponse._from_rest_object(obj) for obj in objs],
        )
        return materialization_jobs

    # @monitor_with_activity(logger, "FeatureSet.ListFeatures", ActivityType.INTERNALCALL)
    def list_features(
        self,
        *,
        feature_set_name,
        version,
        feature_name: Optional[str] = None,
        description: Optional[str] = None,
        tags: Optional[str] = None,
    ) -> ItemPaged[Feature]:
        """List features

        :param feature_set_name: Feature set name.
        :type feature_set_name: str
        :param version: Feature set version.
        :type version: str
        :param feature_name: feature name.
        :type feature_name: str
        :param description: Description of the featureset.
        :type description: str
        :param tags: Comma-separated list of tag names (and optionally values). Example: tag1,tag2=value2.
        :type tags: str
        :return: An iterator like instance of Feature objects
        :rtype: ~azure.core.paging.ItemPaged[Feature]
        """
        features = self._feature_operation.list(
            resource_group_name=self._resource_group_name,
            workspace_name=self._workspace_name,
            featureset_name=feature_set_name,
            featureset_version=version,
            tags=tags,
            feature_name=feature_name,
            description=description,
            cls=lambda objs: [Feature._from_rest_object(obj) for obj in objs],
        )
        return features

    # @monitor_with_activity(logger, "FeatureSet.GetFeature", ActivityType.INTERNALCALL)
    def get_feature(self, *, feature_set_name, version, feature_name) -> "Feature":
        """Get Feature

        :param feature_set_name: Feature set name.
        :type feature_set_name: str
        :param version: Feature set version.
        :type version: str
        :param feature_name. This is case-sensitive.
        :type feature_name: str
        :param tags: Comma-separated list of tag names (and optionally values). Example: tag1,tag2=value2.
        :type tags: str
        :return: Feature object
        :rtype: ~azure.ai.ml.entities.Feature
        """
        feature = self._feature_operation.get(
            resource_group_name=self._resource_group_name,
            workspace_name=self._workspace_name,
            featureset_name=feature_set_name,
            featureset_version=version,
            feature_name=feature_name,
        )

        return Feature._from_rest_object(feature)

    # @monitor_with_activity(logger, "FeatureSet.Archive", ActivityType.PUBLICAPI)
    def archive(
        self,
        *,
        name: str,
        version: str,
        **kwargs,  # pylint:disable=unused-argument
    ) -> None:
        """Archive a FeatureSet asset.

        :param name: Name of FeatureSet asset.
        :type name: str
        :param version: Version of FeatureSet asset.
        :type version: str
        :return: None
        """

        _archive_or_restore(
            asset_operations=self,
            version_operation=self._operation,
            is_archived=True,
            name=name,
            version=version,
        )

    # @monitor_with_activity(logger, "FeatureSet.Restore", ActivityType.PUBLICAPI)
    def restore(
        self,
        *,
        name: str,
        version: str,
        **kwargs,  # pylint:disable=unused-argument
    ) -> None:
        """Restore an archived FeatureSet asset.

        :param name: Name of FeatureSet asset.
        :type name: str
        :param version: Version of FeatureSet asset.
        :type version: str
        :return: None
        """

        _archive_or_restore(
            asset_operations=self,
            version_operation=self._operation,
            is_archived=False,
            name=name,
            version=version,
        )

    def _validate_and_get_feature_set_spec(self, featureset: FeatureSet) -> FeaturesetSpecMetadata:
        # pylint: disable=no-member
        if not (featureset.specification and featureset.specification.path):
            msg = "Missing FeatureSet specification path. Path is required for feature set."
            raise ValidationException(
                message=msg,
                no_personal_data_message=msg,
                error_type=ValidationErrorType.MISSING_FIELD,
                target=ErrorTarget.FEATURE_SET,
                error_category=ErrorCategory.USER_ERROR,
            )

        featureset_spec_path = str(featureset.specification.path)
        if is_url(featureset_spec_path):
            try:
                featureset_spec_contents = read_remote_feature_set_spec_metadata_contents(
                    base_uri=featureset_spec_path,
                    datastore_operations=self._datastore_operation,
                )
                featureset_spec_path = None
            except Exception as ex:  # pylint: disable=broad-except
                module_logger.info("Unable to access FeaturesetSpec at path %s", featureset_spec_path)
                raise ex
            return FeaturesetSpecMetadata._load(featureset_spec_contents, featureset_spec_path)

        if not os.path.isabs(featureset_spec_path):
            featureset_spec_path = Path(featureset.base_path, featureset_spec_path).resolve()

        if not os.path.isdir(featureset_spec_path):
            raise ValidationException(
                message="No such directory: {}".format(featureset_spec_path),
                no_personal_data_message="No such directory",
                target=ErrorTarget.FEATURE_SET,
                error_category=ErrorCategory.USER_ERROR,
                error_type=ValidationErrorType.FILE_OR_FOLDER_NOT_FOUND,
            )

        featureset_spec_contents = read_feature_set_metadata_contents(path=featureset_spec_path)
        featureset_spec_yaml_path = Path(featureset_spec_path, "FeaturesetSpec.yaml")
        return FeaturesetSpecMetadata._load(featureset_spec_contents, featureset_spec_yaml_path)
