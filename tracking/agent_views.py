import secrets
from functools import wraps

from django.conf import settings
from django.contrib.auth.views import LoginView
from django.db import transaction
from django.db.models import Case, Count, F, Max, When
from django.forms.utils import ErrorDict
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from . import access as access_helpers
from . import timeline as timeline_helpers
from .forms import ShipmentForm
from .models import Container, Importer, Shipment, StaffMember, Stage

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
                    bl_number=form.cleaned_data['bl_number'].strip().upper(),
                    shipping_line=form.cleaned_data['shipping_line'],
                    port=form.cleaned_data['port'],
                    destination_area=form.cleaned_data['destination_area'],
                    discharge_date=form.cleaned_data['discharge_date'],
                    **form.free_days(),
                )
                for number, size in form.container_rows():
                    Container.objects.create(
                        shipment=shipment, number=number, size=size
                    )
            return redirect('agent_shipment_detail', pk=shipment.pk)
    else:
        form = ShipmentForm(company)
    return render(
        request, 'tracking/agent_shipment_new.html', {'form': form}
    )


@agent_required
@require_GET
def shipment_detail(request, pk):
    shipment = get_object_or_404(
        Shipment, pk=pk, importer__agent_company=request.agent_company
    )
    containers = list(shipment.containers.order_by('number'))
    entries = [
        {'container': container, 'current_step': _current_label(container)}
        for container in containers
    ]
    message = 'Track your shipment %s at %s with this code: %s' % (
        shipment.bl_number,
        request.build_absolute_uri('/'),
        shipment.importer.access_code,
    )
    return render(
        request,
        'tracking/agent_shipment_detail.html',
        {'shipment': shipment, 'entries': entries, 'message': message},
    )


@agent_required
@require_GET
def importer_list(request):
    importers = Importer.objects.filter(
        agent_company=request.agent_company
    ).order_by('name')
    return render(
        request, 'tracking/agent_importers.html', {'importers': importers}
    )


@agent_required
@require_POST
def importer_rotate(request, pk):
    importer = get_object_or_404(
        Importer, pk=pk, agent_company=request.agent_company
    )
    access_helpers.rotate_code(importer, _random_code())
    return redirect('agent_importers')
