import json
import os
import time

import load_dotenv

from uwazi_api.client import UwaziClient
from uwazi_api.domain.property_schema import PropertySchema
from uwazi_api.domain.property_type import PropertyType
from uwazi_api.domain.template import Template

load_dotenv.load_dotenv()

UWAZI_USER = os.getenv("UWAZI_USER", "admin")
UWAZI_PASSWORD = os.getenv("UWAZI_PASSWORD", "admin")
UWAZI_URL = os.getenv("UWAZI_URL", "http://localhost:3000")


# Probe values to round-trip through the real API. The first three are the
# documented options; the rest are guesses to see whether Uwazi silently
# accepts/rejects/coerces them.
CANDIDATE_STYLES = [
    "cover",
    "fill",
    "fit",
    "",  # legacy empty string (Uwazi's stored sentinel)
    "contain",
    "stretch",
    "center",
    "scale-down",
    "thumbnail",
    "tile",
    "none",
    "Cover",  # wrong case
    "FIT",
    "full",
    "crop",
]


def _delete_template(client: UwaziClient, template_id: str) -> int | None:
    resp = client.http.request_adapter.delete(
        url=f"{client.http.url}/api/templates",
        params={"_id": template_id},
        headers=client.http.headers,
        cookies={},
    )
    return getattr(resp, "status_code", None)


def probe(style_value, property_type: PropertyType):
    client = UwaziClient(user=UWAZI_USER, password=UWAZI_PASSWORD, url=UWAZI_URL)

    safe_name = f"style_probe_{property_type.value}_{int(time.time() * 1000)}"
    # Build the template as a raw dict so unknown style values are sent over
    # the wire as-is. PropertySchema's validator would otherwise reject any
    # string that isn't a PropertyStyle member before Uwazi ever sees it,
    # which defeats the whole point of the probe.
    template = Template(
        name=safe_name,
        properties=[
            PropertySchema(
                name="probe_prop",
                label="Probe Prop",
                type=property_type,
            )
        ],
    )
    payload = template.model_dump(by_alias=True, exclude_none=True)
    payload["properties"][0]["style"] = style_value

    result = {
        "input": style_value,
        "type": property_type.value,
        "create_status": None,
        "create_body": None,
        "read_back_style": None,
        "read_back_full": None,
        "raw_stored_style": None,
        "delete_status": None,
        "error": None,
    }

    try:
        create_resp = client.http.request_adapter.post(
            url=f"{client.http.url}/api/templates",
            headers=client.http.headers,
            cookies={"locale": "en"},
            data=json.dumps(payload),
        )
        result["create_status"] = create_resp.status_code
        try:
            result["create_body"] = create_resp.json()
        except Exception:
            result["create_body"] = create_resp.text[:500]

        if create_resp.status_code in (200, 201):
            body = create_resp.json()
            template_id = body.get("_id") or (body.get("template") or {}).get("_id")
            if not template_id and isinstance(body, dict):
                for v in body.values():
                    if isinstance(v, dict) and v.get("_id"):
                        template_id = v["_id"]
                        break
        else:
            template_id = None
    except Exception as e:
        result["error"] = f"create exception: {e!r}"
        template_id = None

    if template_id:
        try:
            templates = client.templates.get()
            read_back = next((t for t in templates if t.name == safe_name), None)
            if read_back is not None:
                prop = next(
                    (p for p in read_back.properties if p.name == "probe_prop"),
                    None,
                )
                if prop is not None:
                    result["read_back_style"] = prop.style.value if prop.style is not None else None
                    result["read_back_full"] = {
                        "name": prop.name,
                        "type": prop.type.value,
                        "style": prop.style.value if prop.style is not None else None,
                    }
        except Exception as e:
            result["error"] = (result["error"] or "") + f" read exception: {e!r}"

        # Also fetch the raw JSON so we can see what Uwazi actually stored,
        # independent of the local PropertyStyle enum.
        try:
            raw_resp = client.http.request_adapter.get(
                url=f"{client.http.url}/api/templates",
                headers=client.http.headers,
                cookies={},
            )
            raw_rows = json.loads(raw_resp.text).get("rows", [])
            raw_match = next((t for t in raw_rows if t.get("name") == safe_name), None)
            if raw_match is not None:
                raw_prop = next(
                    (p for p in raw_match.get("properties", []) if p.get("name") == "probe_prop"),
                    None,
                )
                if raw_prop is not None:
                    result["raw_stored_style"] = raw_prop.get("style")
        except Exception as e:
            result["error"] = (result["error"] or "") + f" raw read exception: {e!r}"

        try:
            del_status = _delete_template(client, template_id)
            result["delete_status"] = del_status
        except Exception as e:
            result["error"] = (result["error"] or "") + f" delete exception: {e!r}"
        client.templates.clear_cache()

    return result, template_id


def main():
    print(f"Probing {UWAZI_URL} as {UWAZI_USER}\n")
    for prop_type in (PropertyType.IMAGE, PropertyType.PREVIEW):
        print(f"=== Property type: {prop_type.value} ===")
        for value in CANDIDATE_STYLES:
            r, _ = probe(value, prop_type)
            print(
                f"  input={r['input']!r:18}  "
                f"create={r['create_status']}  "
                f"raw_stored={r['raw_stored_style']!r:20}  "
                f"read_back_style={r['read_back_style']!r}  "
                f"err={r['error']}"
            )
            if r["create_status"] not in (200, 201) and r["create_body"] is not None:
                snippet = str(r["create_body"])[:200]
                print(f"      body: {snippet}")
        print()


if __name__ == "__main__":
    main()
