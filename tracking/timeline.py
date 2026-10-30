from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from .models import Stage, StageEvent

STALE_AFTER = timedelta(hours=72)


def _moment(now=None):
    return now if now is not None else timezone.now()


def _is_completed(event):
    return not event.skipped and event.occurred_at is not None


def _container_events(container):
    return list(
        StageEvent.objects.filter(container=container).select_related('stage')
    )


def build_timeline(container, now=None):
    _moment(now)
    stages = list(Stage.objects.order_by('order'))
    events = {event.stage_id: event for event in _container_events(container)}
    duty_paid_done = any(
        event.stage.key == 'duty_paid' and _is_completed(event)
        for event in events.values()
    )
    completed = []
    pending = []
    for stage in stages:
        event = events.get(stage.id)
        if event is not None and event.skipped:
            continue
        if stage.is_optional and event is None:
            continue
        if event is not None and _is_completed(event):
            completed.append((stage, event))
        elif stage.is_processing_track and duty_paid_done:
            pending.append((stage, 'processing'))
        else:
            pending.append((stage, 'upcoming'))
    completed.sort(key=lambda pair: pair[1].occurred_at)
    current_id = None
    if completed:
        current_stage, _ = max(
            completed, key=lambda pair: (pair[1].occurred_at, pair[0].order)
        )
        current_id = current_stage.id
    steps = []
    for stage, event in completed:
        steps.append(
            {
                'key': stage.key,
                'label': stage.label,
                'state': 'current' if stage.id == current_id else 'completed',
                'occurred_at': event.occurred_at,
            }
        )
    pending.sort(key=lambda item: item[0].order)
    for stage, state in pending:
        steps.append(
            {
                'key': stage.key,
                'label': stage.label,
                'state': state,
                'occurred_at': None,
            }
        )
    return steps


PHASE_ORDER = ['arrival', 'release', 'delivery', 'return']


def build_timeline_groups(container, now=None):
    steps = build_timeline(container, now=now)
    phases = dict(Stage.objects.values_list('key', 'phase'))
    shipment = container.shipment
    specs = [
        ('arrival', shipment.port.upper(), 'Arrival and documents'),
        ('release', shipment.port.upper(), 'Processing and release'),
        (
            'delivery',
            shipment.destination_area.upper(),
            'Inland delivery',
        ),
        ('return', 'EMPTY RETURN', 'Return to terminal'),
    ]
    groups = []
    for phase, heading, subtitle in specs:
        group_steps = [
            step for step in steps if phases.get(step['key']) == phase
        ]
        if not group_steps:
            continue
        groups.append(
            {
                'phase': phase,
                'heading': heading,
                'subtitle': subtitle,
                'steps': group_steps,
                'completed_count': sum(
                    1
                    for step in group_steps
                    if step['state'] in ('completed', 'current')
                ),
                'is_current_group': any(
                    step['state'] == 'current' for step in group_steps
                ),
            }
        )
    return groups


def freshness(container, now=None):
    moment = _moment(now)
    latest = (
        StageEvent.objects.filter(container=container)
        .order_by('-updated_at')
        .values_list('updated_at', flat=True)
        .first()
    )
    if latest is None:
        return {'updated_at': None, 'stale': False}
    delivered_done = StageEvent.objects.filter(
        container=container,
        stage__key='delivered',
        skipped=False,
        occurred_at__isnull=False,
    ).exists()
    return {
        'updated_at': latest,
        'stale': (moment - latest) >= STALE_AFTER and not delivered_done,
    }


def holding(container):
    info = {
        'waiting_on': container.waiting_on,
        'waiting_on_label': container.get_waiting_on_display(),
        'waiting_on_since': container.waiting_on_since,
        'waiting_on_note': container.waiting_on_note,
        'pending_processing': [],
    }
    duty_paid_done = StageEvent.objects.filter(
        container=container,
        stage__key='duty_paid',
        skipped=False,
        occurred_at__isnull=False,
    ).exists()
    if not duty_paid_done:
        return info
    resolved_ids = set(
        StageEvent.objects.filter(
            container=container
        )
        .filter(Q(skipped=True) | Q(skipped=False, occurred_at__isnull=False))
        .values_list('stage_id', flat=True)
    )
    labels = [
        stage.label
        for stage in Stage.objects.filter(is_processing_track=True).order_by(
            'order'
        )
        if stage.id not in resolved_ids
    ]
    info['pending_processing'] = labels
    return info
