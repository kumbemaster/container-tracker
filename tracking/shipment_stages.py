"""Shipment-wide stage recording: all containers at once, or nothing."""

from django.db import transaction
from django.utils import timezone

from . import stage_rules
from .models import AuditLog, StageEvent

ALREADY_RECORDED = 'Already recorded.'

EMPTY_RETURNED_KEY = 'empty_container_returned'

NOT_RETURNED_REASON = 'The empty container has not been returned yet.'
UNFINISHED_REASON = (
    'Steps have been recorded but the shipment is not finished.'
)


def is_purgeable(shipment):
    """(True, '') when a soft-deleted shipment may be purged.

    Only finished shipments (every container has the empty container
    returned) and never-started ones (no container has any completed
    stage) can be deleted permanently.
    """
    if shipment.deleted_at is None:
        return False, 'The shipment has not been deleted yet.'
    done = {}
    for event in StageEvent.objects.filter(
        container__shipment=shipment
    ).select_related('stage'):
        if event.skipped or event.occurred_at is None:
            continue
        done.setdefault(event.container_id, set()).add(event.stage.key)
    container_ids = list(
        shipment.containers.values_list('pk', flat=True)
    )
    if not any(done.get(pk) for pk in container_ids):
        return True, ''
    if all(
        EMPTY_RETURNED_KEY in done.get(pk, set())
        for pk in container_ids
    ):
        return True, ''
    if any(
        EMPTY_RETURNED_KEY in done.get(pk, set())
        for pk in container_ids
    ):
        return False, NOT_RETURNED_REASON
    return False, UNFINISHED_REASON


def record_container_stage(
    user, shipment, container, stage, when, note, source
):
    """Record one container's stage completion (the Done button path)."""
    with transaction.atomic():
        event, created = StageEvent.objects.get_or_create(
            container=container,
            stage=stage,
            defaults={'occurred_at': when, 'note': note},
        )
        old_value = '' if created else str(event.occurred_at)
        event.occurred_at = when
        event.skipped = False
        event.note = note
        event.save()
        AuditLog.objects.create(
            shipment=shipment,
            container=container,
            user=user,
            action='stage_completed',
            old_value=old_value,
            new_value=str(when),
            source=source,
        )


def _completed_ids(shipment, stage):
    return set(
        StageEvent.objects.filter(
            container__shipment=shipment,
            stage=stage,
            occurred_at__isnull=False,
            skipped=False,
        ).values_list('container_id', flat=True)
    )


def complete_shipment_stage(shipment, stage, user, source):
    """Complete a shipment-scope stage for every container at once.

    All or nothing: every container lacking the stage is validated
    with check_can_complete first; the first problem sentence is
    returned and nothing is recorded. Otherwise every lacking
    container gets the StageEvent with the same current server time
    in one transaction, plus one AuditLog row for the shipment.
    Returns None when recorded.
    """
    containers = list(shipment.containers.order_by('number'))
    done = _completed_ids(shipment, stage)
    lacking = [c for c in containers if c.pk not in done]
    if not lacking:
        return ALREADY_RECORDED
    when = timezone.now()
    for container in lacking:
        problems = stage_rules.check_can_complete(container, stage, when)
        if problems:
            return problems[0]
    with transaction.atomic():
        for container in lacking:
            event, _ = StageEvent.objects.get_or_create(
                container=container, stage=stage
            )
            event.occurred_at = when
            event.skipped = False
            event.save()
        AuditLog.objects.create(
            shipment=shipment,
            container=None,
            user=user,
            action='stage_completed',
            old_value='',
            new_value='%s for %d containers'
            % (stage.label, len(lacking)),
            source=source,
        )
    return None


def undo_shipment_stage(shipment, stage, user, source):
    """Undo a shipment-scope stage for every container at once.

    All or nothing: check_can_undo must pass for every container,
    otherwise the first problem sentence is returned and nothing is
    removed. Returns None when undone.
    """
    problems = stage_rules.check_can_undo_shipment(shipment, stage)
    if problems:
        return problems[0]
    containers = list(shipment.containers.order_by('number'))
    with transaction.atomic():
        StageEvent.objects.filter(
            container__shipment=shipment, stage=stage
        ).delete()
        AuditLog.objects.create(
            shipment=shipment,
            container=None,
            user=user,
            action='stage_undone',
            old_value='%s for %d containers'
            % (stage.label, len(containers)),
            new_value='',
            source=source,
        )
    return None
