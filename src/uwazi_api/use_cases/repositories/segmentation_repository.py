import json

from uwazi_api.adapters.http_client_adapter import HttpClientAdapter
from uwazi_api.domain.exceptions import SegmentationError, SegmentationNotFoundError
from uwazi_api.domain.segmentation import Segmentation


class SegmentationRepository:
    def __init__(self, http_client: HttpClientAdapter):
        self.http = http_client

    def get_by_file_id(self, file_id: str) -> Segmentation:
        response = self.http.request_adapter.get(
            url=f"{self.http.url}/api/v2/files/{file_id}/segmentation",
            headers=self.http.headers,
            cookies={},
        )
        if response.status_code == 404:
            raise SegmentationNotFoundError(
                f"Segmentation not found for file {file_id}: the segmentation feature may be disabled, "
                f"the file may not be a document, or no ready segmentation exists yet"
            )
        if response.status_code == 401:
            raise SegmentationError(f"Segmentation for file {file_id} requires admin credentials (status=401)")
        if response.status_code != 200:
            raise SegmentationError(
                f"Error getting segmentation for file {file_id}: status={response.status_code}, body={response.text[:500]}"
            )
        return Segmentation.model_validate(json.loads(response.content))
