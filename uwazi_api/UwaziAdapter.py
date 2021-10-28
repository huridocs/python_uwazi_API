from uwazi_adapter.CSV import CSV
from uwazi_adapter.Entities import Entities
from uwazi_adapter.Files import Files
from uwazi_adapter.Settings import Settings
from uwazi_adapter.Templates import Templates
from uwazi_adapter.Thesauris import Thesauris
from uwazi_adapter.UwaziRequest import UwaziRequest


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








