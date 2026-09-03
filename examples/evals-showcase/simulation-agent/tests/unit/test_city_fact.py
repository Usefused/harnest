def test_city_fact_tool(agent, tools):
    assert agent.name == "city_facts_simulation"
    assert tools["get_city_fact"]("Paris")["country"] == "France"
