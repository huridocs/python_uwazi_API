from uwazi_api.CSV import CSV
from uwazi_api.Entities import Entities
from uwazi_api.Files import Files
from uwazi_api.Settings import Settings
from uwazi_api.Templates import Templates
from uwazi_api.Thesauris import Thesauris
from uwazi_api.UwaziRequest import UwaziRequest


class UwaziAdapter(object):
    def __init__(self, user, password, url):
        url = url if url[-1] != '/' else url[:-1]

        self.uwazi_request = UwaziRequest(url, user, password)

        self.entities = Entities(self.uwazi_request)
        self.files = Files(self.uwazi_request, self.entities)
        self.thesauris = Thesauris(self.uwazi_request)
        self.templates = Templates(self.uwazi_request)
        self.settings = Settings(self.uwazi_request)
        self.csv = CSV(self.uwazi_request)








