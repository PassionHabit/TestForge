from datetime import timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from testforge.config import Settings
from testforge.models import (
    DeliveryOutbox,
    Execution,
    ExecutionStatus,
    ResultKind,
    utcnow,
)


def dispatch_pending(db: Session, settings: Settings, limit: int = 100) -> dict[str, int]:
    events = db.scalars(
        select(DeliveryOutbox)
        .where(
            DeliveryOutbox.status == "PENDING",
            DeliveryOutbox.next_attempt_at <= utcnow(),
        )
        .order_by(DeliveryOutbox.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    delivered = failed = 0
    with httpx.Client(timeout=settings.callback_timeout_seconds) as client:
        for event in events:
            event.attempts += 1
            try:
                response = client.post(
                    event.destination_url,
                    json=event.payload,
                    headers={"Idempotency-Key": event.id},
                )
                response.raise_for_status()
                event.status = "DELIVERED"
                event.delivered_at = utcnow()
                event.last_error = None
                execution = db.get(Execution, event.execution_id)
                if execution:
                    execution.delivery_complete = True
                    if execution.status is ExecutionStatus.FINALIZING:
                        from testforge.services import transition

                        if (
                            execution.cancel_requested
                            or execution.result_kind is ResultKind.CANCELLED
                        ):
                            terminal = ExecutionStatus.CANCELLED
                        elif (
                            execution.result_kind is ResultKind.PASSED
                            and execution.cleanup_complete
                            and execution.artifacts_sealed
                        ):
                            terminal = ExecutionStatus.COMPLETED
                        else:
                            terminal = ExecutionStatus.FAILED
                        transition(db, execution, terminal, delivery_id=event.id)
                delivered += 1
            except httpx.HTTPError as exc:
                event.last_error = str(exc)[:2000]
                if event.attempts >= settings.callback_max_attempts:
                    event.status = "DEAD_LETTER"
                    execution = db.get(Execution, event.execution_id)
                    if execution and execution.status is ExecutionStatus.FINALIZING:
                        from testforge.services import transition

                        execution.result_kind = ResultKind.INFRA_FAILED
                        execution.failure_reason = "analysis delivery exhausted all retries"
                        transition(
                            db,
                            execution,
                            ExecutionStatus.FAILED,
                            delivery_id=event.id,
                        )
                else:
                    delay = min(2**event.attempts, 3600)
                    event.next_attempt_at = utcnow() + timedelta(seconds=delay)
                failed += 1
    db.commit()
    return {"processed": len(events), "delivered": delivered, "failed": failed}
