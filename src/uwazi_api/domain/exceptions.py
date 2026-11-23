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


class MissingCredentialsError(AuthenticationError):
    """No username and/or password was supplied — no login was attempted.

    Raised BEFORE any request reaches the Uwazi instance, so a blank field
    never turns into a login attempt (and never into a default account).
    """


class CredentialsRejectedError(AuthenticationError):
    """The instance REFUSED the credentials (401/403) — a wrong password or account.

    Distinct from the other :class:`AuthenticationError` cases (an unreachable
    instance, a missing session cookie): only this one means "the credentials
    were wrong", so only this one may count towards a failed-login lockout.
    """

    def __init__(self, status_code: int) -> None:
        super().__init__(f"Login failed: {status_code}")
        self.status_code = status_code


class PageNotFoundError(DomainError):
    pass


class SegmentationNotFoundError(DomainError):
    pass


class SegmentationError(DomainError):
    pass


class TranslationError(DomainError):
    """Raised when a ``/api/translationService`` call does not yield a translation."""


class TranslationNotAvailableError(TranslationError):
    """Raised when Uwazi refuses the translation endpoint itself.

    ``POST /api/translationService`` is guarded by ``needsAuthorization`` and
    by the ``translationService`` feature flag (a disabled flag answers ``403
    {"error": "Feature not available"}`` before the controller ever runs). A
    401/403/404 here means the endpoint is unreachable for this user/tenant,
    not that the translation itself failed.
    """
