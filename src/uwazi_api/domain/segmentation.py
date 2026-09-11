from pydantic import BaseModel, Field


class Paragraph(BaseModel):
    left: float
    top: float
    width: float
    height: float
    page_number: int = Field(alias="pageNumber")
    text: str
    type: str


class Segmentation(BaseModel):
    id: str
    file_id: str = Field(alias="fileId")
    document_id: str = Field(alias="documentId")
    filename: str
    status: str
    xmlname: str | None = None
    page_width: float | None = Field(default=None, alias="pageWidth")
    page_height: float | None = Field(default=None, alias="pageHeight")
    paragraphs: list[Paragraph] = Field(default_factory=list)

    class Config:
        populate_by_name = True
