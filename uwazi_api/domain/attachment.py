from pydantic import BaseModel


class Attachment(BaseModel):
    filename: str = ""
