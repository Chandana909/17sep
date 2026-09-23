"""Example mapping transform hook: pure dict -> dict."""


def upper_side(row: dict[str, object]) -> dict[str, object]:
    return {**row, "S": str(row.get("S", "")).upper()}
