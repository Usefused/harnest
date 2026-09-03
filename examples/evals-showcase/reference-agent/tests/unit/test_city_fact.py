def test_city_fact_tool(agent, tools):
    assert agent.name == "city_facts_reference"
    assert tools["get_city_fact"]("Paris") == {
        "city": "Paris",
        "country": "France",
        "landmark": "Eiffel Tower",
    }
