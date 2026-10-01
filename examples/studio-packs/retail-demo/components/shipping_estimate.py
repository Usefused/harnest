from harnest.agent import tool


_SHIPPING = {
    "UK": {"cost": 5.00, "business_days": 2},
    "DE": {"cost": 12.00, "business_days": 5},
}


@tool
def shipping_estimate(country: str, quantity: int) -> dict[str, object]:
    """Return shipping cost and business days for a country and positive quantity."""
    normalized_country = country.strip().upper()
    if normalized_country not in _SHIPPING:
        supported = ", ".join(sorted(_SHIPPING))
        raise ValueError(
            f"Unsupported country '{country}'. Supported countries: {supported}."
        )
    if quantity <= 0:
        raise ValueError("Quantity must be a positive integer.")

    estimate = _SHIPPING[normalized_country]
    return {
        "country": normalized_country,
        "quantity": quantity,
        "currency": "GBP",
        "cost": estimate["cost"],
        "business_days": estimate["business_days"],
    }
