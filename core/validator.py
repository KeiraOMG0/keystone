from __future__ import annotations


class TemplateValidator:
    MAX_TITLE = 256
    MAX_DESC = 4000
    MAX_FOOTER = 2048
    MAX_FIELD_NAME = 256
    MAX_FIELD_VALUE = 1024
    MAX_FIELDS = 25
    MAX_EMBEDS = 10
    MAX_TOTAL_CHARS = 6000

    def validate_embed_data(self, data: dict) -> list[str]:
        errors: list[str] = []

        title = data.get("title") or ""
        description = data.get("description") or ""
        footer = (data.get("footer") or {}).get("text") or ""
        fields = data.get("fields") or []

        if len(title) > self.MAX_TITLE:
            errors.append(f"Title exceeds {self.MAX_TITLE} characters.")
        if len(description) > self.MAX_DESC:
            errors.append(f"Description exceeds {self.MAX_DESC} characters.")
        if len(footer) > self.MAX_FOOTER:
            errors.append(f"Footer text exceeds {self.MAX_FOOTER} characters.")
        if len(fields) > self.MAX_FIELDS:
            errors.append(f"Embed has more than {self.MAX_FIELDS} fields.")

        for i, f in enumerate(fields):
            name = f.get("name") or ""
            value = f.get("value") or ""
            if len(name) > self.MAX_FIELD_NAME:
                errors.append(f"Field {i} name exceeds {self.MAX_FIELD_NAME} characters.")
            if len(value) > self.MAX_FIELD_VALUE:
                errors.append(f"Field {i} value exceeds {self.MAX_FIELD_VALUE} characters.")

        total = len(title) + len(description) + sum(
            len(f.get("name") or "") + len(f.get("value") or "") for f in fields
        )
        if total > self.MAX_TOTAL_CHARS:
            errors.append(f"Total embed characters exceed {self.MAX_TOTAL_CHARS}.")

        return errors

    def validate_url(self, url: str | None) -> str | None:
        if url and url.startswith("https://"):
            return url
        return None
