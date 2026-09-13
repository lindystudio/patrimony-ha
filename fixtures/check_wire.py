"""Assert fixtures have no leaked HA internals."""
import json
from pathlib import Path

FORBIDDEN = {
    "entity_id", "entities", "device_id", "area_id", "latitude", "longitude",
    "lat", "lon", "coordinate", "coordinates", "access_token", "ha_token",
    "street", "city", "country", "address", "postal_code",
}


def walk(obj, found):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in FORBIDDEN or str(k).endswith("_entity_id"):
                found.append(k)
            walk(v, found)
    elif isinstance(obj, list):
        for i in obj:
            walk(i, found)


def main():
    for name in ("demo-state.json",):
        path = Path(__file__).with_name(name)
        doc = json.loads(path.read_text())
        found = []
        walk(doc, found)
        assert not found, (name, found)
        assert doc["schemaVersion"] == 1
        wifi = next(i for c in doc["cards"] for i in c["items"] if i["label"]=="Guest Wi-Fi")
        assert wifi["value"] is True and wifi["valueType"]=="bool"
        assert "entity_id" not in json.dumps(doc)
        print("ok", name)


if __name__ == "__main__":
    main()
