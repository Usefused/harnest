from harnest.agent import tool


_PRICES_GBP = {
    "CABLE": 12.50,
    "DOCK": 79.00,
}


@tool
def quote_price(sku: str, quantity: int) -> dict[str, object]:
    """Return GBP pricing for a SKU and requested positive quantity."""
    normalized_sku = sku.strip().upper()
    if normalized_sku not in _PRICES_GBP:
        supported = ", ".join(sorted(_PRICES_GBP))
        raise ValueError(f"Unsupported SKU '{sku}'. Supported SKUs: {supported}.")
    if quantity <= 0:
        raise ValueError("Quantity must be a positive integer.")

    unit_price = _PRICES_GBP[normalized_sku]
    discount_percent = 10 if quantity >= 10 else 0
    subtotal = unit_price * quantity
    discount_amount = subtotal * discount_percent / 100
    total = subtotal - discount_amount

    return {
        "sku": normalized_sku,
        "quantity": quantity,
        "currency": "GBP",
        "unit_price": round(unit_price, 2),
        "discount_percent": discount_percent,
        "subtotal": round(subtotal, 2),
        "discount_amount": round(discount_amount, 2),
        "total": round(total, 2),
    }
