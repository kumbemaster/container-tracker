"""Server-side tools for the agent assistant.

Every function receives the signed-in user's company from the server;
the model never supplies a company or user argument, and no tool
result ever includes an access code.
"""

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .. import shipment_stages
from .. import stage_rules
from .. import timeline as timeline_helpers
from ..models import (
    AuditLog,
    Container,
    Shipment,
    Stage,
    StageEvent,
)

MAX_SHIPMENTS = 5
OPTIONAL_STAGE_KEYS = (
    'customs_examination',
    'en_route_to_bonded_terminal',
)


def _error(message):
    return {'ok': False, 'error': message}


def _shipment_scope(company, shipment_id):
    try:
        pk = int(shipment_id)
    except (TypeError, ValueError):
        return None
    return (
        Shipment.objects.filter(
            pk=pk, importer__agent_company=company
        )
        .select_related('importer')
        .first()
    )


def _current_key(container):
    for step in timeline_helpers.build_timeline(container):
        if step['state'] == 'current':
            return step['key']
    return None


def _container_info(container):
    return {
        'number': container.number,
        'size': container.size,
        'current_stage': _current_key(container),
        'waiting_on': container.waiting_on,
    }


def _resolve_containers(shipment, container_numbers):
    """Return (found, unknown) for the numbers within this shipment."""
    all_containers = list(
        shipment.containers.all().order_by('number')
    )
    if not container_numbers:
        return all_containers, []
    by_number = {
        container.number.upper(): container
        for container in all_containers
    }
    found, unknown = [], []
    for raw in container_numbers or []:
        key = (raw or '').strip().upper()
        if key in by_number:
            if by_number[key] not in found:
                found.append(by_number[key])
        else:
            unknown.append(raw)
    return found, unknown


def find_shipments(company, query):
    """Find open shipments of this company by BL, container or importer."""
    text = (query or '').strip()
    if not text:
        return _error(
            'Say which shipment you mean: a BL number, '
            'a container number or an importer name.'
        )
    compact = text.replace(' ', '').replace('-', '')
    shipments = (
        Shipment.objects.filter(
            importer__agent_company=company, closed_at__isnull=True
        )
        .select_related('importer')
        .prefetch_related('containers')
        .filter(
            Q(importer__name__icontains=text)
            | Q(bl_number__icontains=text)
            | Q(containers__number__icontains=text)
            | Q(containers__number__icontains=compact)
        )
        .distinct()
        .order_by('-created_at')[:MAX_SHIPMENTS]
    )
    return {
        'ok': True,
        'shipments': [
            {
                'id': shipment.pk,
                'bl_number': shipment.bl_number,
                'importer_name': shipment.importer.name,
                'containers': [
                    _container_info(container)
                    for container in shipment.containers.all()
                ],
            }
            for shipment in shipments
        ],
    }


def get_shipment(company, shipment_id):
    """Show one shipment: completed stages and what can be done now."""
    shipment = _shipment_scope(company, shipment_id)
    if shipment is None:
        return _error(
            'No shipment with that id belongs to your company.'
        )
    now = timezone.now()
    containers = []
    for container in shipment.containers.all().order_by('number'):
        done = StageEvent.objects.filter(
            container=container,
            occurred_at__isnull=False,
            skipped=False,
        ).select_related('stage')
        done_ids = {event.stage_id for event in done}
        completed = [
            {
                'key': event.stage.key,
                'at': event.occurred_at.isoformat(),
            }
            for event in done
        ]
        can_do, blocked = [], []
        for stage in Stage.objects.order_by('order'):
            if stage.pk in done_ids:
                continue
            problems = stage_rules.check_can_complete(
                container, stage, now
            )
            if not problems:
                can_do.append(stage.key)
            else:
                blocked.append(
                    {'key': stage.key, 'problem': problems[0]}
                )
        containers.append(
            {
                'number': container.number,
                'completed': completed,
                'can_do': can_do,
                'blocked': blocked,
            }
        )
    return {
        'ok': True,
        'id': shipment.pk,
        'bl_number': shipment.bl_number,
        'importer_name': shipment.importer.name,
        'containers': containers,
        'scopes': {
            key: scope
            for key, scope in Stage.objects.values_list('key', 'scope')
        },
    }


def _whole_item(kind, shipment, stage):
    return {
        'kind': kind,
        'shipment_id': shipment.pk,
        'stage_key': stage.key,
        'container_number': None,
        'whole_shipment': True,
    }


def _check_pending(company, user, pending, candidates):
    """Append candidates, dry run the whole list, drop failures.

    Returns (results, items) for this call's candidates: results carry
    ok or the problem sentence, items are the surviving candidates.
    """
    start = len(pending)
    pending.extend(candidate for candidate, _label in candidates)
    failures = dry_run_items(company, user, pending)
    results, items = [], []
    for offset, (candidate, label) in enumerate(candidates):
        problem = failures.get(start + offset)
        if problem is None:
            results.append({'number': label, 'ok': True})
            items.append(candidate)
        else:
            results.append(
                {'number': label, 'ok': False, 'problem': problem}
            )
    return results, items


def propose_stage_done(
    company, shipment_id, stage_key, container_numbers=None,
    pending=None, user=None,
):
    """Validate a stage completion; never writes anything.

    A shipment-scope stage ignores container_numbers and proposes one
    whole-shipment item. When pending and user are given (the assistant
    loop), candidates join this request's pending list and the whole
    list is dry run in order; otherwise candidates are validated
    against the current database state.
    """
    shipment = _shipment_scope(company, shipment_id)
    if shipment is None:
        return _error(
            'No shipment with that id belongs to your company.'
        )
    stage = Stage.objects.filter(key=stage_key).first()
    if stage is None:
        return _error('Unknown stage: %s.' % (stage_key,))
    if stage.scope == Stage.Scope.SHIPMENT:
        candidates = [(_whole_item('stage_done', shipment, stage), None)]
        if pending is None or user is None:
            return _direct_whole_result(
                company, shipment, stage, candidates
            )
        results, items = _check_pending(
            company, user, pending, candidates
        )
        return {'ok': True, 'results': results, 'items': items}
    found, unknown = _resolve_containers(shipment, container_numbers)
    candidates = [
        (
            {
                'kind': 'stage_done',
                'shipment_id': shipment.pk,
                'stage_key': stage.key,
                'container_number': container.number,
            },
            container.number,
        )
        for container in found
    ]
    results = [
        {
            'number': raw,
            'ok': False,
            'problem': 'No container %s on this shipment.' % (raw,),
        }
        for raw in unknown
    ]
    if pending is None or user is None:
        now = timezone.now()
        items = []
        for candidate, label in candidates:
            container = shipment.containers.filter(
                number__iexact=label
            ).first()
            problems = stage_rules.check_can_complete(
                container, stage, now
            )
            if problems:
                results.append(
                    {'number': label, 'ok': False,
                     'problem': problems[0]}
                )
            else:
                results.append({'number': label, 'ok': True})
                items.append(candidate)
        return {'ok': True, 'results': results, 'items': items}
    checked, items = _check_pending(company, user, pending, candidates)
    return {'ok': True, 'results': results + checked, 'items': items}


def _direct_whole_result(
    company, shipment, stage, candidates, needs_rules=True
):
    """Validate one whole-shipment candidate against current state."""
    done = set(
        StageEvent.objects.filter(
            container__shipment=shipment,
            stage=stage,
            occurred_at__isnull=False,
            skipped=False,
        ).values_list('container_id', flat=True)
    )
    lacking = [
        container
        for container in shipment.containers.order_by('number')
        if container.pk not in done
    ]
    if not lacking:
        return {
            'ok': True,
            'results': [
                {
                    'number': None,
                    'ok': False,
                    'problem': shipment_stages.ALREADY_RECORDED,
                }
            ],
            'items': [],
        }
    if needs_rules:
        now = timezone.now()
        for container in lacking:
            problems = stage_rules.check_can_complete(
                container, stage, now
            )
            if problems:
                return {
                    'ok': True,
                    'results': [
                        {'number': None, 'ok': False,
                         'problem': problems[0]}
                    ],
                    'items': [],
                }
    return {
        'ok': True,
        'results': [{'number': None, 'ok': True}],
        'items': [candidate for candidate, _label in candidates],
    }


def propose_add_optional_stage(
    company, shipment_id, stage_key, container_numbers=None,
    pending=None, user=None,
):
    """Select an optional stage; never writes anything.

    A shipment-scope stage ignores container_numbers and proposes one
    whole-shipment item.
    """
    if stage_key not in OPTIONAL_STAGE_KEYS:
        return _error(
            'Only customs_examination and '
            'en_route_to_bonded_terminal can be added this way.'
        )
    shipment = _shipment_scope(company, shipment_id)
    if shipment is None:
        return _error(
            'No shipment with that id belongs to your company.'
        )
    stage = Stage.objects.filter(
        key=stage_key, is_optional=True
    ).first()
    if stage is None:
        return _error('Unknown optional stage: %s.' % (stage_key,))
    if stage.scope == Stage.Scope.SHIPMENT:
        candidates = [(_whole_item('select_optional', shipment, stage), None)]
        if pending is None or user is None:
            return _direct_whole_result(
                company, shipment, stage, candidates, needs_rules=False
            )
        results, items = _check_pending(
            company, user, pending, candidates
        )
        return {'ok': True, 'results': results, 'items': items}
    found, unknown = _resolve_containers(shipment, container_numbers)
    candidates = [
        (
            {
                'kind': 'select_optional',
                'shipment_id': shipment.pk,
                'stage_key': stage.key,
                'container_number': container.number,
            },
            container.number,
        )
        for container in found
    ]
    results = [
        {
            'number': raw,
            'ok': False,
            'problem': 'No container %s on this shipment.' % (raw,),
        }
        for raw in unknown
    ]
    if pending is None or user is None:
        items = []
        for candidate, label in candidates:
            container = shipment.containers.filter(
                number__iexact=label
            ).first()
            if StageEvent.objects.filter(
                container=container, stage=stage
            ).exists():
                results.append(
                    {
                        'number': label,
                        'ok': False,
                        'problem': '%s is already recorded or selected '
                        'for %s.' % (stage.label, label),
                    }
                )
            else:
                results.append({'number': label, 'ok': True})
                items.append(candidate)
        return {'ok': True, 'results': results, 'items': items}
    checked, items = _check_pending(company, user, pending, candidates)
    return {'ok': True, 'results': results + checked, 'items': items}


def propose_waiting_on(
    company, shipment_id, container_numbers=None, waiting_on='',
    note='', pending=None, user=None,
):
    """Propose a waiting-on change; never writes anything."""
    valid = [choice[0] for choice in Container.WaitingOn.choices]
    if waiting_on not in valid:
        return _error(
            'Unknown waiting_on value. Use one of: %s.'
            % (', '.join(valid),)
        )
    shipment = _shipment_scope(company, shipment_id)
    if shipment is None:
        return _error(
            'No shipment with that id belongs to your company.'
        )
    found, unknown = _resolve_containers(shipment, container_numbers)
    candidates = [
        (
            {
                'kind': 'waiting_on',
                'shipment_id': shipment.pk,
                'container_number': container.number,
                'waiting_on': waiting_on,
                'note': (note or '').strip(),
            },
            container.number,
        )
        for container in found
    ]
    results = [
        {
            'number': raw,
            'ok': False,
            'problem': 'No container %s on this shipment.' % (raw,),
        }
        for raw in unknown
    ]
    if pending is None or user is None:
        return {
            'ok': True,
            'results': results
            + [{'number': label, 'ok': True} for _, label in candidates],
            'items': [candidate for candidate, _label in candidates],
        }
    checked, items = _check_pending(company, user, pending, candidates)
    return {'ok': True, 'results': results + checked, 'items': items}


def apply_proposal_item(company, user, item, when, source):
    """Apply one proposal item for real.

    Returns (True, recorded message) or (False, problem message).
    Used by the confirm endpoint and by the proposal dry run.
    """
    kind = item.get('kind')
    shipment = Shipment.objects.filter(
        pk=item.get('shipment_id'), importer__agent_company=company
    ).first()
    if shipment is None:
        return False, 'Shipment not found for one item.'
    if item.get('whole_shipment'):
        return _apply_whole_item(company, user, shipment, item, source)
    container = shipment.containers.filter(
        number__iexact=(item.get('container_number') or '').strip()
    ).first()
    if container is None:
        return False, 'No container %s on %s.' % (
            item.get('container_number'), shipment.bl_number)
    if kind == 'stage_done':
        stage = Stage.objects.filter(key=item.get('stage_key')).first()
        if stage is None:
            return False, 'Unknown stage for %s.' % container.number
        problems = stage_rules.check_can_complete(container, stage, when)
        if problems:
            return False, 'Refused %s for %s: %s' % (
                stage.label, container.number, '; '.join(problems))
        shipment_stages.record_container_stage(
            user, shipment, container, stage, when, '', source
        )
        return True, 'Recorded %s for %s.' % (
            stage.label, container.number)
    if kind == 'select_optional':
        stage = Stage.objects.filter(
            key=item.get('stage_key'), is_optional=True
        ).first()
        if stage is None or stage.key not in OPTIONAL_STAGE_KEYS:
            return False, 'Unknown optional stage for %s.' % (
                container.number)
        if StageEvent.objects.filter(
            container=container, stage=stage
        ).exists():
            return False, '%s is already recorded or selected for %s.' % (
                stage.label, container.number)
        StageEvent.objects.create(container=container, stage=stage)
        AuditLog.objects.create(
            shipment=shipment,
            container=container,
            user=user,
            action='stage_selected',
            old_value='',
            new_value=stage.label,
            source=source,
        )
        return True, 'Selected %s for %s.' % (
            stage.label, container.number)
    if kind == 'waiting_on':
        waiting_on = item.get('waiting_on', '')
        note = (item.get('note') or '').strip()
        valid = [choice[0] for choice in Container.WaitingOn.choices]
        if waiting_on not in valid:
            return False, 'Unknown waiting status for %s.' % (
                container.number)
        old_value = container.waiting_on
        container.waiting_on = waiting_on
        if waiting_on == Container.WaitingOn.NOBODY:
            container.waiting_on_since = None
            container.waiting_on_note = ''
        else:
            container.waiting_on_since = when
            container.waiting_on_note = note
        container.save()
        AuditLog.objects.create(
            shipment=shipment,
            container=container,
            user=user,
            action='waiting_changed',
            old_value=old_value,
            new_value=('%s — %s' % (waiting_on, note))
            if note
            else waiting_on,
            source=source,
        )
        return True, 'Updated waiting status for %s.' % container.number
    return False, 'Unknown change for %s.' % container.number


def _apply_whole_item(company, user, shipment, item, source):
    kind = item.get('kind')
    stage = Stage.objects.filter(key=item.get('stage_key')).first()
    if stage is None or stage.scope != Stage.Scope.SHIPMENT:
        return False, 'Unknown shipment step.'
    count = shipment.containers.count()
    if kind == 'stage_done':
        problem = shipment_stages.complete_shipment_stage(
            shipment, stage, user, source
        )
        if problem is None:
            return True, 'Recorded %s for %d containers.' % (
                stage.label, count)
        if problem == shipment_stages.ALREADY_RECORDED:
            return False, problem
        return False, 'Refused %s: %s' % (stage.label, problem)
    if kind == 'select_optional':
        if not stage.is_optional or stage.key not in OPTIONAL_STAGE_KEYS:
            return False, 'Unknown shipment step.'
        existing = StageEvent.objects.filter(
            container__shipment=shipment, stage=stage
        ).values_list('container_id', flat=True)
        lacking = shipment.containers.exclude(pk__in=existing)
        if not lacking.exists():
            return False, shipment_stages.ALREADY_RECORDED
        with transaction.atomic():
            for container in lacking:
                StageEvent.objects.get_or_create(
                    container=container, stage=stage
                )
            AuditLog.objects.create(
                shipment=shipment,
                container=None,
                user=user,
                action='stage_selected',
                old_value='',
                new_value='%s for %d containers'
                % (stage.label, lacking.count()),
                source=source,
            )
        return True, 'Selected %s for %d containers.' % (
            stage.label, count)
    return False, 'Unknown change for %s.' % shipment.bl_number


def dry_run_items(company, user, items):
    """Validate pending items in order inside a rolled-back transaction.

    Later items see the effect of earlier ones. Failing items are
    dropped from the list; returns {original index: problem sentence}.
    Nothing is ever written.
    """
    failures = {}
    when = timezone.now()
    with transaction.atomic():
        for index, item in enumerate(items):
            ok, message = apply_proposal_item(
                company, user, item, when, AuditLog.Source.ASSISTANT
            )
            if not ok:
                failures[index] = message
        transaction.set_rollback(True)
    kept = [
        item for index, item in enumerate(items) if index not in failures
    ]
    items[:] = kept
    return failures


def ask_clarification(question):
    """End the request with a question for the agent."""
    text = (question or '').strip()
    return {
        'ok': True,
        'question': text or 'Which shipment do you mean?',
    }


DISPATCH = {
    'find_shipments': find_shipments,
    'get_shipment': get_shipment,
    'propose_stage_done': propose_stage_done,
    'propose_add_optional_stage': propose_add_optional_stage,
    'propose_waiting_on': propose_waiting_on,
    'ask_clarification': ask_clarification,
}
