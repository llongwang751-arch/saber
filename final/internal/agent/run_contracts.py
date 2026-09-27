"""Run state and errors shared by storage, scheduling and HTTP adapters."""

AWAITING_PLAN_REVIEW = "awaiting_plan_review"
ACTIVE = frozenset({"pending", "running", "cancelling", AWAITING_PLAN_REVIEW})
TERMINAL = frozenset({"completed", "failed", "cancelled", "interrupted"})


class RunNotFound(LookupError):
    pass


class RunConflict(RuntimeError):
    pass


class RunCapacityExceeded(RuntimeError):
    pass


def result_status(response, cancelled=False):
    task = response.get("task") or {}
    if cancelled or response.get("status") == "cancelled":
        return "cancelled"
    if response.get("interrupted") or response.get("status") == "interrupted" or task.get("status") == "interrupted":
        return "interrupted"
    if (response.get("error") or response.get("success") is False
            or response.get("status") == "failed" or task.get("status") == "failed"):
        return "failed"
    return "completed"
