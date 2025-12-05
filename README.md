<h3 align="center">Python Uwazi API</h3>

---

## Quick Start

To use the API install the requirements

    pip install git+https://github.com/huridocs/python_uwazi_API@2025.12.5.2

and use it like this:

    uwazi_adapter = UwaziAdapter(user='admin', password='admin', url='http://localhost:3000')
    uwazi_adapter.entities.get_one(shared_id='shared_id', language='en')
    uwazi_adapter.files.get_document(shared_id='shared_id', language='en')
    uwazi_adapter.settings.get()
    uwazi_adapter.templates.get()
    uwazi_adapter.thesauris.get()


## API

<b>Entities</b>

    get_one(shared_id: str, language: str)
    get_id(shared_id: str, language: str)
    get_shared_ids(to_process_template: str, batch_size: int, unpublished: bool = True)
    get(template_id: str, batch_size: int, language: str = 'en', published: bool = False)
    get_by_id(entity_id: str)
    upload(entity: Dict[str, any], language: str)
    delete(share_id:str)

<b>Files</b>

    get_document(shared_id: str, language: str)
    get_document_by_file_name(file_name: str)
    save_document_to_path(shared_id: str, languages: List[str], path: str)
    upload_file(pdf_file_path, share_id, language, title)
    upload_image(image_binary, title, entity_shared_id, language)
    delete_file(id)


<b>Settings</b>

    get()
    get_languages()

<b>Templates</b>

    set(language, template)
    get()

<b>Thesauris</b>

    get(language: str)
    add_value(thesauri_name, thesauri_id, thesauri_values, language)


