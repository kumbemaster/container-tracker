from django.contrib import admin

from .models import AgentCompany, Container, Importer, Shipment, Stage, StageEvent

admin.site.register(AgentCompany)
admin.site.register(Importer)
admin.site.register(Shipment)
admin.site.register(Container)
admin.site.register(Stage)
admin.site.register(StageEvent)
