from harnest.tool import tool


@tool
def get_city_fact(city: str) -> dict[str, str]:
    """Return the trusted capital and landmark facts for a supported city."""

    facts = {
        "Paris": {"country": "France", "landmark": "Eiffel Tower"},
        "London": {"country": "United Kingdom", "landmark": "Big Ben"},
    }
    if city not in facts:
        return {"error": f"No verified facts are available for {city}."}
    return {"city": city, **facts[city]}
