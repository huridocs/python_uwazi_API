import os
from pathlib import Path
from typing import List, Optional

from uwazi_api.domain.interfaces import (
    EntityRepositoryInterface,
    FileRepositoryInterface,
)


class FileService:
    language_to_file_language = {"fr": "fra", "es": "spa", "en": "eng", "pt": "prt", "ar": "arb"}

    def __init__(
        self,
        file_repository: FileRepositoryInterface,
        entity_repository: EntityRepositoryInterface,
    ):
        self.file_repo = file_repository
        self.entity_repo = entity_repository

    # --- Orchestration methods ---

    def get_document(self, shared_id: str, language: str) -> Optional[bytes]:
        entity = self.entity_repo.get_one(shared_id, language)
        mapping = self.language_to_file_language
        if language not in mapping:
            return None
        file_language = mapping[language]
        docs = [d for d in entity.documents if d.language == file_language]
        if not docs:
            return None
        return self.file_repo.get_document_by_file_name(docs[0].filename)

    def save_document_to_path(self, shared_id: str, languages: List[str], path: str) -> None:
        if not os.path.exists(path):
            os.makedirs(path)
        for language in languages:
            document_content = self.get_document(shared_id, language)
            if document_content is None:
                continue
            file_id = str(hash(document_content))
            file_path_pdf = Path(f"{path}/{file_id}.pdf")
            file_path_pdf.write_bytes(document_content)

    # --- Delegates to repository ---

    def get_document_by_file_name(self, file_name: str) -> Optional[bytes]:
        return self.file_repo.get_document_by_file_name(file_name)

    def upload_file(self, pdf_file_path: str, share_id: str, language: str, title: str) -> bool:
        return self.file_repo.upload_file(pdf_file_path, share_id, language, title)

    def upload_document_from_bytes(
        self, file_bytes: bytes, share_id: str, language: str, title: str, file_type: str
    ) -> bool:
        return self.file_repo.upload_document_from_bytes(file_bytes, share_id, language, title, file_type)

    def upload_file_from_bytes(
        self, file_bytes: bytes, share_id: str, language: str, title: str, file_type: str = "application/pdf"
    ) -> bool:
        return self.file_repo.upload_file_from_bytes(file_bytes, share_id, language, title, file_type)

    def upload_image(self, image_binary: bytes, title: str, entity_shared_id: str, language: str) -> Optional[dict]:
        return self.file_repo.upload_image(image_binary, title, entity_shared_id, language)

    def delete_file(self, file_id: str) -> bool:
        return self.file_repo.delete_file(file_id)
