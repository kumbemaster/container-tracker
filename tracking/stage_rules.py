from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from .models import Stage, StageEvent

GATE_OUT_WAITS_FOR_OPTIONAL = ['customs_examination']

DUTY_PAID_KEY = 'duty_paid'
BONDED_KEY = 'en_route_to_bonded_terminal'
GATE_OUT_KEY = 'gate_out'
RELEASE_TRACK_KEYS = [
    'shipping_line_processed',
    'terminal_processed',
    'customs_processed',
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


def _sequenced_chain(stage):
    return list(
        Stage.objects.filter(is_sequenced=True, order__lt=stage.order).order_by(
            'order'
        )
    )


def prerequisites_of(stage, selected=()):
    """The stages that must be completed before this one.

    The single source of truth used by both check_can_complete and
    check_can_undo. A sequenced stage needs the previous sequenced
    stage in Stage.order (gate out also needs the release tracks and
    any selected optional stage it waits for); an unsequenced stage
    needs its starts_after stage; customs examination keeps its chain
    of sequenced stages.
    """
    if stage.key == 'customs_examination':
        return _sequenced_chain(stage)
    prereqs = []
    if stage.is_sequenced:
        previous = (
            Stage.objects.filter(is_sequenced=True, order__lt=stage.order)
            .order_by('-order')
            .first()
        )
        if previous is not None:
            prereqs.append(previous)
    elif stage.starts_after:
        try:
            prereqs.append(Stage.objects.get(key=stage.starts_after))
        except Stage.DoesNotExist:
            pass
    if stage.key == GATE_OUT_KEY:
        for track in Stage.objects.filter(
            key__in=RELEASE_TRACK_KEYS
        ).order_by('order'):
            prereqs.append(track)
        for optional_key in GATE_OUT_WAITS_FOR_OPTIONAL:
            if optional_key in selected:
                try:
                    prereqs.append(Stage.objects.get(key=optional_key))
                except Stage.DoesNotExist:
                    pass
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
    selected = selected_optional_keys(container)
    prereqs = list(_sequenced_chain(stage)) if stage.is_sequenced else []
    known = {prereq.id for prereq in prereqs}
    for prereq in prerequisites_of(stage, selected):
        if prereq.id not in known:
            known.add(prereq.id)
            prereqs.append(prereq)
    latest = None
    latest_label = None
    for prereq in prereqs:
        event = events.get(prereq.key)
        if not _complete(event):
            problems.append('%s is not recorded yet.' % prereq.label)
        elif latest is None or event.occurred_at > latest:
            latest = event.occurred_at
            latest_label = prereq.label
    if latest is not None and when < latest:
        problems.append('This time is earlier than %s.' % latest_label)
    return list(dict.fromkeys(problems))


def check_can_undo(container, stage):
    events = _events_by_key(container)
    if not _complete(events.get(stage.key)):
        return ['%s is not recorded yet.' % stage.label]
    selected = selected_optional_keys(container)
    problems = []
    for event in events.values():
        other = event.stage
        if other.id == stage.id or not _complete(event):
            continue
        for prereq in prerequisites_of(other, selected):
            if prereq.id == stage.id:
                problems.append('Undo %s first.' % other.label)
                break
    return list(dict.fromkeys(problems))


def check_can_undo_shipment(shipment, stage):
    """check_can_undo for every container, naming the container.

    Blocking sentences name the container; "not recorded yet" stays
    plain.
    """
    problems = []
    for container in shipment.containers.order_by('number'):
        for problem in check_can_undo(container, stage):
            if problem.startswith('Undo '):
                problem = '%s (container %s).' % (
                    problem.rstrip('.'),
                    container.number,
                )
            problems.append(problem)
    return problems


def selected_optional_keys(container):
    return [
        event.stage.key
        for event in StageEvent.objects.filter(container=container).select_related(
            'stage'
        )
        if event.stage.is_optional and not event.skipped
    ]
