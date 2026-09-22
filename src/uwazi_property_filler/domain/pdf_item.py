from pydantic import BaseModel

from uwazi_property_filler.domain.fill_status import FillStatus


class PdfItem(BaseModel):
    shared_id: str
    title: str = ""
    subtitle: str = ""
    template_name: str
    filename: str = ""
    language: str = "en"
    status: FillStatus = FillStatus.PENDING
