from harnest.agent import tool


_STOCK = {
    "CABLE": 25,
    "DOCK": 4,
}


@tool
def check_stock(sku: str, quantity: int) -> dict[str, object]:
    """Return stock availability for a SKU and requested positive quantity."""
    normalized_sku = sku.strip().upper()
    if normalized_sku not in _STOCK:
        supported = ", ".join(sorted(_STOCK))
        raise ValueError(f"Unsupported SKU '{sku}'. Supported SKUs: {supported}.")
    if quantity <= 0:
        raise ValueError("Quantity must be a positive integer.")

    available = _STOCK[normalized_sku]
    return {
        "sku": normalized_sku,
        "requested": quantity,
        "available": available,
        "can_fulfil": quantity <= available,
    }
