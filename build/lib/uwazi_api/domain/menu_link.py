from typing import Any

from pydantic import BaseModel, Field


class MenuLink(BaseModel):
    """A link entry in the Uwazi "Settings → Links" menu.

    The Uwazi UI surfaces the items in ``/api/settings/links`` as navigation
    links. Each entry has at least a ``title`` (the visible label) and a
    ``type``. The only ``type`` Uwazi currently accepts is ``"link"``, in
    which case a ``url`` is required.
    """

    title: str
    type: str = "link"
    url: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
