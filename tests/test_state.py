from itertools import pairwise

import pytest

from testforge.models import ExecutionStatus
from testforge.state import InvalidTransition, ensure_transition


def test_state_machine_rejects_skipping_finalization():
    with pytest.raises(InvalidTransition):
        ensure_transition(ExecutionStatus.RUNNING, ExecutionStatus.COMPLETED)


def test_state_machine_accepts_happy_path():
    path = [
        ExecutionStatus.QUEUED,
        ExecutionStatus.PREPARING,
        ExecutionStatus.ASSIGNED,
        ExecutionStatus.RUNNING,
        ExecutionStatus.FINALIZING,
        ExecutionStatus.COMPLETED,
    ]
    for current, target in pairwise(path):
        ensure_transition(current, target)
