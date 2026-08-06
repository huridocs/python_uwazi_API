import pandas as pd

from uwazi_api.domain.property_type import PropertyType
from uwazi_api.use_cases.repositories.template_repository import TemplateRepository
from uwazi_api.use_cases.repositories.thesauri_repository import ThesauriRepository
from uwazi_api.domain.sanitize_property_label import PropertyLabelSanitizer


class ThesauriFromDataframeUseCase:
    def __init__(
        self,
        template_repository: TemplateRepository,
        thesauri_repository: ThesauriRepository,
    ):
        self.template_repo = template_repository
        self.thesauri_repo = thesauri_repository

    def execute(self, df: pd.DataFrame, template_name: str, language: str) -> dict:
        template = self.template_repo.get_by_name(template_name)
        if not template:
            raise ValueError(f"Template '{template_name}' not found")

        thesauri = self.thesauri_repo.get(language)
        thesauri_by_id = {t.id: t for t in thesauri}
        thesauri_by_name = {t.name: t for t in thesauri}

        results = {}
        all_props = template.properties + template.common_properties

        # Build a map: thesauri_id -> list of (property, column_names)
        thesauri_to_props_and_columns = {}

        for prop in all_props:
            if prop.type not in (PropertyType.SELECT, PropertyType.MULTI_SELECT):
                continue
            if not prop.content:
                continue

            # Find the thesauri ID
            thesauri_id = None
            if prop.content in thesauri_by_id:
                thesauri_id = prop.content
            elif prop.content in thesauri_by_name:
                thesauri_id = thesauri_by_name[prop.content].id
            else:
                continue

            # Find matching columns for this property
            column_names = []
            sanitized_prop_name = PropertyLabelSanitizer.sanitize(prop.name)
            for col in df.columns:
                sanitized_col = PropertyLabelSanitizer.sanitize(col)
                if sanitized_col == sanitized_prop_name and col not in column_names:
                    column_names.append(col)

            if thesauri_id not in thesauri_to_props_and_columns:
                thesauri_to_props_and_columns[thesauri_id] = []
            thesauri_to_props_and_columns[thesauri_id].append((prop, column_names))

        # Process each thesauri
        for thesauri_id, props_and_columns in thesauri_to_props_and_columns.items():
            # Get existing values
            existing_values_map = {}
            if thesauri_id in thesauri_by_id:
                existing_values_map = {v.label: v.id for v in thesauri_by_id[thesauri_id].values}

            # Collect all values from all columns
            values_to_add = {}

            for prop, column_names in props_and_columns:
                for col in column_names:
                    if col in df.columns:
                        for val in df[col].dropna().unique():
                            if prop.type == PropertyType.MULTI_SELECT and isinstance(val, str) and "|" in val:
                                individual_vals = [v.strip() for v in val.split("|") if v.strip()]
                                for iv in individual_vals:
                                    if iv and iv not in existing_values_map and iv not in values_to_add:
                                        values_to_add[iv] = iv
                            else:
                                str_val = str(val).strip()
                                if str_val and str_val not in existing_values_map:
                                    if str_val not in values_to_add:
                                        values_to_add[str_val] = str_val

            if values_to_add:
                merged_values = {**existing_values_map, **values_to_add}
                result = self.thesauri_repo.add_value(
                    thesauri_id=thesauri_id,
                    thesauri_values=merged_values,
                    language=language,
                )
                # Record result for all properties in this group
                for prop, _ in props_and_columns:
                    results[prop.name] = result
            else:
                for prop, _ in props_and_columns:
                    results[prop.name] = {"status": "no_new_values"}

        return results
