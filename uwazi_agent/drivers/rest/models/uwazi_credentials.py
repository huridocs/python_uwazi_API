from pydantic import BaseModel


class UwaziCredentials(BaseModel):
    url: str
    username: str
    password: str
