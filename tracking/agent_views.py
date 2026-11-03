import hashlib
import hmac
import json
import logging
import mimetypes
import re
import secrets
import struct
from datetime import timedelta
from functools import wraps
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, update_session_auth_hash
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.views import LoginView
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.db.models import Case, Count, F, Max, Q, When
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
from .ai.pdfs import PROTECTED_PDF_MESSAGE, PdfError
from .ai.transcribe import transcribe_audio
from .forms import ShipmentForm, normalise_number
from .models import (
    AccessAttempt,
    AgentCompany,
    AuditLog,
    CompanyEvent,
    Container,
    Document,
    Importer,
    ProposedAction,
    SecurityProfile,
    Shipment,
    StaffMember,
    Stage,
    StageEvent,
)
from .validators import (
    expected_check_digit,
    has_container_format,
    is_valid_container_number,
)

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
        return ip_key, 'login-user:%s' % username.strip().lower()

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

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['allow_signup'] = getattr(
            settings, 'ALLOW_AGENT_SIGNUP', True
        )
        context['reset_done'] = self.request.GET.get('reset') == '1'
        context['css_version'] = _css_version()
        return context


SIGNUP_LIMIT = 10
SIGNUP_WINDOW_HOURS = 1
SIGNUP_LOCKED_MESSAGE = (
    'Too many sign-up attempts from this address. '
    'Please try again later.'
)
COMPANY_TAKEN_MESSAGE = (
    'That company name is already registered. If it is your company, '
    'ask its owner to give you access.'
)
USERNAME_RE = re.compile(r'^[A-Za-z0-9._-]+$')


def _signup_key(request):
    return 'signup-ip:%s' % request.META.get('REMOTE_ADDR', '')


def _signup_locked(key):
    since = timezone.now() - timedelta(hours=SIGNUP_WINDOW_HOURS)
    return (
        AccessAttempt.objects.filter(
            key=key, created_at__gte=since
        ).count()
        >= SIGNUP_LIMIT
    )


def _validate_signup(
    company_name,
    username,
    password,
    confirm,
    question_choice='',
    custom_question='',
    answer='',
):
    if not 2 <= len(company_name) <= 120:
        return 'Company name must be 2 to 120 characters.'
    if not 3 <= len(username) <= 30:
        return 'Username must be 3 to 30 characters.'
    if not USERNAME_RE.match(username):
        return (
            'Username may only contain letters, digits, dots, '
            'underscores and hyphens.'
        )
    if AgentCompany.objects.filter(name__iexact=company_name).exists():
        return COMPANY_TAKEN_MESSAGE
    if User.objects.filter(username__iexact=username).exists():
        return 'That username is taken. Please choose another.'
    if password != confirm:
        return 'Passwords do not match.'
    try:
        validate_password(
            password, user=User(username=username)
        )
    except ValidationError as exc:
        return ' '.join(exc.messages)
    _, security_error = _validate_security(
        question_choice, custom_question, answer
    )
    if security_error is not None:
        return security_error
    return None


def signup(request):
    if not getattr(settings, 'ALLOW_AGENT_SIGNUP', True):
        raise Http404
    error = None
    values = {'company': '', 'username': ''}
    if request.method == 'POST':
        key = _signup_key(request)
        if _signup_locked(key):
            error = SIGNUP_LOCKED_MESSAGE
        else:
            AccessAttempt.objects.create(key=key)
            company_name = (request.POST.get('company') or '').strip()
            username = (request.POST.get('username') or '').strip()
            password = request.POST.get('password') or ''
            confirm = request.POST.get('confirm') or ''
            question_choice = request.POST.get('question') or ''
            custom_question = request.POST.get('custom_question') or ''
            answer = request.POST.get('answer') or ''
            values = {'company': company_name, 'username': username}
            error = _validate_signup(
                company_name,
                username,
                password,
                confirm,
                question_choice,
                custom_question,
                answer,
            )
            question, _ = _validate_security(
                question_choice, custom_question, answer
            )
            if error is None:
                try:
                    with transaction.atomic():
                        user = User.objects.create_user(
                            username=username, password=password
                        )
                        company = AgentCompany.objects.create(
                            name=company_name
                        )
                        StaffMember.objects.create(
                            user=user,
                            agent_company=company,
                            role=StaffMember.Role.OWNER,
                        )
                        SecurityProfile.objects.create(
                            user=user,
                            question=question,
                            answer_hash=make_password(
                                normalise_answer(answer)
                            ),
                        )
                except IntegrityError:
                    error = _validate_signup(
                        company_name,
                        username,
                        password,
                        confirm,
                        question_choice,
                        custom_question,
                        answer,
                    ) or (
                        'Could not create your account. '
                        'Please try again.'
                    )
                else:
                    login(request, user)
                    messages.success(
                        request,
                        'Welcome, %s. Your company %s is ready.'
                        % (username, company_name),
                    )
                    return redirect('agent_shipments')
    return render(
        request,
        'tracking/agent_signup.html',
        {
            'error': error,
            'values': values,
            'questions': list(enumerate(SECURITY_QUESTIONS)),
            'css_version': _css_version(),
        },
    )


SECURITY_QUESTIONS = (
    'What was the name of your first school?',
    'What is the name of your favourite teacher?',
    'In which town did you attend secondary school?',
    'What was the make of your first car or motorbike?',
    'What is a phrase only you would know?',
)
SECURITY_QUESTION_CUSTOM = 'own'


def normalise_answer(raw):
    """Normalise a security answer before hashing or checking."""
    text = re.sub(r'\s+', ' ', (raw or '').strip().lower())
    return re.sub(r'[^a-z0-9 ]', '', text)


def _validate_security(question_choice, custom_question, answer):
    """Return (question, error) for the sign-up security fields."""
    if question_choice == SECURITY_QUESTION_CUSTOM:
        question = (custom_question or '').strip()
        if not 10 <= len(question) <= 200:
            return (
                None,
                'Your own question must be 10 to 200 characters.',
            )
    elif question_choice in [
        str(index) for index in range(len(SECURITY_QUESTIONS))
    ]:
        question = SECURITY_QUESTIONS[int(question_choice)]
    else:
        return None, 'Choose a security question.'
    if len(normalise_answer(answer)) < 4:
        return (
            None,
            'The answer must be at least 4 characters.',
        )
    return question, None


def _client_ip(request):
    """Return the client IP for rate limiting.

    Uses REMOTE_ADDR only, unless TRUSTED_PROXY_HEADER names a header
    to trust (default off, so X-Forwarded-For cannot dodge a limit).
    """
    header = getattr(settings, 'TRUSTED_PROXY_HEADER', '')
    if header:
        value = request.META.get(header, '')
        if value:
            return value.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '')


RESET_GENERIC_MESSAGE = (
    'We could not reset the password with those details. If you '
    'cannot recover your account, contact the platform owner.'
)
RESET_LOCKED_MESSAGE = 'Too many attempts. Please try again in 15 minutes.'
RESET_EXPIRY = timedelta(minutes=10)
RESET_STEP1_LIMIT = 20
RESET_IP_LIMIT = 10
RESET_HOUR = timedelta(hours=1)


def _reset_fake_question(username):
    """Deterministic stand-in question that reveals nothing."""
    digest = hmac.new(
        settings.SECRET_KEY.encode(),
        (username or '').encode(),
        hashlib.sha256,
    ).hexdigest()
    return SECURITY_QUESTIONS[int(digest, 16) % len(SECURITY_QUESTIONS)]


def _reset_keys(request, username):
    return (
        'forgot-ip:%s' % _client_ip(request),
        'forgot-user:%s' % (username or '').strip().lower(),
    )


def forgot_username(request):
    error = None
    if request.method == 'POST':
        key = 'forgot-step1-ip:%s' % _client_ip(request)
        if access_helpers.is_locked(
            key, limit=RESET_STEP1_LIMIT, window=RESET_HOUR
        ):
            error = RESET_LOCKED_MESSAGE
        else:
            access_helpers.record_failure(key)
            username = (request.POST.get('username') or '').strip()
            request.session['reset_username'] = username
            request.session['reset_started'] = (
                timezone.now().isoformat()
            )
            return redirect('agent_forgot_answer')
    return render(
        request,
        'tracking/agent_forgot.html',
        {'error': error, 'css_version': _css_version()},
    )


def _reset_session(request):
    username = request.session.get('reset_username') or ''
    started = request.session.get('reset_started') or ''
    try:
        started_at = timezone.datetime.fromisoformat(started)
    except (TypeError, ValueError):
        return '', None
    if timezone.is_naive(started_at):
        started_at = timezone.make_aware(started_at)
    if timezone.now() - started_at > RESET_EXPIRY:
        return '', None
    return username, started_at


def _clear_reset_session(request):
    for key in ('reset_username', 'reset_started'):
        request.session.pop(key, None)


def forgot_answer(request):
    username, _ = _reset_session(request)
    if not username:
        return redirect('agent_forgot')
    user = User.objects.filter(username=username).first()
    try:
        profile = user.securityprofile if user is not None else None
    except SecurityProfile.DoesNotExist:
        profile = None
    real = profile is not None
    question = profile.question if real else _reset_fake_question(username)
    error = None
    if request.method == 'POST':
        ip_key, user_key = _reset_keys(request, username)
        if access_helpers.is_locked(
            user_key
        ) or access_helpers.is_locked(
            ip_key, limit=RESET_IP_LIMIT, window=RESET_HOUR
        ):
            error = RESET_LOCKED_MESSAGE
        else:
            answer = request.POST.get('answer') or ''
            new_password = request.POST.get('new_password') or ''
            confirm = request.POST.get('confirm') or ''
            if real:
                passed = check_password(
                    normalise_answer(answer), profile.answer_hash
                )
            else:
                make_password('forgot-password-dummy-check')
                passed = False
            if not passed:
                access_helpers.record_failure(ip_key)
                access_helpers.record_failure(user_key)
                error = RESET_GENERIC_MESSAGE
            elif new_password != confirm:
                error = 'Passwords do not match.'
            else:
                try:
                    validate_password(new_password, user=user)
                except ValidationError as exc:
                    error = ' '.join(exc.messages)
                else:
                    user.set_password(new_password)
                    user.save()
                    access_helpers.clear_failures(user_key)
                    _clear_reset_session(request)
                    return redirect(
                        '%s?reset=1' % reverse('agent_login')
                    )
    return render(
        request,
        'tracking/agent_forgot_answer.html',
        {
            'error': error,
            'question': question,
            'css_version': _css_version(),
        },
    )


@agent_required
def account(request):
    try:
        profile = request.user.securityprofile
    except SecurityProfile.DoesNotExist:
        profile = None
    password_error = None
    password_done = False
    question_error = None
    question_done = False
    if request.method == 'POST':
        which = request.POST.get('form')
        current = request.POST.get('current_password') or ''
        if not request.user.check_password(current):
            error = 'The current password is not correct.'
            if which == 'question':
                question_error = error
            else:
                password_error = error
        elif which == 'question':
            question, question_error = _validate_security(
                request.POST.get('question') or '',
                request.POST.get('custom_question') or '',
                request.POST.get('answer') or '',
            )
            if question_error is None:
                SecurityProfile.objects.update_or_create(
                    user=request.user,
                    defaults={
                        'question': question,
                        'answer_hash': make_password(
                            normalise_answer(
                                request.POST.get('answer') or ''
                            )
                        ),
                    },
                )
                question_done = True
        else:
            new_password = request.POST.get('new_password') or ''
            if new_password != (request.POST.get('confirm') or ''):
                password_error = 'Passwords do not match.'
            else:
                try:
                    validate_password(new_password, user=request.user)
                except ValidationError as exc:
                    password_error = ' '.join(exc.messages)
                else:
                    request.user.set_password(new_password)
                    request.user.save()
                    update_session_auth_hash(request, request.user)
                    password_done = True
    else:
        try:
            profile = request.user.securityprofile
        except SecurityProfile.DoesNotExist:
            profile = None
    return render(
        request,
        'tracking/agent_account.html',
        {
            'profile': profile,
            'is_owner': _is_owner(request.user),
            'password_error': password_error,
            'password_done': password_done,
            'question_error': question_error,
            'question_done': question_done,
            'questions': list(enumerate(SECURITY_QUESTIONS)),
            'css_version': _css_version(),
        },
    )


def _current_label(container):
    current = timeline_helpers.current_stage(container)
    return current['label'] if current is not None else 'Not started'


def _current_label_and_order(container, order_by_key):
    current = timeline_helpers.current_stage(container)
    if current is None:
        return 'Not started', -1
    return current['label'], order_by_key.get(current['key'], -1)


@agent_required
@require_GET
def shipment_list(request):
    show_closed = request.GET.get('show') == 'closed'
    shipments = (
        Shipment.objects.filter(
            importer__agent_company=request.agent_company,
            deleted_at__isnull=True,
        )
        .select_related('importer')
        .prefetch_related('containers')
        .annotate(
            container_count=Count('containers', distinct=True),
            last_update=Max('containers__stageevent__updated_at'),
            is_closed=Case(When(closed_at__isnull=True, then=0), default=1),
        )
        .order_by('-created_at', '-id')
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
        {
            'rows': rows,
            'show_closed': show_closed,
            'has_shipments': Shipment.objects.filter(
                importer__agent_company=request.agent_company,
                deleted_at__isnull=True,
            ).exists(),
            'show_security_notice': not SecurityProfile.objects.filter(
                user=request.user
            ).exists(),
            'css_version': _css_version(),
        },
    )


def _random_code():
    return '%06d' % secrets.randbelow(1000000)


@agent_required
def shipment_new(request):
    company = request.agent_company
    names = list(
        Importer.objects.filter(agent_company=company)
        .order_by('name')
        .values_list('name', flat=True)
    )
    context = {
        'uncertain': [],
        'prefill': False,
        'scan_document': None,
        'importer_check': False,
        'importer_source': None,
        'importer_missing': False,
        'importer_names': names,
        'importer_names_json': json.dumps(names),
        'css_version': _css_version(),
    }
    if request.method == 'POST':
        form = ShipmentForm(company, request.POST)
        if form.is_valid():
            with transaction.atomic():
                importer = form.resolved_importer
                if importer is None:
                    importer, _ = Importer.objects.get_or_create(
                        agent_company=company,
                        name=form.cleaned_data['importer_name'],
                        defaults={'access_code': _random_code()},
                    )
                shipment = Shipment.objects.create(
                    importer=importer,
                    bl_number=form.cleaned_data['bl_number'],
                    shipping_line=form.cleaned_data['shipping_line'],
                    port=form.cleaned_data['port'],
                    destination_area=form.cleaned_data['destination_area'],
                )
                for number, size, confirmed in form.container_rows():
                    bad_digit = not is_valid_container_number(number)
                    container = Container.objects.create(
                        shipment=shipment,
                        number=number,
                        size=size,
                        check_digit_confirmed=bad_digit and confirmed,
                    )
                    if bad_digit and confirmed:
                        AuditLog.objects.create(
                            shipment=shipment,
                            container=container,
                            user=request.user,
                            action='container_created',
                            new_value=(
                                'Container %s saved with a check digit '
                                'that does not match (confirmed by the '
                                'agent).' % number
                            ),
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
                initial['importer_name'] = preselected.name
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
        form.initial['importer_name'] = importer_name
    rows = []
    for item in result.get('containers') or []:
        number = normalise_number(item.get('number'))
        digit_failed = has_container_format(
            number
        ) and not is_valid_container_number(number)
        rows.append(
            {
                'number': item.get('number') or '',
                'size': item.get('size') or '',
                'errors': [],
                'check': (not item.get('valid'))
                or not item.get('size'),
                'check_digit_failed': digit_failed,
                'check_digit_hint': (
                    '%s does not pass the container check digit. '
                    'If the first ten characters are right, the last '
                    'digit would be %s. Please check it against the '
                    'document.'
                    % (
                        number,
                        expected_check_digit(number[:10]),
                    )
                ) if digit_failed else '',
                'confirmed': False,
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
            'importer_source': result.get('importer_source'),
            'importer_missing': not (
                result.get('importer_name') or ''
            ).strip(),
            'pages_total': result.get('pages_total') or 1,
            'pages_read': result.get('pages_read') or 1,
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
            'importer_source': result.get('importer_source'),
            'importer_missing': not (
                result.get('importer_name') or ''
            ).strip(),
            'pages_total': result.get('pages_total') or 1,
            'pages_read': result.get('pages_read') or 1,
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
        Shipment,
        pk=pk,
        importer__agent_company=request.agent_company,
        deleted_at__isnull=True,
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
        messages.error(
            request,
            'Cannot undo %s for %s. %s'
            % (stage.label, container.number, problems[0]),
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
        for step in steps:
            if step['state'] in ('completed', 'current'):
                step['undo_problems'] = stage_rules.check_can_undo(
                    container, stages_by_key[step['key']]
                )
            else:
                step['undo_problems'] = []
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
                 'occurred_at': latest,
                 'undo_problems': (
                     stage_rules.check_can_undo_shipment(shipment, stage)
                 )}
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
            {
                'stage': stage,
                'state': state,
                'occurred_at': None,
                'undo_problems': [],
            }
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
            'header_updated': _updated_ago(
                StageEvent.objects.filter(container__shipment=shipment)
                .order_by('-updated_at')
                .values_list('updated_at', flat=True)
                .first(),
                now,
            ),
            'css_version': _css_version(),
        },
    )


@agent_required
def shipment_delete(request, pk):
    shipment = _scoped_shipment(request, pk)
    if request.method == 'POST':
        shipment.deleted_at = timezone.now()
        shipment.save(update_fields=['deleted_at'])
        _log(
            request, shipment, None, 'shipment_deleted', '',
            shipment.bl_number,
        )
        messages.success(
            request,
            'Shipment %s deleted.' % shipment.bl_number,
        )
        return redirect('agent_shipments')
    return render(
        request,
        'tracking/agent_shipment_delete.html',
        {
            'shipment': shipment,
            'containers': list(
                shipment.containers.order_by('number')
            ),
            'css_version': _css_version(),
        },
    )


@agent_required
@require_GET
def deleted_list(request):
    shipments = (
        Shipment.objects.filter(
            importer__agent_company=request.agent_company,
            deleted_at__isnull=False,
        )
        .select_related('importer')
        .prefetch_related('containers')
        .order_by('-deleted_at')
    )
    rows = []
    for shipment in shipments:
        purgeable, reason = shipment_stages.is_purgeable(shipment)
        rows.append(
            {
                'shipment': shipment,
                'purgeable': purgeable,
                'purge_reason': reason,
            }
        )
    return render(
        request,
        'tracking/agent_shipments_deleted.html',
        {
            'rows': rows,
            'purgeable_count': sum(1 for row in rows if row['purgeable']),
            'is_owner': _is_owner(request.user),
            'css_version': _css_version(),
        },
    )


def _is_owner(user):
    try:
        return user.staffmember.role == StaffMember.Role.OWNER
    except (StaffMember.DoesNotExist, AttributeError):
        return False


def _owner_or_forbidden(request):
    try:
        role = request.user.staffmember.role
    except (StaffMember.DoesNotExist, AttributeError):
        role = None
    if role != StaffMember.Role.OWNER:
        return render(
            request,
            'tracking/agent_forbidden.html',
            {'css_version': _css_version()},
            status=403,
        )
    return None


def _danger_counts(shipments):
    shipment_ids = [shipment.pk for shipment in shipments]
    return {
        'shipments': len(shipment_ids),
        'containers': Container.objects.filter(
            shipment_id__in=shipment_ids
        ).count(),
        'importers': Importer.objects.filter(
            shipment__id__in=shipment_ids
        ).distinct().count(),
        'documents': Document.objects.filter(
            shipment_id__in=shipment_ids
        ).count(),
    }


@agent_required
def shipment_clear(request):
    denied = _owner_or_forbidden(request)
    if denied is not None:
        return denied
    company = request.agent_company
    key = 'danger-user:%d' % request.user.pk
    error = None
    shipments = list(
        Shipment.objects.filter(
            importer__agent_company=company, deleted_at__isnull=True
        ).order_by('pk')
    )
    if request.method == 'POST':
        if access_helpers.is_locked(key):
            error = LOGIN_LOCKED_MESSAGE
        elif (request.POST.get('confirm_word') or '') != 'CLEAR':
            access_helpers.record_failure(key)
            error = 'Type CLEAR in the box to confirm.'
        elif not request.user.check_password(
            request.POST.get('current_password') or ''
        ):
            access_helpers.record_failure(key)
            error = 'The current password is not correct.'
        else:
            access_helpers.clear_failures(key)
            counts = _danger_counts(shipments)
            counts['documents'] = Document.objects.filter(
                company=company
            ).count()
            reset_codes = bool(request.POST.get('reset_codes'))
            now = timezone.now()
            with transaction.atomic():
                for shipment in shipments:
                    shipment.deleted_at = now
                    shipment.save(update_fields=['deleted_at'])
                    AuditLog.objects.create(
                        shipment=shipment,
                        container=None,
                        user=request.user,
                        action='shipment_cleared',
                        new_value='Cleared in bulk',
                    )
                rotated = 0
                if reset_codes:
                    for importer in Importer.objects.filter(
                        agent_company=company
                    ):
                        access_helpers.rotate_code(
                            importer, _random_code()
                        )
                        rotated += 1
                CompanyEvent.objects.create(
                    company=company,
                    user=request.user,
                    action='shipments_cleared',
                    detail=(
                        'shipments=%d containers=%d importers=%d '
                        'documents=%d codes_reset=%d'
                        % (
                            counts['shipments'],
                            counts['containers'],
                            counts['importers'],
                            counts['documents'],
                            rotated,
                        )
                    ),
                )
            messages.success(
                request,
                '%d shipments cleared. You can restore them from '
                'Deleted shipments.' % counts['shipments'],
            )
            if reset_codes:
                messages.success(
                    request,
                    '%d importer codes were reset. See the Importers '
                    'page.' % rotated,
                )
            return redirect('agent_shipments')
    counts = _danger_counts(shipments)
    counts['documents'] = Document.objects.filter(
        company=company
    ).count()
    return render(
        request,
        'tracking/agent_shipment_clear.html',
        {
            'counts': counts,
            'error': error,
            'css_version': _css_version(),
        },
    )


@agent_required
def shipment_purge(request):
    denied = _owner_or_forbidden(request)
    if denied is not None:
        return denied
    company = request.agent_company
    key = 'danger-user:%d' % request.user.pk
    error = None
    deleted = list(
        Shipment.objects.filter(
            importer__agent_company=company, deleted_at__isnull=False
        ).order_by('pk')
    )
    rows = []
    for shipment in deleted:
        purgeable, reason = shipment_stages.is_purgeable(shipment)
        rows.append(
            {'shipment': shipment, 'purgeable': purgeable, 'reason': reason}
        )
    shipments = [row['shipment'] for row in rows if row['purgeable']]
    staying = [row for row in rows if not row['purgeable']]
    skipped = len(staying)
    if request.method == 'POST':
        if access_helpers.is_locked(key):
            error = LOGIN_LOCKED_MESSAGE
        elif (request.POST.get('confirm_word') or '') != 'DELETE FOREVER':
            access_helpers.record_failure(key)
            error = 'Type DELETE FOREVER in the box to confirm.'
        elif not request.user.check_password(
            request.POST.get('current_password') or ''
        ):
            access_helpers.record_failure(key)
            error = 'The current password is not correct.'
        else:
            access_helpers.clear_failures(key)
            counts = _danger_counts(shipments)
            counts['skipped'] = skipped
            shipment_ids = [shipment.pk for shipment in shipments]
            with transaction.atomic():
                for document in Document.objects.filter(
                    shipment_id__in=shipment_ids
                ):
                    if document.file:
                        try:
                            document.file.delete(save=False)
                        except OSError:
                            pass
                    document.delete()
                AuditLog.objects.filter(
                    shipment_id__in=shipment_ids
                ).delete()
                Container.objects.filter(
                    shipment_id__in=shipment_ids
                ).delete()
                Shipment.objects.filter(pk__in=shipment_ids).delete()
                CompanyEvent.objects.create(
                    company=company,
                    user=request.user,
                    action='shipments_purged',
                    detail=(
                        'shipments=%d containers=%d importers=%d '
                        'documents=%d skipped=%d'
                        % (
                            counts['shipments'],
                            counts['containers'],
                            counts['importers'],
                            counts['documents'],
                            counts['skipped'],
                        )
                    ),
                )
            removed = counts['shipments']
            if removed:
                message = (
                    '%d shipments permanently deleted.' % removed
                )
                if counts['skipped']:
                    message += (
                        ' %d kept because they are not finished.'
                        % counts['skipped']
                    )
                messages.success(request, message)
            else:
                if counts['skipped']:
                    message = (
                        '0 shipments permanently deleted. '
                        '%d kept because they are not finished.'
                        % counts['skipped']
                    )
                else:
                    message = '0 shipments permanently deleted.'
                messages.warning(request, message)
            return redirect('agent_deleted_shipments')
    return render(
        request,
        'tracking/agent_shipment_purge.html',
        {
            'counts': _danger_counts(shipments),
            'purgeable': [
                {'shipment': row['shipment']}
                for row in rows
                if row['purgeable']
            ],
            'staying': staying,
            'error': error,
            'css_version': _css_version(),
        },
    )


def _active_bl_clash(company, bl_number, exclude_pk):
    """True when another active shipment uses this BL number."""
    wanted = re.sub(r'\s+', '', bl_number or '').upper()
    existing = Shipment.objects.filter(
        importer__agent_company=company,
        deleted_at__isnull=True,
    ).exclude(pk=exclude_pk).values_list('bl_number', flat=True)
    return any(
        re.sub(r'\s+', '', bl or '').upper() == wanted for bl in existing
    )


@agent_required
@require_POST
def shipment_restore(request, pk):
    shipment = get_object_or_404(
        Shipment, pk=pk, importer__agent_company=request.agent_company
    )
    if shipment.deleted_at is None:
        return redirect('agent_shipment_detail', pk=shipment.pk)
    if _active_bl_clash(
        request.agent_company, shipment.bl_number, shipment.pk
    ):
        messages.error(
            request,
            'Cannot restore shipment %s: an active shipment already '
            'uses this BL number.' % shipment.bl_number,
        )
        return redirect('agent_deleted_shipments')
    shipment.deleted_at = None
    shipment.save(update_fields=['deleted_at'])
    _log(
        request, shipment, None, 'shipment_restored', '',
        shipment.bl_number,
    )
    messages.success(
        request,
        '%s restored.' % shipment.bl_number,
    )
    return redirect('agent_shipments')


@agent_required
@require_GET
def importer_list(request):
    company = request.agent_company
    query = (request.GET.get('q') or '').strip()
    importers = (
        Importer.objects.filter(agent_company=company)
        .annotate(
            shipment_count=Count(
                'shipment',
                filter=Q(shipment__deleted_at__isnull=True),
            )
        )
        .order_by('-pk')
    )
    total = importers.count()
    if query:
        importers = importers.filter(name__icontains=query)
    new_importer = None
    added = request.GET.get('added') or ''
    if added.isdigit():
        new_importer = Importer.objects.filter(
            pk=int(added), agent_company=company
        ).first()
    rotated_importer = None
    rotated = request.GET.get('rotated') or ''
    if rotated.isdigit():
        rotated_importer = Importer.objects.filter(
            pk=int(rotated), agent_company=company
        ).first()
    return render(
        request,
        'tracking/agent_importers.html',
        {
            'importers': importers,
            'new_importer': new_importer,
            'rotated_importer': rotated_importer,
            'site': request.build_absolute_uri('/'),
            'query': query,
            'total': total,
            'shown': importers.count(),
            'css_version': _css_version(),
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
    code = _random_code()
    access_helpers.rotate_code(importer, code)
    return redirect(
        '%s?rotated=%d' % (reverse('agent_importers'), importer.pk)
    )


SCAN_MAX_BYTES = 4 * 1024 * 1024
SCAN_DAILY_LIMIT = 50
_JPEG_SIG = b'\xff\xd8\xff'
_PNG_SIG = b'\x89PNG\r\n\x1a\n'
_PDF_SIG = b'%PDF-'


def _scan_upload_kind(data):
    """JPEG, PNG or PDF by file signature, else None."""
    if data[:3] == _JPEG_SIG:
        return 'jpeg'
    if data[:8] == _PNG_SIG:
        return 'png'
    if _PDF_SIG in bytes(data[:1024]):
        return 'pdf'
    return None


def _scan_upload_error(upload):
    """Check the size and file signature of a scan upload."""
    if upload is None:
        return 'Please upload a JPEG, PNG or PDF.'
    if upload.size > SCAN_MAX_BYTES:
        return 'That file is larger than 4 MB. Please upload a smaller file.'
    head = upload.read(1024)
    upload.seek(0)
    if _scan_upload_kind(head) is None:
        return 'Please upload a JPEG, PNG or PDF.'
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
                request,
                'tracking/agent_scan.html',
                {'error': error, 'css_version': _css_version()},
            )
        data = upload.read()
        Path(settings.MEDIA_ROOT, 'private').mkdir(
            parents=True, exist_ok=True
        )
        document = Document.objects.create(
            company=company,
            uploaded_by=request.user,
            kind=Document.Kind.BL,
        )
        kind = _scan_upload_kind(data)
        extension = {'png': '.png', 'pdf': '.pdf'}.get(kind, '.jpg')
        document.file.save(
            'scan-%d%s' % (document.pk, extension), ContentFile(data)
        )
        try:
            result = extract_shipment(data)
        except PdfError as exc:
            logger.warning('bl scan failed document=%d', document.pk)
            if str(exc) == PROTECTED_PDF_MESSAGE:
                return render(
                    request,
                    'tracking/agent_scan.html',
                    {
                        'error': PROTECTED_PDF_MESSAGE,
                        'css_version': _css_version(),
                    },
                )
            return render(
                request,
                'tracking/agent_scan.html',
                {'scan_failed': True, 'css_version': _css_version()},
            )
        except Exception:
            logger.warning('bl scan failed document=%d', document.pk)
            return render(
                request,
                'tracking/agent_scan.html',
                {'scan_failed': True, 'css_version': _css_version()},
            )
        request.session[_scan_session_key(document.pk)] = result
        return redirect(
            '%s?scan=%d' % (reverse('agent_shipment_new'), document.pk)
        )
    return render(
        request,
        'tracking/agent_scan.html',
        {'css_version': _css_version()},
    )


@agent_required
@require_GET
def document_file(request, pk):
    document = get_object_or_404(
        Document, pk=pk, company=request.agent_company
    )
    content_type, _ = mimetypes.guess_type(document.file.name)
    response = FileResponse(
        document.file.open('rb'),
        content_type=content_type or 'application/octet-stream',
    )
    response['Content-Disposition'] = 'attachment; filename="document"'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


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


def _validated_page_context(company, context):
    """Validate the assistant page context against company data.

    Any invalid part is silently ignored: a shipment outside this
    company drops the whole context, a container outside the shipment
    drops only the focus.
    """
    if not isinstance(context, dict):
        return None
    try:
        shipment_id = int(context.get('shipment_id'))
    except (TypeError, ValueError):
        return None
    shipment = (
        Shipment.objects.filter(
            pk=shipment_id,
            importer__agent_company=company,
            deleted_at__isnull=True,
        )
        .select_related('importer')
        .first()
    )
    if shipment is None:
        return None
    numbers = list(
        shipment.containers.order_by('number').values_list(
            'number', flat=True
        )
    )
    focused = context.get('container_number')
    match = None
    if isinstance(focused, str):
        for number in numbers:
            if number.upper() == focused.strip().upper():
                match = number
                break
    return {
        'shipment_id': shipment.pk,
        'bl_number': shipment.bl_number,
        'importer_name': shipment.importer.name,
        'container_numbers': numbers,
        'focused_container': match,
    }


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
    input_source = payload.get('input_source', 'typed')
    if input_source != 'voice':
        input_source = 'typed'
    history = request.session.get('assistant_history', [])
    if not isinstance(history, list):
        history = []
    page_context = _validated_page_context(
        request.agent_company, payload.get('context')
    )
    result = run_assistant(
        request.user, text, history, input_source=input_source,
        page_context=page_context,
    )
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
    recorded, refused = [], []
    for item in proposal.actions or []:
        _apply_assistant_item(
            request, request.agent_company, item, timezone.now(),
            recorded, refused,
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


TRANSCRIBE_MAX_BYTES = 4 * 1024 * 1024


def _css_version():
    """Stylesheet modification time for cache-busting the CSS link."""
    css = (
        Path(__file__).resolve().parent
        / 'static'
        / 'tracking'
        / 'style.css'
    )
    try:
        return int(css.stat().st_mtime)
    except OSError:
        return 0
TRANSCRIBE_DAILY_LIMIT = 100
TRANSCRIBE_FRIENDLY = 'We could not hear that. Please type it instead.'


def _wav_is_valid(data):
    """Check RIFF/WAVE bytes for mono 16-bit PCM at 24000 Hz."""
    if len(data) < 44 or data[0:4] != b'RIFF' or data[8:12] != b'WAVE':
        return False
    offset = 12
    while offset + 8 <= len(data):
        chunk = data[offset:offset + 4]
        size = struct.unpack('<I', data[offset + 4:offset + 8])[0]
        body = data[offset + 8:offset + 8 + size]
        if chunk == b'fmt ' and len(body) >= 16:
            audio_format, channels, rate, _, _, bits = struct.unpack(
                '<HHIIHH', body[:16]
            )
            return (
                audio_format == 1
                and channels == 1
                and rate == 24000
                and bits == 16
            )
        offset += 8 + size + (size % 2)
    return False


@agent_required
@require_POST
def assistant_transcribe(request):
    upload = request.FILES.get('audio')
    data = upload.read() if upload is not None else b''
    if not data or len(data) > TRANSCRIBE_MAX_BYTES or not _wav_is_valid(
        data
    ):
        return JsonResponse({'error': TRANSCRIBE_FRIENDLY}, status=400)
    key = 'assistant-transcribe:%d:%s' % (
        request.user.pk, timezone.localdate().isoformat()
    )
    count = cache.get(key, 0)
    if count >= TRANSCRIBE_DAILY_LIMIT:
        return JsonResponse({'error': TRANSCRIBE_FRIENDLY}, status=429)
    try:
        text = transcribe_audio(data, request.agent_company)
    except Exception:
        logger.warning('assistant transcribe failed')
        return JsonResponse({'error': TRANSCRIBE_FRIENDLY}, status=502)
    cache.set(key, count + 1, 24 * 60 * 60)
    return JsonResponse({'text': text})
