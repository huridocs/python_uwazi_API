from uwazi_api.UwaziAdapter import UwaziAdapter


def upload_entity_to_localhost():
    uwazi_adapter = UwaziAdapter(user='admin', password='admin', url='http://localhost:3000')
    entity = {'title': 'title_of_the_entity',
              'template': 'template_id_like_4e57cdd6f54e0a1304c0d5dd',
              'metadata': {'property_name': [{'value': "property_value"}]}}

    shared_id = uwazi_adapter.entities.upload(entity=entity, language='en')
    print(uwazi_adapter.entities.get_one(shared_id=shared_id, language='en'))
    uwazi_adapter.entities.delete(share_id=shared_id)
    print(uwazi_adapter.templates.get())


if __name__ == '__main__':
    upload_entity_to_localhost()