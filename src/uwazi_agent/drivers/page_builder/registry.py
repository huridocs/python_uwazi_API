import json
import yaml
from pathlib import Path
from typing import Any


class BlockSchema:
    def __init__(
        self,
        name: str,
        description: str,
        when_to_use: str,
        required_slots: dict[str, Any],
        optional_slots: dict[str, Any],
    ) -> None:
        self.name = name
        self.description = description
        self.when_to_use = when_to_use
        self.required_slots = required_slots
        self.optional_slots = optional_slots

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "when_to_use": self.when_to_use,
            "required_slots": _serialize_slots(self.required_slots),
            "optional_slots": _serialize_slots(self.optional_slots),
        }

    def validate(self, slots: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        for key, schema in self.required_slots.items():
            if key not in slots:
                errors.append(f"Missing required slot '{key}' for block '{self.name}'")
                continue
            errors.extend(_validate_value(key, slots[key], schema, self.name))
        for key, value in slots.items():
            if key in self.required_slots:
                continue
            if key in self.optional_slots:
                errors.extend(_validate_value(key, value, self.optional_slots[key], self.name))
            else:
                errors.append(f"Unknown slot '{key}' for block '{self.name}'")
        return errors


def _serialize_slots(slots: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, schema in slots.items():
        entry: dict[str, Any] = {"type": schema["type"], "description": schema.get("description", "")}
        if "default" in schema:
            entry["default"] = schema["default"]
        if "enum" in schema:
            entry["enum"] = schema["enum"]
        if "item_schema" in schema:
            item_schema = schema["item_schema"]
            if "type" in item_schema:
                entry["item_schema"] = _serialize_single_slot(item_schema)
            else:
                entry["item_schema"] = _serialize_slots(item_schema)
        result[key] = entry
    return result


def _serialize_single_slot(schema: dict[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {"type": schema["type"], "description": schema.get("description", "")}
    if "default" in schema:
        entry["default"] = schema["default"]
    if "enum" in schema:
        entry["enum"] = schema["enum"]
    return entry


def _validate_value(key: str, value: Any, schema: dict[str, Any], block_name: str) -> list[str]:
    errors: list[str] = []
    expected_type = schema["type"]

    if expected_type == "string":
        if not isinstance(value, str):
            errors.append(f"Slot '{key}' in block '{block_name}' must be a string, got {type(value).__name__}")
        elif "enum" in schema and value not in schema["enum"]:
            errors.append(f"Slot '{key}' in block '{block_name}' must be one of {schema['enum']}, got '{value}'")
    elif expected_type == "integer":
        if not isinstance(value, int):
            errors.append(f"Slot '{key}' in block '{block_name}' must be an integer, got {type(value).__name__}")
        elif "enum" in schema and value not in schema["enum"]:
            errors.append(f"Slot '{key}' in block '{block_name}' must be one of {schema['enum']}, got {value}")
    elif expected_type == "list":
        if not isinstance(value, list):
            errors.append(f"Slot '{key}' in block '{block_name}' must be a list, got {type(value).__name__}")
        elif "item_schema" in schema:
            item_schema = schema["item_schema"]
            if "type" in item_schema:
                item_type = item_schema["type"]
                if item_type == "block_reference":
                    pass  # validated at render time
                else:
                    for idx, item in enumerate(value):
                        errors.extend(_validate_value(f"{key}[{idx}]", item, item_schema, block_name))
            else:
                for idx, item in enumerate(value):
                    if isinstance(item, dict):
                        for ik, ischema in item_schema.items():
                            if not isinstance(ischema, dict):
                                continue
                            if ik in item:
                                errors.extend(_validate_value(f"{key}[{idx}].{ik}", item[ik], ischema, block_name))
    elif expected_type == "block_reference":
        pass  # Validated at render time

    return errors


class BlockRegistry:
    def __init__(self, blocks_dir: Path) -> None:
        self._blocks: dict[str, BlockSchema] = {}
        self._blocks_dir = blocks_dir
        self._load_blocks()

    def _load_blocks(self) -> None:
        for meta_path in sorted(self._blocks_dir.glob("*/meta.yaml")):
            with open(meta_path) as f:
                data = yaml.safe_load(f)
            name = data["name"]
            self._blocks[name] = BlockSchema(
                name=name,
                description=data["description"],
                when_to_use=data["when_to_use"],
                required_slots=data.get("required_slots", {}),
                optional_slots=data.get("optional_slots", {}),
            )

    def list_blocks(self) -> list[dict[str, Any]]:
        return [b.to_dict() for b in self._blocks.values()]

    def get_block(self, name: str) -> BlockSchema:
        if name not in self._blocks:
            raise ValueError(f"Unknown block type: '{name}'. Available: {list(self._blocks.keys())}")
        return self._blocks[name]

    def validate(self, block_name: str, slots: dict[str, Any]) -> list[str]:
        schema = self.get_block(block_name)
        return schema.validate(slots)

    def get_block_template_path(self, name: str) -> Path:
        self.get_block(name)  # Validate exists
        return self._blocks_dir / name / "block.html.j2"


class VibeRegistry:
    def __init__(self, vibes_dir: Path) -> None:
        self._vibes: dict[str, dict[str, str]] = {}
        self._vibes_dir = vibes_dir
        self._load_vibes()

    def _load_vibes(self) -> None:
        for vibe_path in sorted(self._vibes_dir.glob("*.json")):
            name = vibe_path.stem
            with open(vibe_path) as f:
                self._vibes[name] = json.load(f)

    def list_vibes(self) -> list[str]:
        return sorted(self._vibes.keys())

    def get_vibe(self, name: str) -> dict[str, str]:
        if name not in self._vibes:
            raise ValueError(f"Unknown vibe: '{name}'. Available: {self.list_vibes()}")
        return dict(self._vibes[name])

    def get_vibe_tokens(self, name: str, overrides: dict[str, str] | None = None) -> str:
        tokens = self.get_vibe(name)
        if overrides:
            tokens.update(overrides)
        return "\n".join(f"  {k}: {v};" for k, v in tokens.items())
