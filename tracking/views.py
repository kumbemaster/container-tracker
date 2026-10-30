from django.conf import settings
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from django.utils import timezone

from . import access as access_helpers
from . import timeline as timeline_helpers
from .clocks import build_clocks
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


def _age_display(updated_at, now):
    if updated_at is None:
        return 'No updates yet.'
    seconds = max(0, int((now - updated_at).total_seconds()))
    if seconds < 60:
        return 'Last updated just now.'
    minutes = seconds // 60
    if minutes < 60:
        unit = 'minute' if minutes == 1 else 'minutes'
        return 'Last updated %d %s ago.' % (minutes, unit)
    hours = minutes // 60
    if hours < 24:
        unit = 'hour' if hours == 1 else 'hours'
        return 'Last updated %d %s ago.' % (hours, unit)
    days = hours // 24
    unit = 'day' if days == 1 else 'days'
    return 'Last updated %d %s ago.' % (days, unit)


def _container_context(container):
    now = timezone.now()
    fresh = timeline_helpers.freshness(container, now=now)
    if settings.SHOW_FREE_TIME_CLOCKS:
        clocks = build_clocks(container)
    else:
        clocks = [None, None, None]
    steps = timeline_helpers.build_timeline(container, now=now)
    arrived_at = next(
        (
            step['occurred_at']
            for step in steps
            if step['key'] == 'arrived_at_port'
            and step['occurred_at'] is not None
        ),
        None,
    )
    return {
        'container': container,
        'shipment': container.shipment,
        'steps': steps,
        'clocks': clocks,
        'fresh': fresh,
        'age_display': _age_display(fresh['updated_at'], now),
        'hold': timeline_helpers.holding(container),
        'arrived_on': (
            timezone.localtime(arrived_at).date()
            if arrived_at is not None
            else None
        ),
    }


def _panel_context(container):
    context = _container_context(container)
    steps = context['steps']
    current = next(
        (step for step in steps if step['state'] == 'current'), None
    )
    context['current_label'] = current['label'] if current is not None else None
    context['open'] = False
    return context


@require_GET
def timeline(request, ref):
    target = access_helpers.find_target(ref)
    if target is None:
        return _private(redirect('/'))
    session = _session_from_request(request)
    if session is None or not access_helpers.has_access(session, _owner_of(target)):
        return _private(redirect('/'))
    if isinstance(target, Container):
        context = _panel_context(target)
        context['is_bl_page'] = False
        return _private(render(request, 'tracking/timeline.html', context))
    containers = list(target.containers.order_by('number'))
    panels = [_panel_context(container) for container in containers]
    if len(panels) == 1:
        panels[0]['open'] = True
    return _private(
        render(
            request,
            'tracking/timeline.html',
            {
                'is_bl_page': True,
                'shipment': target,
                'container_count': len(panels),
                'panels': panels,
            },
        )
    )


@require_GET
def agent_login(request):
    return _private(render(request, 'tracking/agent_login.html'))
