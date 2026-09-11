"""A stand-in provider SDK, vendored so the fixture's own tests can run."""


class AcmePayClient:
    def __init__(self, api_version: str = "v1") -> None:
        self.api_version = api_version

    def post(self, path: str, json: dict, headers: dict | None = None,
             timeout: float | None = None) -> dict:
        missing = [field for field in REQUIRED_FIELDS if field not in json]
        if missing:
            raise ValueError(f"missing required field(s): {missing}")
        return {"path": path, **json}


REQUIRED_FIELDS = ["amount"]
