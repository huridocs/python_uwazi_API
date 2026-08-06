from pydantic import BaseModel


class UwaziCredentials(BaseModel):
    url: str
    username: str
    password: str

    def make_url_secure(self):
        if "https://" in self.url:
            return

        self.url = self.url.replace("http://", "")
        self.url = "https://" + self.url
