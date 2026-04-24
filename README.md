<h3 align="center">Python Uwazi API</h3>

<p align="center">
  A Python client for the <a href="https://www.uwazi.io/">Uwazi</a> API with Pydantic models and pandas support.
</p>

---

## Installation

### From Git
```bash
pip install git+https://github.com/huridocs/python_uwazi_API@2026.4.24.4
```

### Local Development
```bash
git clone https://github.com/huridocs/python_uwazi_API.git
cd python_uwazi_API
pip install -e ".[dev]"
```

**Requirements:** Python >= 3.8

---

## Quick Start

```python
from uwazi_api.client import UwaziClient

client = UwaziClient(
    user='admin',
    password='admin',
    url='http://localhost:3000'
)

# Get an entity
entity = client.entities.get_one(shared_id='shared_id', language='en')

# Get documents
pdf_bytes = client.files.get_document(shared_id='shared_id', language='en')

# Export to DataFrame
import pandas as pd
df = client.exports.to_dataframe(
    start_from=0,
    batch_size=100,
    template_name='template_name',
    language='en',
    published=False
)
```

---

## Client Services

The `UwaziClient` provides access to these services:

| Service | Property | Description |
|---------|----------|-------------|
| Entities | `client.entities` | CRUD operations on entities |
| Templates | `client.templates` | Manage entity templates |
| Files | `client.files` | File upload/download operations |
| Thesauri | `client.thesauris` | Manage thesauri/vocabularies |
| Settings | `client.settings` | System settings and languages |
| Search | `client.search` | Search entities with filters |
| CSV | `client.csv` | CSV import operations |
| Relationships | `client.relationships` | Create entity relationships |
| Exports | `client.exports` | Export entities to DataFrame |

---

## API Reference

### Entities

```python
from uwazi_api.domain.entity import Entity

# Get entities
entity = client.entities.get_one(shared_id='id', language='en')
entity = client.entities.get_by_id(entity_id='internal_id')
entities = client.entities.get(start_from=0, batch_size=100, template_name='tpl', language='en', published=False)
entities = client.entities.get_shared_ids(template='tpl', batch_size=100, unpublished=True)

# Search
entities = client.entities.search_by_text(
    search_term='query',
    template_name='tpl',
    start_from=0,
    batch_size=100,
    language='en'
)

# Search with filters
from uwazi_api.domain.search_filters import SearchFilters, DateRange, SelectFilter

filters = SearchFilters()
filters.add("date_property", DateRange(from_=date(2026, 1, 1), to=None))
filters.add("select_property", SelectFilter(values=["value1", "value2"]))

entities = client.entities.search_by_filter(
    filters=filters,
    template_name='tpl',
    start_from=0,
    batch_size=100,
    language='en',
    published=False
)

# Create/Update
shared_id = client.entities.upload(entity=entity, language='en')
shared_id = client.entities.update_partially(entity=entity, language='en')

# Bulk operations
responses = client.entities.create_or_update_entities_from_dataframe(df=df, language='en')
client.entities.publish_entities(shared_ids=['id1', 'id2'])
client.entities.delete_entities(shared_ids=['id1', 'id2'])
client.entities.delete(shared_id='id')

# DataFrame export
df = client.entities.search_by_filter_to_dataframe(
    filters=filters,
    template_name='tpl',
    language='en',
    batch_size=100
)
```

### Files

```python
# Download
pdf_bytes = client.files.get_document(shared_id='id', language='en')
pdf_bytes = client.files.get_document_by_file_name(file_name='doc.pdf')
client.files.save_document_to_path(shared_id='id', languages=['en'], path='/path/to/save')

# Upload
client.files.upload_file(
    pdf_file_path='/path/to/file.pdf',
    share_id='entity_id',
    language='en',
    title='Document Title'
)

# Upload from bytes
from uwazi_api.domain.FileType import FileType

with open('document.pdf', 'rb') as f:
    client.files.upload_document_from_bytes(
        file_bytes=f.read(),
        shared_id='entity_id',
        language='en',
        title='document.pdf'
    )

# Upload other file types
with open('document.odt', 'rb') as f:
    client.files.upload_file_from_bytes(
        file_bytes=f.read(),
        shared_id='entity_id',
        language='en',
        title='document.odt',
        file_type=FileType.ODT
    )

# Images
client.files.upload_image(
    image_binary=image_bytes,
    title='image.jpg',
    entity_shared_id='entity_id',
    language='en'
)

# Delete
client.files.delete_file(file_id='file_id')
```

### Templates

```python
# Get templates
templates = client.templates.get()
template = client.templates.get_by_name(template_name='Template Name')
template = client.templates.get_by_id(template_id='id')

# Resolve template (accepts name or ID)
template_id = client.templates.resolve_template_id('Template Name or ID')

# Find property in template
prop = client.templates.find_property(template=template, prop_name='property_name')

# Create/Update
client.templates.set(language='en', template=template_dict)

# Clear cache
client.templates.clear_cache()
```

### Thesauri

```python
# Get thesauri
thesauri = client.thesauris.get(language='en')

# Add values
client.thesauris.add_value(
    thesauri_name='Thesaurus Name',
    thesauri_id='thesaurus_id',
    thesauri_values=['New Value 1', 'New Value 2'],
    language='en'
)

# Clear cache
client.thesauris.clear_cache(language='en')
```

### Settings

```python
settings = client.settings.get()
languages = client.settings.get_languages()  # Returns list of language codes
```

### Search

The search service provides the same methods as `client.entities` for search operations:

```python
# Text search
entities = client.search.search_by_text(...)

# Filtered search
entities = client.search.search_by_filter(...)

# To DataFrame
df = client.search.search_by_filter_to_dataframe(...)

# Get entities (same as entities.get)
entities = client.search.get(...)

# Get shared IDs
ids = client.search.get_shared_ids(...)
```

### CSV

```python
import pandas as pd

df = pd.DataFrame({
    'title': ['Entity 1', 'Entity 2'],
    'property1': ['value1', 'value2']
})

# Upload DataFrame
client.csv.upload_dataframe(df=df, template_name='template_name')

# Upload and get shared ID
shared_id = client.csv.upload_dataframe_and_get_shared_id(df=df, template_name='template_name')
```

### Relationships

```python
from uwazi_api.domain.reference import Reference
from uwazi_api.domain.selection_rectangle import SelectionRectangle

reference = Reference(
    text="Reference text",
    selection_rectangles=[
        SelectionRectangle(top=172.94, left=335.66, width=155.34, height=17.62, page="1")
    ]
)

client.relationships.create(
    file_entity_shared_id='source_entity_id',
    file_id='file_id',
    reference=reference,
    to_entity_shared_id='target_entity_id',
    relationship_type_id='relationship_type_id',
    language='en'
)
```

### Exports

```python
# Export entities to DataFrame
df = client.exports.to_dataframe(
    start_from=0,
    batch_size=100,
    template_name='template_name',
    language='en',
    published=False
)
```

---

## Data Models

All responses use Pydantic models for type safety and validation.

### Entity

```python
class Entity(BaseModel):
    id: str | None              # Internal ID
    shared_id: str | None       # Shared ID across translations
    title: str | None
    template: str | None
    language: str | None
    published: bool | None
    creation_date: Any | None
    edit_date: Any | None
    documents: list[Document]
    attachments: list[Attachment]
    metadata: dict[str, Any]
```

### Template

```python
class Template(BaseModel):
    id: str
    name: str
    properties: list[PropertySchema]
    common_properties: list[PropertySchema]
```

### Property Types

Available property types: `text`, `date`, `select`, `numeric`, `daterange`, `multidate`, `link`, `image`, `multidaterange`, `markdown`, `media`, `generatedid`, `multiselect`, `geolocation`, `relationship`

### Thesauri

```python
class Thesauri(BaseModel):
    id: str
    name: str
    values: list[ThesauriValue]

class ThesauriValue(BaseModel):
    label: str
    id: str
```

---

## Configuration

### Environment Variables

Create a `.env` file in your working directory:

```
UWAZI_USER=admin
UWAZI_PASSWORD=admin
UWAZI_URL=http://localhost:3000
```

### Authentication

Authentication is handled automatically when creating the client. The client logs in via `/api/login` and stores the session cookie for subsequent requests.

---

## Error Handling

The library defines custom exceptions:

- `AuthenticationError` - Login failed
- `EntityNotFoundError` - Entity not found
- `UploadError` - Upload failed
- `SearchError` - Search failed
- `TemplateNotFoundError` - Template not found
- `DomainError` - Base exception for all domain errors

---

## Architecture

The library follows Clean Architecture:

- **domain/** - Pydantic models and exceptions
- **ports/** - Abstract interfaces
- **adapters/** - HTTP client implementation with retry logic
- **use_cases/** - Business logic, repositories, and services

---

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Format code
black .

# Lint
ruff check .
```

---

## License

MIT
