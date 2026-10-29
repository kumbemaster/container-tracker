from django.conf import settings
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from . import access as access_helpers
from .models import Container

COOKIE_NAME = 'device_token'
ONE_YEAR = 365 * 24 * 60 * 60

WRONG_DETAILS_MESSAGE = (
    'We couldn’t find a shipment with those details. '
    'Check the number and the code your clearing agent gave you.'
)
LOCKED_MESSAGE = 'Too many attempts. Please try again in 15 minutes.'


def _private(response):
    response['Cache-Control'] = 'no-store'
    response['X-Robots-Tag'] = 'noindex'
    return response


def _normalise(query):
    if not isinstance(query, str):
        return ''
    return query.strip().upper()


def _session_from_request(request):
    return access_helpers.get_session(request.COOKIES.get(COOKIE_NAME))


def _owner_of(target):
    if isinstance(target, Container):
        return target.shipment.importer
    return target.importer


def _ref_of(target):
    if isinstance(target, Container):
        return target.number
    return target.bl_number


@require_GET
def search(request):
    response = render(
        request, 'tracking/search.html', {'query': '', 'show_dialog': False}
    )
    return _private(response)


@require_POST
def track(request):
    raw_query = request.POST.get('q', '')
    target = access_helpers.find_target(raw_query)
    if target is not None:
        session = _session_from_request(request)
        if session is not None and access_helpers.has_access(
            session, _owner_of(target)
        ):
            return _private(redirect('timeline', ref=_ref_of(target)))
    response = render(
        request,
        'tracking/search.html',
        {'query': raw_query, 'show_dialog': True, 'error': ''},
    )
    return _private(response)


@require_POST
def verify(request):
    raw_query = request.POST.get('q', '')
    code = request.POST.get('code', '')
    query = _normalise(raw_query)
    ip_key = 'ip:%s' % request.META.get('REMOTE_ADDR', '')
    target_key = 'target:%s' % query
    if access_helpers.is_locked(ip_key) or access_helpers.is_locked(target_key):
        response = render(
            request,
            'tracking/search.html',
            {'query': raw_query, 'show_dialog': True, 'error': LOCKED_MESSAGE},
        )
        return _private(response)
    target = access_helpers.find_target(raw_query)
    owner = _owner_of(target) if target is not None else None
    if owner is not None and access_helpers.code_is_valid(owner, code):
        session = _session_from_request(request)
        if session is None:
            session, raw_token = access_helpers.create_device_session()
        else:
            raw_token = request.COOKIES.get(COOKIE_NAME)
        access_helpers.grant_access(session, owner)
        response = redirect('timeline', ref=_ref_of(target))
        response.set_cookie(
            COOKIE_NAME,
            raw_token,
            max_age=ONE_YEAR,
            httponly=True,
            samesite='Lax',
            secure=not settings.DEBUG,
        )
        return _private(response)
    access_helpers.record_failure(ip_key)
    access_helpers.record_failure(target_key)
    response = render(
        request,
        'tracking/search.html',
        {
            'query': raw_query,
            'show_dialog': True,
            'error': WRONG_DETAILS_MESSAGE,
        },
    )
    return _private(response)


@require_GET
def timeline(request, ref):
    target = access_helpers.find_target(ref)
    if target is None:
        return _private(redirect('/'))
    session = _session_from_request(request)
    if session is None or not access_helpers.has_access(session, _owner_of(target)):
        return _private(redirect('/'))
    if isinstance(target, Container):
        context = {'container_number': target.number, 'container_numbers': None}
    else:
        numbers = list(
            target.containers.order_by('number').values_list('number', flat=True)
        )
        context = {'container_number': None, 'container_numbers': numbers}
    return _private(render(request, 'tracking/timeline.html', context))


@require_GET
def agent_login(request):
    return _private(render(request, 'tracking/agent_login.html'))
