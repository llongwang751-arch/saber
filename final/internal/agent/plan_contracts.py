"""Validated, serializable research plans shared by the planner and review API."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
StepId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
ToolName = Literal["search_web", "read_url", "rag_search", "exec_command"]


class ResearchStep(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: StepId
    title: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    kind: Literal["research", "code", "write"]
    guidance: str = Field(default="", max_length=8000)
    tool_policy: list[ToolName] = Field(
        default_factory=list, max_length=32
    )
    depends_on: list[StepId] = Field(default_factory=list, max_length=24)
    acceptance: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def valid_tools_for_role(self):
        allowed = {
            "research": {"search_web", "read_url", "rag_search"},
            "code": {"exec_command"},
            "write": set(),
        }[self.kind]
        if len(self.tool_policy) != len(set(self.tool_policy)):
            raise ValueError("Plan tool policy must not contain duplicates")
        if set(self.tool_policy) - allowed:
            raise ValueError(f"Tool policy contains tools unavailable to the {self.kind} role")
        return self


class ResearchPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    objective: Annotated[str, StringConstraints(min_length=1, max_length=20000)]
    constraints: list[ShortText] = Field(default_factory=list, max_length=32)
    steps: list[ResearchStep] = Field(min_length=1, max_length=24)

    @model_validator(mode="after")
    def valid_dag(self):
        by_id = {step.id: step for step in self.steps}
        if len(by_id) != len(self.steps):
            raise ValueError("Plan step IDs must be unique")
        visited, visiting = set(), set()

        def visit(step_id):
            if step_id in visiting:
                raise ValueError("Plan dependencies must not contain a cycle")
            if step_id in visited:
                return
            if step_id not in by_id:
                raise ValueError(f"Unknown plan dependency: {step_id}")
            visiting.add(step_id)
            dependencies = by_id[step_id].depends_on
            if len(dependencies) != len(set(dependencies)):
                raise ValueError("Plan dependencies must be unique")
            for dependency in dependencies:
                visit(dependency)
            visiting.remove(step_id)
            visited.add(step_id)

        for step in self.steps:
            visit(step.id)
        return self


def validate_research_plan(value) -> dict:
    """Return a fresh JSON-compatible plan or raise a validation error."""
    return ResearchPlan.model_validate(value).model_dump(mode="json")
