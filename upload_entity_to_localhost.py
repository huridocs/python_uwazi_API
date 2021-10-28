from uwazi_api.UwaziAdapter import UwaziAdapter


def upload_entity_to_localhost():
    uwazi_adapter = UwaziAdapter(user='admin', password='admin', url='http://localhost:3000')
    shared_id = uwazi_adapter.entities.upload(entity={'title': 'first_entity'}, language='en')
    print(uwazi_adapter.entities.get_one(shared_id=shared_id, language='en'))
    uwazi_adapter.entities.delete(share_id=shared_id)


if __name__ == '__main__':
    upload_entity_to_localhost()