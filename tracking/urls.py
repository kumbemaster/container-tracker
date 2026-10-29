from django.urls import path

from . import views

urlpatterns = [
    path('', views.search, name='search'),
    path('track/', views.track, name='track'),
    path('track/verify/', views.verify, name='verify'),
    path('s/<str:ref>/', views.timeline, name='timeline'),
    path('agent/login/', views.agent_login, name='agent_login'),
]
