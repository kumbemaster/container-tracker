from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from .models import Stage, StageEvent

GATE_OUT_WAITS_FOR_OPTIONAL = ['customs_examination']

DUTY_PAID_KEY = 'duty_paid'
BONDED_KEY = 'en_route_to_bonded_terminal'
GATE_OUT_KEY = 'gate_out'
DUTY_FIRST_KEYS = [
    'shipping_line_processed',
    'terminal_processed',
    'customs_processed',
    BONDED_KEY,
]


def _complete(event):
    return event is not None and not event.skipped and event.occurred_at is not None


def _events_by_key(container):
    return {
        event.stage.key: event
        for event in StageEvent.objects.filter(container=container).select_related(
            'stage'
        )
    }


def _prereq_stages(stage):
    prereqs = list(
        Stage.objects.filter(is_sequenced=True, order__lt=stage.order).order_by(
            'order'
        )
    )
    if stage.key in DUTY_FIRST_KEYS:
        prereqs.append(Stage.objects.get(key=DUTY_PAID_KEY))
    if stage.key == GATE_OUT_KEY:
        prereqs.extend(
            Stage.objects.filter(
                key__in=[
                    'shipping_line_processed',
                    'terminal_processed',
                    'customs_processed',
                ]
            ).order_by('order')
        )
    seen = set()
    ordered = []
    for prereq in prereqs:
        if prereq.id not in seen:
            seen.add(prereq.id)
            ordered.append(prereq)
    return ordered


def check_can_complete(container, stage, when):
    events = _events_by_key(container)
    if _complete(events.get(stage.key)):
        return ['%s is already recorded.' % stage.label]
    problems = []
    if when > timezone.now() + timedelta(minutes=5):
        problems.append('This time is in the future.')
    latest = None
    latest_label = None
    for prereq in _prereq_stages(stage):
        event = events.get(prereq.key)
        if not _complete(event):
            problems.append('%s is not recorded yet.' % prereq.label)
        elif latest is None or event.occurred_at > latest:
            latest = event.occurred_at
            latest_label = prereq.label
    if latest is not None and when < latest:
        problems.append('This time is earlier than %s.' % latest_label)
    if stage.key in DUTY_FIRST_KEYS and not _complete(events.get(DUTY_PAID_KEY)):
        problems.append('Duty paid is not recorded yet.')
    if stage.key == GATE_OUT_KEY:
        for optional_key in GATE_OUT_WAITS_FOR_OPTIONAL:
            event = events.get(optional_key)
            if event is not None and not event.skipped and not _complete(event):
                problems.append('%s is not recorded yet.' % event.stage.label)
    return list(dict.fromkeys(problems))


def _completed_dependents(container, stage):
    events = _events_by_key(container)
    dependents = []
    for event in events.values():
        if not _complete(event):
            continue
        other = event.stage
        if other.id == stage.id:
            continue
        if other.is_sequenced and other.order > stage.order:
            dependents.append((other, event))
        elif stage.key == DUTY_PAID_KEY and (
            other.is_processing_track or other.key == BONDED_KEY
        ):
            dependents.append((other, event))
        elif other.key == GATE_OUT_KEY and (
            stage.is_processing_track
            or stage.key in GATE_OUT_WAITS_FOR_OPTIONAL
        ):
            dependents.append((other, event))
    return dependents


def check_can_undo(container, stage):
    events = _events_by_key(container)
    if not _complete(events.get(stage.key)):
        return ['%s is not recorded yet.' % stage.label]
    problems = []
    for dependent, _event in _completed_dependents(container, stage):
        problems.append(
            '%s depends on %s.' % (dependent.label, stage.label)
        )
    return list(dict.fromkeys(problems))


def selected_optional_keys(container):
    return [
        event.stage.key
        for event in StageEvent.objects.filter(container=container).select_related(
            'stage'
        )
        if event.stage.is_optional and not event.skipped
    ]
