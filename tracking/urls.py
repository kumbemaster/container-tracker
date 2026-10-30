from django.contrib.auth.views import LogoutView
from django.urls import path

from . import agent_views, views

urlpatterns = [
    path('', views.search, name='search'),
    path('track/', views.track, name='track'),
    path('track/verify/', views.verify, name='verify'),
    path('s/<str:ref>/', views.timeline, name='timeline'),
    path(
        'agent/login/',
        agent_views.AgentLoginView.as_view(),
        name='agent_login',
    ),
    path(
        'agent/logout/',
        LogoutView.as_view(next_page='/'),
        name='agent_logout',
    ),
    path(
        'agent/shipments/',
        agent_views.shipment_list,
        name='agent_shipments',
    ),
    path(
        'agent/shipments/new/',
        agent_views.shipment_new,
        name='agent_shipment_new',
    ),
    path(
        'agent/shipments/<int:pk>/',
        agent_views.shipment_detail,
        name='agent_shipment_detail',
    ),
    path(
        'agent/importers/',
        agent_views.importer_list,
        name='agent_importers',
    ),
    path(
        'agent/importers/<int:pk>/rotate/',
        agent_views.importer_rotate,
        name='agent_importer_rotate',
    ),
]
