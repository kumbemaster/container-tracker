"""Shipment-wide stage recording: all containers at once, or nothing."""

from django.db import transaction
from django.utils import timezone

from . import stage_rules
from .models import AuditLog, StageEvent

ALREADY_RECORDED = 'Already recorded.'


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
    containers = list(shipment.containers.order_by('number'))
    for container in containers:
        problems = stage_rules.check_can_undo(container, stage)
        if problems:
            return problems[0]
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
