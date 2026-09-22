from enum import Enum


class FillStatus(str, Enum):
    PENDING = "pending"
    VALIDATED = "validated"
