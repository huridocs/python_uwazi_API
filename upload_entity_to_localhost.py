from uwazi_api.Reference import Reference, SelectionRectangle
from uwazi_api.UwaziAdapter import UwaziAdapter
import pandas as pd


def upload_entity_to_localhost():
    uwazi_adapter = UwaziAdapter(user="admin", password="admin", url="http://localhost:3000")
    entity = {
        "title": "title_of_the_entity",
        "template": "template_id_like_4e57cdd6f54e0a1304c0d5dd",
        "metadata": {"property_name": [{"value": "property_value"}]},
    }

    shared_id = uwazi_adapter.entities.upload(entity=entity, language="en")
    print(uwazi_adapter.entities.get_one(shared_id=shared_id, language="en"))
    uwazi_adapter.entities.delete(share_id=shared_id)
    print(uwazi_adapter.templates.get())


def create_relationship():
    uwazi_adapter = UwaziAdapter(user="admin", password="admin", url="http://localhost:3000")
    reference = Reference(
        text="29 DE JULIO DE 1991",
        selection_rectangles=[
            SelectionRectangle(
                top=172.94667742693863,
                left=335.66813738787613,
                width=155.3464233398437,
                height=17.629629629629626,
                page="1",
            ),
        ],
    )
    uwazi_adapter.relationships.create(
        file_entity_shared_id="0mg4pkm4y78n",
        file_id="68f098050058648f7a83c35f",
        reference=reference,
        to_entity_shared_id="cos3av69d98",
        relationship_type_id="68f097b60058648f7a83c307",
        language="en",
    )


def search_entities():
    uwazi_adapter = UwaziAdapter(user="admin", password="admin", url="http://localhost:3000")

    entities = uwazi_adapter.entities.get_from_text(
        search_term="Malawi", template_id="68f0c2400058648f7a83d39f", start_from=0, batch_size=300, language="en"
    )

    print(entities)


def update_entity():
    data = {
        "_id": "69120830deb0c2aa4cfc8f3f",
        "__v": 1,
        "language": "en",
        "metadata": {"foo_date": [{"value": 1794355200}]},
        "sharedId": "6jcdjm1k453",
        "template": "6912059adeb0c2aa4cfc8ec4",
        "title": "1",
    }
    uwazi_adapter = UwaziAdapter(user="admin", password="admin", url="http://localhost:3000")
    uwazi_adapter.entities.upload(entity=data, language="en")


def loop_entities():
    uwazi_adapter = UwaziAdapter(user="admin", password="admin", url="http://localhost:3000")

    start = 0
    batch_size = 100
    entities = []
    while True:
        batch = uwazi_adapter.entities.get(
            start_from=start,
            batch_size=start + batch_size,
            template_id="6912059adeb0c2aa4cfc8ec4",
            language="en",
            published=False,
        )

        if not batch:
            break

        entities += batch
        start += batch_size

    return entities


def convert_entities_to_panda(entities):
    flattened_entities = []
    for entity in entities:
        flattened = {
            "_id": entity.get("_id"),
            "sharedId": entity.get("sharedId"),
            "title": entity.get("title"),
            "template": entity.get("template"),
            "language": entity.get("language"),
            "published": entity.get("published"),
            "creationDate": entity.get("creationDate"),
            "editDate": entity.get("editDate"),
        }

        metadata = entity.get("metadata", {})
        for key, value in metadata.items():
            if isinstance(value, list) and len(value) > 0:
                if isinstance(value[0], dict) and "value" in value[0]:
                    flattened[f"metadata_{key}"] = value[0]["value"]
                else:
                    flattened[f"metadata_{key}"] = value
            else:
                flattened[f"metadata_{key}"] = None

        flattened_entities.append(flattened)

    df = pd.DataFrame(flattened_entities)
    return df


if __name__ == "__main__":
    df = convert_entities_to_panda(loop_entities())
    print(df.to_string())
    print(f"\nDataFrame shape: {df.shape}")
    print(f"\nColumns: {df.columns.tolist()}")
    #
    # update_entity()
    # upload_entity_to_localhost()
    # create_relationship()
    # search_entities()
