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
        'agent/shipments/<int:pk>/stages/update/',
        agent_views.stage_update,
        name='agent_stage_update',
    ),
    path(
        'agent/shipments/<int:pk>/stages/select/',
        agent_views.stage_select,
        name='agent_stage_select',
    ),
    path(
        'agent/shipments/<int:pk>/stages/unselect/',
        agent_views.stage_unselect,
        name='agent_stage_unselect',
    ),
    path(
        'agent/shipments/<int:pk>/stages/undo/',
        agent_views.stage_undo,
        name='agent_stage_undo',
    ),
    path(
        'agent/shipments/<int:pk>/waiting/',
        agent_views.waiting_update,
        name='agent_waiting_update',
    ),
    path(
        'agent/importers/',
        agent_views.importer_list,
        name='agent_importers',
    ),
    path(
        'agent/importers/add/',
        agent_views.importer_add,
        name='agent_importer_add',
    ),
    path(
        'agent/importers/<int:pk>/rotate/',
        agent_views.importer_rotate,
        name='agent_importer_rotate',
    ),
    path(
        'agent/importers/<int:pk>/rename/',
        agent_views.importer_rename,
        name='agent_importer_rename',
    ),
]
