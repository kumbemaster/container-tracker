"""Server-side tools for the agent assistant.

Every function receives the signed-in user's company from the server;
the model never supplies a company or user argument, and no tool
result ever includes an access code.
"""

from django.db.models import Q
from django.utils import timezone

from .. import stage_rules
from .. import timeline as timeline_helpers
from ..models import Container, Shipment, Stage, StageEvent

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
    }


def propose_stage_done(
    company, shipment_id, stage_key, container_numbers=None
):
    """Validate a stage completion per container; never writes anything."""
    shipment = _shipment_scope(company, shipment_id)
    if shipment is None:
        return _error(
            'No shipment with that id belongs to your company.'
        )
    stage = Stage.objects.filter(key=stage_key).first()
    if stage is None:
        return _error('Unknown stage: %s.' % (stage_key,))
    found, unknown = _resolve_containers(shipment, container_numbers)
    now = timezone.now()
    results, items = [], []
    for raw in unknown:
        results.append(
            {
                'number': raw,
                'ok': False,
                'problem': 'No container %s on this shipment.'
                % (raw,),
            }
        )
    for container in found:
        problems = stage_rules.check_can_complete(
            container, stage, now
        )
        if problems:
            results.append(
                {
                    'number': container.number,
                    'ok': False,
                    'problem': problems[0],
                }
            )
        else:
            results.append({'number': container.number, 'ok': True})
            items.append(
                {
                    'kind': 'stage_done',
                    'shipment_id': shipment.pk,
                    'stage_key': stage.key,
                    'container_number': container.number,
                }
            )
    return {'ok': True, 'results': results, 'items': items}


def propose_add_optional_stage(
    company, shipment_id, stage_key, container_numbers=None
):
    """Select an optional stage for containers; never writes anything."""
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
    found, unknown = _resolve_containers(shipment, container_numbers)
    results, items = [], []
    for raw in unknown:
        results.append(
            {
                'number': raw,
                'ok': False,
                'problem': 'No container %s on this shipment.'
                % (raw,),
            }
        )
    for container in found:
        if StageEvent.objects.filter(
            container=container, stage=stage
        ).exists():
            results.append(
                {
                    'number': container.number,
                    'ok': False,
                    'problem': '%s is already recorded or selected '
                    'for %s.' % (stage.label, container.number),
                }
            )
        else:
            results.append({'number': container.number, 'ok': True})
            items.append(
                {
                    'kind': 'select_optional',
                    'shipment_id': shipment.pk,
                    'stage_key': stage.key,
                    'container_number': container.number,
                }
            )
    return {'ok': True, 'results': results, 'items': items}


def propose_waiting_on(
    company, shipment_id, container_numbers=None, waiting_on='',
    note=''
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
    results, items = [], []
    for raw in unknown:
        results.append(
            {
                'number': raw,
                'ok': False,
                'problem': 'No container %s on this shipment.'
                % (raw,),
            }
        )
    for container in found:
        results.append({'number': container.number, 'ok': True})
        items.append(
            {
                'kind': 'waiting_on',
                'shipment_id': shipment.pk,
                'container_number': container.number,
                'waiting_on': waiting_on,
                'note': (note or '').strip(),
            }
        )
    return {'ok': True, 'results': results, 'items': items}


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
