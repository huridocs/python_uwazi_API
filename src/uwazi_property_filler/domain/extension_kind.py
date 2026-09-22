from enum import Enum


class ExtensionKind(str, Enum):
    LOCAL = "local"
    REMOTE = "remote"
