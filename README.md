<h3 align="center">Python Uwazi API</h3>

---

## Quick Start

To use the API install the requirements

    pip install git+https://github.com/huridocs/python_uwazi_API@2026.3.13.2

and use it like this:

    from uwazi_api import UwaziClient

    client = UwaziClient(user='admin', password='admin', url='http://localhost:3000')
    client.entities.get_one(shared_id='shared_id', language='en')
    client.files.get_document(shared_id='shared_id', language='en')
    client.settings.get()
    client.templates.get()
    client.thesauris.get()


## API

All responses are now Pydantic models instead of raw dictionaries.

<b>Entities</b>

    get_one(shared_id: str, language: str) -> Entity
    get_id(shared_id: str, language: str) -> str
    get_shared_ids(to_process_template: str, batch_size: int, unpublished: bool = True) -> List[str]
    get(template_id: str, batch_size: int, language: str = 'en', published: bool = False) -> List[Entity]
    get_by_id(entity_id: str) -> Optional[Entity]
    upload(entity: Dict[str, any], language: str) -> str
    delete(shared_id: str)
    search_by_text(search_term: str, template_id: Optional[str], start_from: int, batch_size: int, language: str) -> List[Entity]

<b>Files</b>

    get_document(shared_id: str, language: str) -> Optional[bytes]
    get_document_by_file_name(file_name: str) -> Optional[bytes]
    save_document_to_path(shared_id: str, languages: List[str], path: str)
    upload_file(pdf_file_path, share_id, language, title)
    upload_image(image_binary, title, entity_shared_id, language)
    delete_file(id)

<b>Exports</b> (pandas DataFrames)

    to_dataframe(start_from: int, batch_size: int, template_id: Optional[str], language: str, published: Optional[bool]) -> pd.DataFrame

<b>Settings</b>

    get() -> Settings
    get_languages() -> List[str]

<b>Templates</b>

    set(language, template)
    get() -> List[Template]

<b>Thesauris</b>

    get(language: str) -> List[Thesauri]
    add_value(thesauri_name, thesauri_id, thesauri_values, language)

<b>CSV</b>

    upload_dataframe(df, template_name)
    upload_dataframe_and_get_shared_id(df, template_name) -> str

<b>Relationships</b>

    create(file_entity_shared_id, file_id, reference, to_entity_shared_id, relationship_type_id, language)
