import json
from uwazi_api.domain.thesauri import Thesauri
from uwazi_api.adapters.http_client_adapter import HttpClientAdapter


class ThesauriRepository:
    def __init__(self, http_client: HttpClientAdapter):
        self.http = http_client
        self._cache = {}

    def get(self, language: str) -> list[Thesauri]:
        if language in self._cache:
            return self._cache[language]
        response = self.http.request_adapter.get(
            url=f"{self.http.url}/api/thesauris",
            headers=self.http.headers,
            cookies={"locale": language},
        )
        data = json.loads(response.content.decode("utf-8"))
        self._cache[language] = [Thesauri.model_validate(t) for t in data.get("rows", []) if t.get("type", "") != "template"]
        return self._cache[language]

    def clear_cache(self, language: str = None) -> None:
        if language is None:
            self._cache.clear()
        else:
            self._cache.pop(language, None)

    def add_value(
        self, thesauri_name: str = None, thesauri_id: str = None, thesauri_values: dict = None, language: str = None
    ) -> dict:
        if not thesauri_name and not thesauri_id:
            raise ValueError("Either thesauri_name or thesauri_id must be provided")
        if not thesauri_values:
            raise ValueError("thesauri_values must be provided")
        if not language:
            raise ValueError("language must be provided")

        self.get(language)
        resolved_name = thesauri_name
        resolved_id = thesauri_id
        existing_thesaurus = None

        for t in self._cache.get(language, []):
            if not resolved_id and t.name == thesauri_name:
                resolved_id = t.id
                existing_thesaurus = t
                break
            if not resolved_name and t.id == thesauri_id:
                resolved_name = t.name
                existing_thesaurus = t
                break

        if not resolved_id:
            raise ValueError(f"Thesauri with name '{thesauri_name}' not found")
        if not resolved_name:
            raise ValueError(f"Thesauri with id '{thesauri_id}' not found")

        existing_value_ids = set()
        merged_values = []
        if existing_thesaurus:
            for v in existing_thesaurus.values:
                value_dict = {"label": v.label, "id": v.id}
                existing_value_ids.add(v.id)
                if v.values:
                    value_dict["values"] = [{"label": child.label, "id": child.id} for child in v.values]
                    for child in v.values:
                        existing_value_ids.add(child.id)
                merged_values.append(value_dict)

        for label, value_id in thesauri_values.items():
            if value_id not in existing_value_ids:
                merged_values.append({"label": label, "id": value_id})

        self.clear_cache(language)
        data = {
            "_id": resolved_id,
            "name": resolved_name,
            "values": merged_values,
        }
        response = self.http.request_adapter.post(
            url=f"{self.http.url}/api/thesauris",
            headers=self.http.headers,
            cookies={"locale": language},
            data=json.dumps(data),
        )
        return json.loads(response.content)

    def create(self, name: str, values: list[dict], language: str) -> dict:
        self.clear_cache(language)
        data = {
            "name": name,
            "values": values,
        }
        response = self.http.request_adapter.post(
            url=f"{self.http.url}/api/thesauris",
            headers=self.http.headers,
            cookies={"locale": language},
            data=json.dumps(data),
        )
        return json.loads(response.content)

    def update(self, thesauri_id: str, name: str, values: list[dict], language: str) -> dict:
        """Replace a thesaurus' entire value tree.

        ``values`` is the full desired list of values. Each item is either a
        flat value ``{"label": ...}`` or a group ``{"label": ..., "values":
        [{"label": ...}, ...]}``. Items may also carry an ``id`` to preserve
        existing value ids. Posting to ``/api/thesauris`` with an ``_id``
        replaces the whole vocabulary, so callers that want to keep existing
        values must include them here.
        """
        self.clear_cache(language)
        data = {
            "_id": thesauri_id,
            "name": name,
            "values": values,
        }
        response = self.http.request_adapter.post(
            url=f"{self.http.url}/api/thesauris",
            headers=self.http.headers,
            cookies={"locale": language},
            data=json.dumps(data),
        )
        return json.loads(response.content)

    def delete_unassigned(self, thesauri_id: str, language: str) -> dict:
        self.clear_cache(language)
        response = self.http.request_adapter.delete(
            url=f"{self.http.url}/api/thesauris",
            headers=self.http.headers,
            cookies={"locale": language},
            params={"_id": thesauri_id},
        )
        return json.loads(response.content)
