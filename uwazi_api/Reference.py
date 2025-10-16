class SelectionRectangle:
    def __init__(self, top: float, left: float, width: float, height: float, page: str):
        self.top = top
        self.left = left
        self.width = width
        self.height = height
        self.page = page

    def to_dict(self):
        return {
            "top": self.top,
            "left": self.left,
            "width": self.width,
            "height": self.height,
            "page": self.page,
        }


class Reference:
    def __init__(self, text: str, selection_rectangles: list[SelectionRectangle]):
        self.text = text
        self.selection_rectangles = selection_rectangles

    def to_dict(self):
        return {
            "text": self.text,
            "selectionRectangles": [rect.to_dict() for rect in self.selection_rectangles],
        }
