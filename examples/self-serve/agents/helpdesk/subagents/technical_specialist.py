from harnest.agent import Agent
from harnest.model import OllamaModel


technical_specialist = Agent(
    name="technical_specialist",
    model=OllamaModel.from_environment(),
    description="Diagnoses technical API and integration problems.",
    instruction=(
        "Diagnose technical support issues. Ask for the smallest useful set of "
        "reproduction details, separate evidence from hypotheses, and return next steps."
    ),
)
