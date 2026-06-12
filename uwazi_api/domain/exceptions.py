class DomainError(Exception):
    """Base domain exception."""

    pass


class EntityNotFoundError(DomainError):
    pass


class UploadError(DomainError):
    pass


class SearchError(DomainError):
    pass


class PropertyNotFilterableError(SearchError):
    """Raised when a filter targets a template property that is not flagged
    ``useAsFilter`` (``PropertySchema.filter == True``) in the Uwazi template.

    Carries ``property_name``, ``template_name`` and the list of property
    names that *are* filterable on that template so the caller can suggest
    concrete alternatives.
    """

    def __init__(self, property_name: str, template_name: str, filterable_properties: list[str]):
        self.property_name = property_name
        self.template_name = template_name
        self.filterable_properties = filterable_properties
        super().__init__(
            f"Property '{property_name}' is not filterable on template '{template_name}'. "
            f"Only properties with `use_as_filter` set on the template can be used in "
            f"search_entities_by_filter. Filterable properties on this template: "
            f"{filterable_properties}."
        )


class TemplateNotFoundError(DomainError):
    pass


class AuthenticationError(DomainError):
    pass


class PageNotFoundError(DomainError):
    pass
