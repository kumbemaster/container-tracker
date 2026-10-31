import json
import logging
import mimetypes
import secrets
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.views import LoginView
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Case, Count, F, Max, When
from django.forms.utils import ErrorDict
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from . import access as access_helpers
from . import shipment_stages
from . import stage_rules
from . import timeline as timeline_helpers
from .ai.assistant import HISTORY_LIMIT, run_assistant
from .ai import assistant_tools
from .ai.extract_bl import extract_shipment
from .forms import ShipmentForm, normalise_number
from .models import (
    AuditLog,
    Container,
    Document,
    Importer,
    ProposedAction,
    Shipment,
    StaffMember,
    Stage,
    StageEvent,
)
from .validators import is_valid_container_number

logger = logging.getLogger(__name__)

LOGIN_FAILURE_MESSAGE = 'Incorrect username or password.'
LOGIN_LOCKED_MESSAGE = 'Too many attempts. Please try again in 15 minutes.'


def _agent_company(request):
    user = request.user
    if not user.is_authenticated:
        return None
    try:
        return user.staffmember.agent_company
    except StaffMember.DoesNotExist:
        return None


def agent_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        company = _agent_company(request)
        if company is None:
            return redirect(
                '%s?next=%s' % (settings.LOGIN_URL, request.path)
            )
        request.agent_company = company
        return view(request, *args, **kwargs)

    return wrapper


class AgentLoginView(LoginView):
    template_name = 'tracking/agent_login.html'

    def _keys(self):
        username = self.request.POST.get('username', '')
        ip_key = 'login-ip:%s' % self.request.META.get('REMOTE_ADDR', '')
        return ip_key, 'login-user:%s' % username

    def post(self, request, *args, **kwargs):
        ip_key, user_key = self._keys()
        if access_helpers.is_locked(ip_key) or access_helpers.is_locked(
            user_key
        ):
            form = self.get_form()
            form.is_valid()
            form._errors = ErrorDict()
            form.add_error(None, LOGIN_LOCKED_MESSAGE)
            return self.render_to_response(self.get_context_data(form=form))
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        ip_key, user_key = self._keys()
        access_helpers.record_failure(ip_key)
        access_helpers.record_failure(user_key)
        form._errors = ErrorDict()
        form.add_error(None, LOGIN_FAILURE_MESSAGE)
        return super().form_invalid(form)


def _current_label(container):
    steps = timeline_helpers.build_timeline(container)
    current = next(
        (step for step in steps if step['state'] == 'current'), None
    )
    return current['label'] if current is not None else 'Not started'


def _current_label_and_order(container, order_by_key):
    steps = timeline_helpers.build_timeline(container)
    current = next(
        (step for step in steps if step['state'] == 'current'), None
    )
    if current is None:
        return 'Not started', -1
    return current['label'], order_by_key.get(current['key'], -1)


@agent_required
@require_GET
def shipment_list(request):
    show_closed = request.GET.get('show') == 'closed'
    shipments = (
        Shipment.objects.filter(importer__agent_company=request.agent_company)
        .select_related('importer')
        .prefetch_related('containers')
        .annotate(
            container_count=Count('containers', distinct=True),
            last_update=Max('containers__stageevent__updated_at'),
            is_closed=Case(When(closed_at__isnull=True, then=0), default=1),
        )
        .order_by('is_closed', F('last_update').asc(nulls_first=True))
    )
    if not show_closed:
        shipments = shipments.filter(closed_at__isnull=True)
    now = timezone.now()
    order_by_key = dict(Stage.objects.values_list('key', 'order'))
    rows = []
    for shipment in shipments:
        ranked = [
            _current_label_and_order(container, order_by_key)
            for container in shipment.containers.all()
        ]
        best = max(ranked, key=lambda item: item[1])[0] if ranked else 'Not started'
        if shipment.last_update is None:
            days = None
        else:
            days = max(0, (now - shipment.last_update).days)
        rows.append(
            {
                'shipment': shipment,
                'current_step': best,
                'days_since_update': days,
            }
        )
    return render(
        request,
        'tracking/agent_shipments.html',
        {'rows': rows, 'show_closed': show_closed},
    )


def _random_code():
    return '%06d' % secrets.randbelow(1000000)


@agent_required
def shipment_new(request):
    company = request.agent_company
    context = {
        'uncertain': [],
        'prefill': False,
        'scan_document': None,
        'importer_check': False,
    }
    if request.method == 'POST':
        form = ShipmentForm(company, request.POST)
        if form.is_valid():
            with transaction.atomic():
                importer = form.cleaned_data['importer']
                if importer is None:
                    importer, _ = Importer.objects.get_or_create(
                        agent_company=company,
                        name=form.importer_name(),
                        defaults={'access_code': _random_code()},
                    )
                shipment = Shipment.objects.create(
                    importer=importer,
                    bl_number=form.cleaned_data['bl_number'],
                    shipping_line=form.cleaned_data['shipping_line'],
                    port=form.cleaned_data['port'],
                    destination_area=form.cleaned_data['destination_area'],
                )
                for number, size in form.container_rows():
                    Container.objects.create(
                        shipment=shipment, number=number, size=size
                    )
                _link_scan_document(request, company, shipment)
            return redirect('agent_shipment_detail', pk=shipment.pk)
        _restore_scan_context(request, company, form, context)
    else:
        initial = {}
        raw_preselected = request.GET.get('importer') or ''
        if raw_preselected.isdigit():
            preselected = Importer.objects.filter(
                pk=int(raw_preselected), agent_company=company
            ).first()
            if preselected is not None:
                initial['importer'] = preselected
        form = ShipmentForm(company, initial=initial)
        _apply_scan_prefill(request, company, form, context)
    context['form'] = form
    context['duplicate_shipment'] = form.duplicate_shipment
    return render(request, 'tracking/agent_shipment_new.html', context)


def _scan_session_key(document_pk):
    return 'bl_scan_%d' % document_pk


def _scan_result(request, company, raw_id):
    """Return (document_pk, result) for a company-owned scan, else None."""
    if not (raw_id or '').isdigit():
        return None
    document = Document.objects.filter(
        pk=int(raw_id), company=company
    ).first()
    if document is None:
        return None
    result = request.session.get(_scan_session_key(document.pk))
    if not isinstance(result, dict):
        return None
    return document.pk, result


def _flag_scan_rows(rows):
    for row in rows:
        number = normalise_number(row.get('number'))
        row['check'] = (not is_valid_container_number(number)) or not (
            row.get('size') or ''
        ).strip()


def _apply_scan_prefill(request, company, form, context):
    """Prefill the unbound form from a scan result kept in the session."""
    found = _scan_result(request, company, request.GET.get('scan'))
    if found is None:
        return
    document_pk, result = found
    uncertain = [
        field for field in result.get('uncertain') or [] if field
    ]
    if result.get('bl_number'):
        form.initial['bl_number'] = result['bl_number']
    for key in ('shipping_line', 'port', 'destination_area'):
        if result.get(key):
            form.initial[key] = result[key]
    importer_name = (result.get('importer_name') or '').strip()
    if importer_name:
        match = Importer.objects.filter(
            agent_company=company, name__iexact=importer_name
        ).first()
        if match is not None:
            form.initial['importer'] = match.pk
        else:
            form.initial['new_importer_name'] = importer_name
    rows = []
    for item in result.get('containers') or []:
        rows.append(
            {
                'number': item.get('number') or '',
                'size': item.get('size') or '',
                'errors': [],
                'check': (not item.get('valid'))
                or not item.get('size'),
            }
        )
    if rows:
        form.rows = rows
    for field in ('bl_number', 'shipping_line', 'port', 'destination_area'):
        if field in uncertain:
            form.fields[field].widget.attrs['class'] = 'check-field'
    context.update(
        {
            'uncertain': uncertain,
            'prefill': True,
            'scan_document': document_pk,
            'importer_check': 'importer_name' in uncertain,
        }
    )


def _restore_scan_context(request, company, form, context):
    """Keep the scan banner and highlights when a prefilled POST fails."""
    found = _scan_result(
        request, company, request.POST.get('scan_document')
    )
    if found is None:
        return
    document_pk, result = found
    uncertain = [
        field for field in result.get('uncertain') or [] if field
    ]
    _flag_scan_rows(form.rows)
    for field in ('bl_number', 'shipping_line', 'port', 'destination_area'):
        if field in uncertain:
            form.fields[field].widget.attrs['class'] = 'check-field'
    context.update(
        {
            'uncertain': uncertain,
            'prefill': True,
            'scan_document': document_pk,
            'importer_check': 'importer_name' in uncertain,
        }
    )


def _link_scan_document(request, company, shipment):
    """Link a scan document to a newly saved shipment."""
    found = _scan_result(
        request, company, request.POST.get('scan_document')
    )
    if found is None:
        return
    document_pk, _ = found
    Document.objects.filter(pk=document_pk, company=company).update(
        shipment=shipment
    )
    request.session.pop(_scan_session_key(document_pk), None)


def _scoped_shipment(request, pk):
    return get_object_or_404(
        Shipment, pk=pk, importer__agent_company=request.agent_company
    )


def _scoped_container(shipment, container_id):
    return get_object_or_404(Container, pk=container_id, shipment=shipment)


def _updated_ago(updated_at, now):
    if updated_at is None:
        return 'Never updated'
    seconds = max(0, int((now - updated_at).total_seconds()))
    if seconds < 60:
        return 'Updated just now'
    minutes = seconds // 60
    if minutes < 60:
        unit = 'minute' if minutes == 1 else 'minutes'
        return 'Updated %d %s ago' % (minutes, unit)
    hours = minutes // 60
    if hours < 24:
        unit = 'hour' if hours == 1 else 'hours'
        return 'Updated %d %s ago' % (hours, unit)
    days = hours // 24
    unit = 'day' if days == 1 else 'days'
    return 'Updated %d %s ago' % (days, unit)


def _log(
    request,
    shipment,
    container,
    action,
    old_value='',
    new_value='',
    source=AuditLog.Source.MANUAL,
):
    AuditLog.objects.create(
        shipment=shipment,
        container=container,
        user=request.user,
        action=action,
        old_value=old_value,
        new_value=new_value,
        source=source,
    )


@agent_required
@require_POST
def stage_update(request, pk):
    shipment = _scoped_shipment(request, pk)
    stage = get_object_or_404(Stage, key=request.POST.get('stage_key'))
    if stage.scope == Stage.Scope.SHIPMENT:
        messages.error(
            request,
            '%s is a shipment step. Record it for the whole shipment.'
            % stage.label,
        )
        return redirect('agent_shipment_detail', pk=shipment.pk)
    when = timezone.now()
    note = request.POST.get('note', '').strip()
    container_ids = request.POST.getlist('containers')
    if not container_ids:
        messages.error(request, 'Choose at least one container.')
        return redirect('agent_shipment_detail', pk=shipment.pk)
    for container_id in container_ids:
        container = _scoped_container(shipment, container_id)
        problems = stage_rules.check_can_complete(container, stage, when)
        if problems:
            messages.error(
                request,
                'Refused %s for %s: %s'
                % (stage.label, container.number, ' '.join(problems)),
            )
            continue
        shipment_stages.record_container_stage(
            request.user, shipment, container, stage, when, note,
            AuditLog.Source.MANUAL,
        )
        messages.success(
            request, 'Recorded %s for %s.' % (stage.label, container.number)
        )
    return redirect('agent_shipment_detail', pk=shipment.pk)


@agent_required
@require_POST
def stage_select(request, pk):
    shipment = _scoped_shipment(request, pk)
    stage = get_object_or_404(Stage, key=request.POST.get('stage_key'))
    if stage.scope == Stage.Scope.SHIPMENT:
        messages.error(
            request,
            '%s is a shipment step. Record it for the whole shipment.'
            % stage.label,
        )
        return redirect('agent_shipment_detail', pk=shipment.pk)
    if not stage.is_optional:
        raise Http404('No Stage matches the given query.')
    container_ids = request.POST.getlist('containers')
    if not container_ids:
        messages.error(request, 'Choose at least one container.')
        return redirect('agent_shipment_detail', pk=shipment.pk)
    for container_id in container_ids:
        container = _scoped_container(shipment, container_id)
        event, created = StageEvent.objects.get_or_create(
            container=container, stage=stage
        )
        if created:
            _log(request, shipment, container, 'stage_selected',
                 new_value=stage.label)
            messages.success(
                request,
                'Selected %s for %s.' % (stage.label, container.number),
            )
        elif event.occurred_at is not None and not event.skipped:
            messages.error(
                request,
                '%s is already recorded for %s.'
                % (stage.label, container.number),
            )
        else:
            messages.error(
                request,
                '%s is already selected for %s.'
                % (stage.label, container.number),
            )
    return redirect('agent_shipment_detail', pk=shipment.pk)


@agent_required
@require_POST
def stage_unselect(request, pk):
    shipment = _scoped_shipment(request, pk)
    container = _scoped_container(shipment, request.POST.get('container'))
    stage = get_object_or_404(
        Stage, key=request.POST.get('stage_key'), is_optional=True
    )
    try:
        event = StageEvent.objects.get(container=container, stage=stage)
    except StageEvent.DoesNotExist:
        event = None
    if event is None or event.occurred_at is not None or event.skipped:
        messages.error(
            request,
            'Only a pending selection of %s can be removed for %s.'
            % (stage.label, container.number),
        )
    else:
        event.delete()
        _log(
            request,
            shipment,
            container,
            'stage_unselected',
            old_value=stage.label,
        )
        messages.success(
            request,
            'Removed %s for %s.' % (stage.label, container.number),
        )
    return redirect('agent_shipment_detail', pk=shipment.pk)


@agent_required
@require_POST
def stage_undo(request, pk):
    shipment = _scoped_shipment(request, pk)
    container = _scoped_container(shipment, request.POST.get('container'))
    stage = get_object_or_404(Stage, key=request.POST.get('stage_key'))
    if stage.scope == Stage.Scope.SHIPMENT:
        messages.error(
            request,
            '%s is a shipment step. Undo it for the whole shipment.'
            % stage.label,
        )
        return redirect('agent_shipment_detail', pk=shipment.pk)
    problems = stage_rules.check_can_undo(container, stage)
    if problems:
        dependents = stage_rules._completed_dependents(container, stage)
        if dependents:
            first = sorted(
                dependents, key=lambda pair: pair[1].occurred_at
            )[0][0].label
            hint = 'Undo %s first.' % first
        else:
            hint = ' '.join(problems)
        messages.error(
            request,
            'Cannot undo %s for %s. %s'
            % (stage.label, container.number, hint),
        )
        return redirect('agent_shipment_detail', pk=shipment.pk)
    event = StageEvent.objects.get(container=container, stage=stage)
    old_value = str(event.occurred_at)
    event.delete()
    _log(
        request,
        shipment,
        container,
        'stage_undone',
        old_value=old_value,
    )
    messages.success(
        request, 'Undid %s for %s.' % (stage.label, container.number)
    )
    return redirect('agent_shipment_detail', pk=shipment.pk)


@agent_required
@require_POST
def stage_shipment_done(request, pk):
    shipment = _scoped_shipment(request, pk)
    stage = get_object_or_404(Stage, key=request.POST.get('stage_key'))
    if stage.scope != Stage.Scope.SHIPMENT:
        messages.error(
            request,
            '%s is recorded per container.' % stage.label,
        )
        return redirect('agent_shipment_detail', pk=shipment.pk)
    problem = shipment_stages.complete_shipment_stage(
        shipment, stage, request.user, AuditLog.Source.MANUAL
    )
    if problem is None:
        messages.success(
            request,
            'Recorded %s for %d containers.'
            % (stage.label, shipment.containers.count()),
        )
    elif problem == shipment_stages.ALREADY_RECORDED:
        messages.error(request, problem)
    else:
        messages.error(
            request, 'Refused %s: %s' % (stage.label, problem)
        )
    return redirect('agent_shipment_detail', pk=shipment.pk)


@agent_required
@require_POST
def stage_shipment_undo(request, pk):
    shipment = _scoped_shipment(request, pk)
    stage = get_object_or_404(Stage, key=request.POST.get('stage_key'))
    if stage.scope != Stage.Scope.SHIPMENT:
        messages.error(
            request,
            '%s is undone per container.' % stage.label,
        )
        return redirect('agent_shipment_detail', pk=shipment.pk)
    problem = shipment_stages.undo_shipment_stage(
        shipment, stage, request.user, AuditLog.Source.MANUAL
    )
    if problem is None:
        messages.success(
            request,
            'Undid %s for %d containers.'
            % (stage.label, shipment.containers.count()),
        )
    else:
        messages.error(
            request, 'Cannot undo %s. %s' % (stage.label, problem)
        )
    return redirect('agent_shipment_detail', pk=shipment.pk)


@agent_required
@require_POST
def waiting_update(request, pk):
    shipment = _scoped_shipment(request, pk)
    container = _scoped_container(shipment, request.POST.get('container'))
    waiting_on = request.POST.get('waiting_on', '')
    note = request.POST.get('note', '').strip()
    valid = [choice[0] for choice in Container.WaitingOn.choices]
    if waiting_on not in valid:
        messages.error(request, 'Choose who the container is waiting on.')
        return redirect('agent_shipment_detail', pk=shipment.pk)
    old_value = container.waiting_on
    if waiting_on != old_value:
        container.waiting_on = waiting_on
        if waiting_on == Container.WaitingOn.NOBODY:
            container.waiting_on_since = None
            container.waiting_on_note = ''
        else:
            container.waiting_on_since = timezone.now()
            container.waiting_on_note = note
        container.save()
        _log(
            request,
            shipment,
            container,
            'waiting_changed',
            old_value=old_value,
            new_value='%s — %s' % (waiting_on, note) if note else waiting_on,
        )
        messages.success(
            request, 'Updated waiting status for %s.' % container.number
        )
    return redirect('agent_shipment_detail', pk=shipment.pk)


@agent_required
@require_GET
def shipment_detail(request, pk):
    shipment = _scoped_shipment(request, pk)
    containers = list(shipment.containers.order_by('number'))
    optional_stages = list(
        Stage.objects.filter(is_optional=True).order_by('order')
    )
    stages_by_key = {stage.key: stage for stage in Stage.objects.all()}
    now = timezone.now()
    entries = []
    optional_keys = [stage.key for stage in optional_stages]
    for container in containers:
        steps = [
            step
            for step in timeline_helpers.build_timeline(container, now=now)
            if stages_by_key[step['key']].scope == Stage.Scope.CONTAINER
        ]
        event_keys = set(
            StageEvent.objects.filter(container=container).values_list(
                'stage__key', flat=True
            )
        )
        pending_keys = set(
            StageEvent.objects.filter(
                container=container, occurred_at__isnull=True, skipped=False
            ).values_list('stage__key', flat=True)
        )
        actionable_keys = {
            step['key']
            for step in steps
            if step['state'] in ('upcoming', 'processing')
            and not stage_rules.check_can_complete(
                container, stages_by_key[step['key']], now
            )
        }
        latest_update = (
            StageEvent.objects.filter(container=container)
            .order_by('-updated_at')
            .values_list('updated_at', flat=True)
            .first()
        )
        entries.append(
            {
                'container': container,
                'current_step': _current_label(container),
                'steps': steps,
                'pending_keys': pending_keys,
                'actionable_keys': actionable_keys,
                'updated_ago': _updated_ago(latest_update, now),
                'addable_keys': [
                    key for key in optional_keys if key not in event_keys
                ],
            }
        )
    shipment_steps = []
    for stage in Stage.objects.filter(
        scope=Stage.Scope.SHIPMENT
    ).order_by('order'):
        done_ids = set(
            StageEvent.objects.filter(
                container__shipment=shipment,
                stage=stage,
                occurred_at__isnull=False,
                skipped=False,
            ).values_list('container_id', flat=True)
        )
        if containers and all(
            container.pk in done_ids for container in containers
        ):
            latest = (
                StageEvent.objects.filter(
                    container__shipment=shipment,
                    stage=stage,
                    occurred_at__isnull=False,
                    skipped=False,
                )
                .order_by('-occurred_at')
                .values_list('occurred_at', flat=True)
                .first()
            )
            shipment_steps.append(
                {'stage': stage, 'state': 'completed',
                 'occurred_at': latest}
            )
            continue
        lacking = [
            container
            for container in containers
            if container.pk not in done_ids
        ]
        if lacking and not any(
            stage_rules.check_can_complete(container, stage, now)
            for container in lacking
        ):
            state = 'actionable'
        else:
            state = 'upcoming'
        shipment_steps.append(
            {'stage': stage, 'state': state, 'occurred_at': None}
        )
    message = 'Track your shipment %s at %s with this code: %s' % (
        shipment.bl_number,
        request.build_absolute_uri('/'),
        shipment.importer.access_code,
    )
    history = (
        AuditLog.objects.filter(shipment=shipment)
        .select_related('user', 'container')
        .order_by('-created_at')[:20]
    )
    return render(
        request,
        'tracking/agent_shipment_detail.html',
        {
            'shipment': shipment,
            'entries': entries,
            'shipment_steps': shipment_steps,
            'message': message,
            'stages': Stage.objects.order_by('order'),
            'optional_stages': optional_stages,
            'optional_keys': [stage.key for stage in optional_stages],
            'waiting_choices': Container.WaitingOn.choices,
            'history': history,
        },
    )


@agent_required
@require_GET
def importer_list(request):
    company = request.agent_company
    importers = (
        Importer.objects.filter(agent_company=company)
        .annotate(shipment_count=Count('shipment'))
        .order_by('name')
    )
    new_importer = None
    added = request.GET.get('added') or ''
    if added.isdigit():
        new_importer = Importer.objects.filter(
            pk=int(added), agent_company=company
        ).first()
    return render(
        request,
        'tracking/agent_importers.html',
        {
            'importers': importers,
            'new_importer': new_importer,
            'site': request.build_absolute_uri('/'),
        },
    )


@agent_required
@require_POST
def importer_add(request):
    company = request.agent_company
    name = (request.POST.get('name') or '').strip()
    if not name:
        messages.error(request, 'Enter an importer name.')
    elif len(name) > 200:
        messages.error(
            request, 'Importer name must be 200 characters or fewer.'
        )
    elif Importer.objects.filter(
        agent_company=company, name__iexact=name
    ).exists():
        messages.error(
            request, 'An importer called %s already exists.' % name
        )
    else:
        importer = Importer.objects.create(
            agent_company=company, name=name, access_code=_random_code()
        )
        return redirect(
            '%s?added=%d' % (reverse('agent_importers'), importer.pk)
        )
    return redirect('agent_importers')


@agent_required
@require_POST
def importer_rotate(request, pk):
    importer = get_object_or_404(
        Importer, pk=pk, agent_company=request.agent_company
    )
    access_helpers.rotate_code(importer, _random_code())
    return redirect('agent_importers')


SCAN_MAX_BYTES = 8 * 1024 * 1024
SCAN_DAILY_LIMIT = 50
SCAN_CONTENT_TYPES = {'image/jpeg', 'image/png'}
_JPEG_SIG = b'\xff\xd8\xff'
_PNG_SIG = b'\x89PNG\r\n\x1a\n'


def _scan_upload_error(upload):
    """Check the content type, size and file signature of a scan upload."""
    if upload is None:
        return 'Choose a JPEG or PNG file to scan.'
    if upload.content_type not in SCAN_CONTENT_TYPES:
        return 'Only JPEG or PNG files can be scanned.'
    if upload.size > SCAN_MAX_BYTES:
        return 'The file must be 8 MB or smaller.'
    head = upload.read(8)
    upload.seek(0)
    if head[:3] != _JPEG_SIG and head != _PNG_SIG:
        return 'Only JPEG or PNG files can be scanned.'
    return None


@agent_required
def shipment_scan(request):
    company = request.agent_company
    if request.method == 'POST':
        upload = request.FILES.get('scan')
        error = _scan_upload_error(upload)
        if error is None and Document.objects.filter(
            company=company,
            kind=Document.Kind.BL,
            created_at__date=timezone.localdate(),
        ).count() >= SCAN_DAILY_LIMIT:
            error = 'You have reached the daily scan limit (50 per day).'
        if error is not None:
            return render(
                request, 'tracking/agent_scan.html', {'error': error}
            )
        data = upload.read()
        document = Document.objects.create(
            company=company,
            uploaded_by=request.user,
            kind=Document.Kind.BL,
        )
        extension = '.png' if data[:4] == _PNG_SIG[:4] else '.jpg'
        document.file.save(
            'scan-%d%s' % (document.pk, extension), ContentFile(data)
        )
        try:
            result = extract_shipment(data)
        except Exception:
            logger.warning('bl scan failed document=%d', document.pk)
            return render(
                request, 'tracking/agent_scan.html', {'scan_failed': True}
            )
        request.session[_scan_session_key(document.pk)] = result
        return redirect(
            '%s?scan=%d' % (reverse('agent_shipment_new'), document.pk)
        )
    return render(request, 'tracking/agent_scan.html', {})


@agent_required
@require_GET
def document_file(request, pk):
    document = get_object_or_404(
        Document, pk=pk, company=request.agent_company
    )
    content_type, _ = mimetypes.guess_type(document.file.name)
    return FileResponse(
        document.file.open('rb'),
        content_type=content_type or 'application/octet-stream',
    )


@agent_required
@require_POST
def importer_rename(request, pk):
    importer = get_object_or_404(
        Importer, pk=pk, agent_company=request.agent_company
    )
    name = (request.POST.get('name') or '').strip()
    if not name:
        messages.error(request, 'Enter an importer name.')
    elif len(name) > 200:
        messages.error(
            request, 'Importer name must be 200 characters or fewer.'
        )
    elif Importer.objects.filter(
        agent_company=request.agent_company, name__iexact=name
    ).exclude(pk=importer.pk).exists():
        messages.error(
            request, 'An importer called %s already exists.' % name
        )
    else:
        importer.name = name
        importer.save(update_fields=['name'])
        messages.success(
            request, 'Renamed importer to %s.' % importer.name
        )
    return redirect('agent_importers')


def _assistant_history_text(result):
    kind = result.get('type')
    if kind == 'proposal':
        return 'Proposed:\n' + '\n'.join(result.get('summary') or [])
    if kind == 'question':
        return result.get('question', '')
    return result.get('text', '')


@agent_required
@require_POST
def assistant_run(request):
    try:
        payload = json.loads(request.body or '{}')
    except ValueError:
        return JsonResponse(
            {'type': 'message', 'text': 'That request was not valid.'},
            status=400,
        )
    if not isinstance(payload, dict):
        return JsonResponse(
            {'type': 'message', 'text': 'That request was not valid.'},
            status=400,
        )
    text = payload.get('text', '')
    history = request.session.get('assistant_history', [])
    if not isinstance(history, list):
        history = []
    result = run_assistant(request.user, text, history)
    history = history + [
        {'role': 'user', 'content': (text or '')[:500]},
        {'role': 'assistant', 'content': _assistant_history_text(result)},
    ]
    request.session['assistant_history'] = history[-HISTORY_LIMIT:]
    return JsonResponse(result)


def _scoped_proposal(request, proposal_id):
    try:
        pk = int(proposal_id)
    except (TypeError, ValueError):
        return None
    return ProposedAction.objects.filter(
        pk=pk, company=request.agent_company
    ).first()


def _apply_assistant_item(request, company, item, now, recorded, refused):
    ok, message = assistant_tools.apply_proposal_item(
        company,
        request.user,
        item,
        now,
        AuditLog.Source.ASSISTANT,
    )
    if ok:
        recorded.append(message)
    else:
        refused.append(message)


@agent_required
@require_POST
def assistant_confirm(request):
    try:
        payload = json.loads(request.body or '{}')
    except ValueError:
        return JsonResponse(
            {'ok': False, 'error': 'That request was not valid.'},
            status=400,
        )
    proposal = _scoped_proposal(request, (payload or {}).get('proposal_id'))
    if proposal is None:
        return JsonResponse(
            {'ok': False, 'error': 'Proposal not found.'}, status=404
        )
    if proposal.user_id != request.user.pk:
        return JsonResponse(
            {
                'ok': False,
                'error': 'Only the agent who created this proposal '
                'can confirm it.',
            },
            status=400,
        )
    if proposal.status != ProposedAction.Status.PENDING:
        return JsonResponse(
            {
                'ok': False,
                'error': 'This proposal has already been %s.'
                % (proposal.status,),
            },
            status=400,
        )
    if proposal.is_expired():
        proposal.status = ProposedAction.Status.EXPIRED
        proposal.save(update_fields=['status'])
        return JsonResponse(
            {
                'ok': False,
                'error': 'This proposal has expired. '
                'Ask the assistant again.',
            },
            status=400,
        )
    now = timezone.now()
    recorded, refused = [], []
    for item in proposal.actions or []:
        _apply_assistant_item(
            request, request.agent_company, item, now, recorded, refused
        )
    proposal.status = ProposedAction.Status.CONFIRMED
    proposal.save(update_fields=['status'])
    request.session.pop('assistant_history', None)
    return JsonResponse(
        {'ok': True, 'recorded': recorded, 'refused': refused}
    )


@agent_required
@require_POST
def assistant_cancel(request):
    try:
        payload = json.loads(request.body or '{}')
    except ValueError:
        return JsonResponse(
            {'ok': False, 'error': 'That request was not valid.'},
            status=400,
        )
    proposal = _scoped_proposal(request, (payload or {}).get('proposal_id'))
    if proposal is None:
        return JsonResponse(
            {'ok': False, 'error': 'Proposal not found.'}, status=404
        )
    if proposal.user_id != request.user.pk:
        return JsonResponse(
            {
                'ok': False,
                'error': 'Only the agent who created this proposal '
                'can cancel it.',
            },
            status=400,
        )
    if proposal.status != ProposedAction.Status.PENDING:
        return JsonResponse(
            {
                'ok': False,
                'error': 'This proposal has already been %s.'
                % (proposal.status,),
            },
            status=400,
        )
    proposal.status = ProposedAction.Status.CANCELLED
    proposal.save(update_fields=['status'])
    request.session.pop('assistant_history', None)
    return JsonResponse({'ok': True})
