class PropertyLabelSanitizer:
    @staticmethod
    def sanitize(name: str) -> str:
        sanitized = name.strip().lower()
        sanitized = "".join(ch if ch.isalnum() else "_" for ch in sanitized)
        return sanitized


if __name__ == "__main__":
    print(PropertyLabelSanitizer.sanitize("Title"))
    print(PropertyLabelSanitizer.sanitize("Date added"))
    print(PropertyLabelSanitizer.sanitize("Date modified"))
    print(PropertyLabelSanitizer.sanitize("Custom Property 1"))
    print(PropertyLabelSanitizer.sanitize("Custom-Property-2"))
    print(PropertyLabelSanitizer.sanitize("المهنة"))
