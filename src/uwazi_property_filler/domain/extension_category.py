from enum import Enum


class ExtensionCategory(str, Enum):
    SUGGESTION = "suggestion"
    HIGHLIGHTER = "highlighter"
    DISPLAYER = "displayer"
    SEARCH = "search"
    FILLER = "filler"
