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
    dataframes = []
    while True:
        batch = uwazi_adapter.entities.get_pandas_dataframe(
            start_from=start,
            batch_size=start + batch_size,
            template_id="6912059adeb0c2aa4cfc8ec4",
            language="en",
            published=False,
        )

        if batch is None or (isinstance(batch, pd.DataFrame) and batch.empty):
            break

        dataframes.append(batch)
        start += batch_size

    if dataframes:
        combined_df = pd.concat(dataframes, ignore_index=True)
        return combined_df
    else:
        return pd.DataFrame()


def convert_dates(dataframe: pd.DataFrame = None, template_id: str = "") -> pd.DataFrame:
    if dataframe is None or dataframe.empty:
        return dataframe

    uwazi_adapter = UwaziAdapter(user="admin", password="admin", url="http://localhost:3000")
    templates = uwazi_adapter.templates.get()

    template = None
    for t in templates:
        if t["_id"] == template_id:
            template = t
            break

    if template is None:
        print(f"Template {template_id} not found")
        return dataframe

    date_columns = set()

    for prop in template.get("commonProperties", []):
        if prop.get("type") == "date":
            prop_name = prop.get("name")
            if prop_name in dataframe.columns:
                date_columns.add(prop_name)

    for prop in template.get("properties", []):
        if prop.get("type") == "date":
            prop_name = prop.get("name")
            metadata_col = f"metadata_{prop_name}"
            if metadata_col in dataframe.columns:
                date_columns.add(metadata_col)

    df_copy = dataframe.copy()

    for col in date_columns:
        if col in ["creationDate", "editDate"]:
            df_copy[col] = pd.to_datetime(df_copy[col], unit="ms", errors="coerce").dt.strftime("%Y/%m/%d %H:%M:%S")
        else:
            df_copy[col] = pd.to_datetime(df_copy[col], unit="s", errors="coerce").dt.strftime("%Y/%m/%d %H:%M:%S")

    return df_copy


def get_dictionaries():
    uwazi_adapter = UwaziAdapter(user="admin", password="admin", url="http://localhost:3000")
    dictionaries = uwazi_adapter.thesauris.get(language="en")
    print(dictionaries)
    return dictionaries


if __name__ == "__main__":
    df = loop_entities()
    df_converted = convert_dates(dataframe=df, template_id="6912059adeb0c2aa4cfc8ec4")
    print(df_converted.to_string())
