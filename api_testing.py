import os

import load_dotenv
from datetime import date

import numpy as np

from uwazi_api.client import UwaziClient
from uwazi_api.domain.entity import Entity
from uwazi_api.domain.reference import Reference
from uwazi_api.domain.search_filters import SearchFilters, DateRange, SelectFilter
from uwazi_api.domain.selection_rectangle import SelectionRectangle
import pandas as pd

from uwazi_api.domain.FileType import FileType

load_dotenv.load_dotenv()

UWAZI_USER = os.getenv("UWAZI_USER", "admin")
UWAZI_PASSWORD = os.getenv("UWAZI_PASSWORD", "admin")
UWAZI_URL = os.getenv("UWAZI_URL", "http://localhost:3000")
UWAZI_TEMPLATE_ID = os.getenv("UWAZI_TEMPLATE_ID", "")


def create_relationship():
    client = UwaziClient(user=UWAZI_USER, password=UWAZI_PASSWORD, url=UWAZI_URL)
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
    client.relationships.create(
        file_entity_shared_id="0mg4pkm4y78n",
        file_id="68f098050058648f7a83c35f",
        reference=reference,
        to_entity_shared_id="cos3av69d98",
        relationship_type_id="68f097b60058648f7a83c307",
        language="en",
    )


def search_entities():
    client = UwaziClient(user=UWAZI_USER, password=UWAZI_PASSWORD, url=UWAZI_URL)

    entities = client.entities.search_by_text(
        search_term="Malawi", template_id="68f0c2400058648f7a83d39f", start_from=0, batch_size=300, language="en"
    )

    print(entities)


def loop_entities():
    client = UwaziClient(user=UWAZI_USER, password=UWAZI_PASSWORD, url=UWAZI_URL)

    start = 0
    batch_size = 100
    dataframes = []
    while True:
        batch = client.exports.to_dataframe(
            start_from=start,
            batch_size=batch_size,
            template_name="template_2",
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


def get_dictionaries():
    client = UwaziClient(user=UWAZI_USER, password=UWAZI_PASSWORD, url=UWAZI_URL)
    dictionaries = client.thesauris.get(language="en")
    print(dictionaries)
    return dictionaries


def upload_dataframe(df, template_name):
    client = UwaziClient(user=UWAZI_USER, password=UWAZI_PASSWORD, url=UWAZI_URL)
    return client.csv.upload_dataframe_and_get_shared_id(df=df, template_name=template_name)


def upload_pdf():
    client = UwaziClient(user=UWAZI_USER, password=UWAZI_PASSWORD, url=UWAZI_URL)
    with open("data/test_document.pdf", "rb") as f:
        pdf_bytes = f.read()
        client.files.upload_document_from_bytes(
            file_bytes=pdf_bytes, title="test_document.pdf", share_id="nhjjc7q30oh", language="en"
        )


def upload_odt():
    client = UwaziClient(user=UWAZI_USER, password=UWAZI_PASSWORD, url=UWAZI_URL)
    with open("/home/gabo/Downloads/short.odt", "rb") as f:
        pdf_bytes = f.read()
        client.files.upload_file_from_bytes(
            file_bytes=pdf_bytes, title="short.odt", share_id="nhjjc7q30oh", language="en", file_type=FileType.ODT
        )


def get_templates():
    client = UwaziClient(user=UWAZI_USER, password=UWAZI_PASSWORD, url=UWAZI_URL)
    return client.templates.get()


def search_by_two_properties():
    client = UwaziClient(user=UWAZI_USER, password=UWAZI_PASSWORD, url=UWAZI_URL)
    filters: SearchFilters = SearchFilters()
    filters.add("date", DateRange(from_=date(2026, 2, 1), to=None))
    filters.add("select", SelectFilter(values=["item 2", "missing"]))
    return client.search.search_by_filter_to_dataframe(
        filters=filters, template_name="template_2", language="en", batch_size=100
    )


def update_entity():
    shared_id = "dun73bzdlnj"
    client = UwaziClient(user=UWAZI_USER, password=UWAZI_PASSWORD, url=UWAZI_URL)
    entity = Entity(
        title="Updated entity 1",
        shared_id=shared_id,
        template="template_2",
        language="en",
        metadata={"date": date(2026, 5, 18)},
    )
    return client.entities.upload(entity=entity, language="en")


def upload_entity():
    client = UwaziClient(user=UWAZI_USER, password=UWAZI_PASSWORD, url=UWAZI_URL)
    entity = Entity(
        title="Test 101",
        template="template_2",
        language="es",
        metadata={
            "date": date(2026, 5, 17),
            "numeric": 4321,
        },
    )
    return client.entities.upload(entity=entity, language="en")


def update_partially():
    shared_id = "dun73bzdlnj"
    client = UwaziClient(user=UWAZI_USER, password=UWAZI_PASSWORD, url=UWAZI_URL)
    entity = Entity(
        shared_id=shared_id,
        title="Partially Updated entity 1",
        language="en",
        metadata={"date": date(2026, 8, 15)},
    )
    return client.entities.update_partially(entity=entity, language="en")


def create_entities_from_dataframe():
    client = UwaziClient(user=UWAZI_USER, password=UWAZI_PASSWORD, url=UWAZI_URL)
    data_frame = loop_entities()
    # data_frame["_id"] = np.nan
    # data_frame["sharedId"] = np.nan
    # data_frame["title"] = data_frame["title"].astype(str) + " COPY"
    data_frame["date"] = "2030/01/01"
    return client.entities.create_or_update_entities_from_dataframe(df=data_frame, language="en")


if __name__ == "__main__":
    df = loop_entities()
    # print(df.head().to_string())
    # for x in create_entities_from_dataframe():
    #     print(x.model_dump())
    # update_entity()
    # update_partially()
    # print(upload_entity())
    # print(get_templates())
    df = search_by_two_properties()
    print(df.head().to_string())
    # upload_odt()
    # upload_pdf()

    # df.loc[0, "title"] = "Updated Title via CSV Upload 3"
    # one_row_df = df.head(1).reset_index(drop=True)
    # print(upload_dataframe(one_row_df, template_name="Document"))
