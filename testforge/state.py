from testforge.models import ExecutionStatus

ALLOWED_TRANSITIONS: dict[ExecutionStatus, set[ExecutionStatus]] = {
    ExecutionStatus.QUEUED: {ExecutionStatus.PREPARING, ExecutionStatus.CANCELLED},
    ExecutionStatus.PREPARING: {
        ExecutionStatus.ASSIGNED,
        ExecutionStatus.FAILED,
        ExecutionStatus.CANCELLED,
    },
    ExecutionStatus.ASSIGNED: {
        ExecutionStatus.RUNNING,
        ExecutionStatus.RECONCILING,
        ExecutionStatus.CANCELLED,
    },
    ExecutionStatus.RUNNING: {
        ExecutionStatus.FINALIZING,
        ExecutionStatus.RECONCILING,
    },
    ExecutionStatus.RECONCILING: {
        ExecutionStatus.QUEUED,
        ExecutionStatus.RUNNING,
        ExecutionStatus.FINALIZING,
        ExecutionStatus.FAILED,
        ExecutionStatus.CANCELLED,
    },
    ExecutionStatus.FINALIZING: {
        ExecutionStatus.COMPLETED,
        ExecutionStatus.FAILED,
        ExecutionStatus.CANCELLED,
    },
    ExecutionStatus.COMPLETED: set(),
    ExecutionStatus.FAILED: set(),
    ExecutionStatus.CANCELLED: set(),
}


class InvalidTransition(ValueError):
    pass


def ensure_transition(current: ExecutionStatus, target: ExecutionStatus) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidTransition(f"invalid execution transition: {current.value} -> {target.value}")
